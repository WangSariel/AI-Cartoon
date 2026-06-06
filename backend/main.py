import asyncio
import contextlib
import datetime
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from config import settings
from database import SessionLocal, get_db, init_db
from logging_config import configure_logging, request_id_from, request_id_var
from metrics import metrics_payload, manga_job_timer, record_manga_job, record_request
from models import Chapter, ChatMessage, MangaImage, Story, StoryAssetGroup
from paths import (
    BACKEND_DIR,
    COVERS_DIR,
    MANGA_OUTPUTS_DIR,
    THUMB_DIR,
    asset_group_ref_dir,
    backend_path,
    chapter_dir,
    chapter_ref_dir,
    legacy_chapter_ref_image,
    legacy_story_ref_image,
    story_dir,
    story_ref_dir,
)
from schemas import (
    ChapterOut,
    ChatMessageIn,
    ChatMessageOut,
    MangaImageOut,
    StoryCreate,
    StoryOut,
    StoryUpdate,
)
from services.deepseek import chat_stream, generate_novel, split_scenes
from services.errors import MissingApiKeyError
from services.image2 import close_image_client, generate_manga_image

load_dotenv()
configure_logging()

logger = logging.getLogger("main")
ACTIVE_MANGA_GENERATIONS: set[int] = set()
MANGA_GENERATION_JOBS: dict[int, "MangaGenerationJob"] = {}
MANGA_GENERATION_SEMAPHORE = asyncio.Semaphore(settings.max_manga_generation_jobs)

app = FastAPI(title="Novel & Manga Generator")

MAX_UPLOAD_BYTES = settings.max_upload_bytes
MAX_CHARACTER_PROFILE_CHARS = settings.max_character_profile_chars
MAX_IMPORT_ZIP_BYTES = settings.max_import_zip_bytes
API_TOKENS = set(settings.api_tokens)
LOCAL_CLIENT_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(MissingApiKeyError)
async def _missing_api_key_handler(request: Request, exc: MissingApiKeyError):
    return JSONResponse(status_code=400, content={"detail": str(exc), "provider": exc.provider})

# Serve generated manga images as static files
manga_dir = MANGA_OUTPUTS_DIR
manga_dir.mkdir(parents=True, exist_ok=True)
thumb_dir = THUMB_DIR
THUMBNAIL_WIDTHS = (320, 480, 720, 960, 1280)
THUMBNAIL_QUALITY = 76
THUMBNAIL_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}
_THUMB_MEMORY_CACHE: dict[tuple[str, int, float], Path] = {}


def _nearest_thumbnail_width(width: int) -> int:
    if width <= THUMBNAIL_WIDTHS[0]:
        return THUMBNAIL_WIDTHS[0]
    if width >= THUMBNAIL_WIDTHS[-1]:
        return THUMBNAIL_WIDTHS[-1]
    return min(THUMBNAIL_WIDTHS, key=lambda allowed: abs(allowed - width))


def _manga_media_path(rel_path: str) -> Path:
    rel = Path(rel_path)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise HTTPException(400, "Invalid image path")
    if rel.parts and rel.parts[0] == ".thumbs":
        raise HTTPException(400, "Invalid image path")
    source = (manga_dir / rel).resolve()
    try:
        source.relative_to(manga_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid image path")
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(415, "Unsupported image type")
    if not source.exists() or not source.is_file():
        raise HTTPException(404, "Image not found")
    return source


@app.get("/static/manga/_thumb/{rel_path:path}", include_in_schema=False)
def manga_thumbnail(rel_path: str, w: int = 720):
    source = _manga_media_path(rel_path)
    width = _nearest_thumbnail_width(w)
    rel = source.relative_to(manga_dir.resolve())
    cache_key = (rel.as_posix(), width, source.stat().st_mtime)
    cached = _THUMB_MEMORY_CACHE.get(cache_key)
    if cached and cached.exists():
        return FileResponse(cached, media_type="image/webp", headers=THUMBNAIL_CACHE_HEADERS)
    cache_path = thumb_dir / f"w{width}" / rel.parent / f"{rel.name}.webp"

    if cache_path.exists() and cache_path.stat().st_mtime >= source.stat().st_mtime:
        _THUMB_MEMORY_CACHE[cache_key] = cache_path
        return FileResponse(cache_path, media_type="image/webp", headers=THUMBNAIL_CACHE_HEADERS)

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)
            if img.width > width:
                ratio = width / img.width
                img = img.resize((width, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            img.save(tmp_path, format="WEBP", quality=THUMBNAIL_QUALITY, method=6)
        tmp_path.replace(cache_path)
    except UnidentifiedImageError:
        raise HTTPException(415, "Unsupported image type")
    except Exception as exc:
        logger.warning("Failed to create thumbnail for %s: %s", source, exc)
        if "tmp_path" in locals() and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        return FileResponse(source, headers={"Cache-Control": "public, max-age=86400"})

    _THUMB_MEMORY_CACHE[cache_key] = cache_path
    if len(_THUMB_MEMORY_CACHE) > 2048:
        _THUMB_MEMORY_CACHE.clear()
    return FileResponse(cache_path, media_type="image/webp", headers=THUMBNAIL_CACHE_HEADERS)


app.mount("/static/manga", StaticFiles(directory=str(manga_dir)), name="manga")


def _client_is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCAL_CLIENT_HOSTS


def _supplied_api_token(request: Request) -> str:
    supplied = request.headers.get("x-api-token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return supplied


@app.middleware("http")
async def request_context_and_security(request: Request, call_next):
    request_id = request_id_from(request)
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    status_code = 500
    try:
        if request.url.path.startswith("/api") and request.method != "OPTIONS":
            if API_TOKENS:
                if _supplied_api_token(request) not in API_TOKENS:
                    status_code = 401
                    return JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)
            elif settings.require_api_token_for_remote and not _client_is_local(request):
                status_code = 401
                return JSONResponse({"detail": "API token is required for non-local requests"}, status_code=401)
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        record_request(request.method, request.url.path, status_code)
        logger.info(
            "request finished method=%s path=%s status=%s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            status_code,
            (time.perf_counter() - started) * 1000,
        )
        request_id_var.reset(token)


def _require_chapter(chapter_id: int, db: Session) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.deleted_at is not None:
        raise HTTPException(404, "Chapter not found")
    return chapter


def _user_deepseek_api_key(request: Request) -> str | None:
    return request.headers.get("x-deepseek-api-key") or None


def _user_image_api_key(request: Request) -> str | None:
    return request.headers.get("x-image-api-key") or None


def _character_profile_text(body: dict) -> str:
    raw = body.get("characters", "")
    if not isinstance(raw, str):
        raise HTTPException(400, "characters must be a string")
    text = raw.strip()
    if len(text) > MAX_CHARACTER_PROFILE_CHARS:
        raise HTTPException(413, f"Character profile is too long. Max length is {MAX_CHARACTER_PROFILE_CHARS} characters")
    return text


def _unlink_file(path: Path, label: str) -> None:
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Failed to delete %s %s: %s", label, path, exc)
        raise HTTPException(409, f"{label} file is currently in use. Try again later")


def _rmtree_best_effort(path: Path, label: str) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("Failed to delete %s %s: %s", label, path, exc)


def _write_bytes_or_conflict(path: Path, data: bytes, label: str) -> None:
    try:
        path.write_bytes(data)
    except OSError as exc:
        logger.warning("Failed to write %s %s: %s", label, path, exc)
        raise HTTPException(409, f"{label} file is currently in use. Try again later")


def _backend_path(relative_path: str | None) -> Path | None:
    return backend_path(relative_path)


def _chapter_dir(chapter_id: int) -> Path:
    return chapter_dir(chapter_id)


def _clear_chapter_manga_state(chapter: Chapter, db: Session) -> None:
    if hasattr(chapter, "scenes_text"):
        chapter.scenes_text = None
    chapter_dir = _chapter_dir(chapter.id)
    if chapter_dir.exists():
        for filename in ("scenes.txt",):
            path = chapter_dir / filename
            if path.exists():
                _unlink_file(path, filename)
        for img in list(chapter.images):
            img_path = Path(__file__).resolve().parent / img.image_path
            if img_path.exists():
                _unlink_file(img_path, "manga image")
            db.delete(img)


def _decode_png_upload(b64: str) -> bytes:
    if not b64:
        raise HTTPException(400, "No image provided")
    if "," in b64 and b64.lstrip().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        import base64
        import binascii
        import io

        from PIL import Image, UnidentifiedImageError

        img_bytes = base64.b64decode(b64, validate=True)
        if len(img_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Image is too large. Max size is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        with Image.open(io.BytesIO(img_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(img_bytes)) as img:
            output = io.BytesIO()
            img.convert("RGBA").save(output, format="PNG", optimize=True)
            png_bytes = output.getvalue()
        if len(png_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Image is too large after processing. Max size is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        return png_bytes
    except HTTPException:
        raise
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Invalid base64 image data")
    except UnidentifiedImageError:
        raise HTTPException(400, "Uploaded file is not a valid image")


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"Request body is too large. Max size is {max_bytes // (1024 * 1024)}MB")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_image_upload(request: Request) -> str:
    # Base64 expands by roughly 4/3, so allow JSON body overhead without
    # changing the existing frontend request shape.
    body = await _read_limited_body(request, int(MAX_UPLOAD_BYTES * 1.5) + 4096)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid JSON upload body")
    image = payload.get("image", "")
    if not isinstance(image, str):
        raise HTTPException(400, "image must be a base64 string")
    return image


@app.on_event("startup")
def on_startup():
    if not API_TOKENS:
        logger.warning("API_TOKEN/API_TOKENS is not set; non-local /api requests will be rejected")
    init_db()
    # One-time: rename default stories
    from database import SessionLocal
    db = SessionLocal()
    try:
        for s in db.query(Story).filter(Story.title.in_(["我的第一个故事", "未命名故事"])).all():
            if s.chapters and any(ch.messages for ch in s.chapters):
                s.title = "转生成为暗恋公主的女仆故事"
                s.description = "百合女仆与公主的奇幻冒险"
            # else: leave as-is for empty stories
        db.commit()
    finally:
        db.close()


@app.on_event("shutdown")
async def on_shutdown():
    await close_image_client()


@app.get("/health")
def health():
    db_ok = False
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.warning("Health DB check failed: %s", exc)
    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "database": "ok" if db_ok else "error",
        "image_api": {
            "primary_configured": bool(settings.image_api_base_url),
            "fallback_configured": bool(settings.image_api_base_url_fallback),
        },
        "active_manga_jobs": len(ACTIVE_MANGA_GENERATIONS),
        "max_manga_jobs": settings.max_manga_generation_jobs,
    }


@app.get("/metrics")
def metrics():
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)


# ─── Story CRUD ─────────────────────────────────────────────

@app.post("/api/stories", response_model=StoryOut)
def create_story(body: StoryCreate, db: Session = Depends(get_db)):
    story = Story(title=body.title, description=body.description)
    db.add(story)
    db.flush()
    # Auto-create first chapter
    chapter = Chapter(story_id=story.id, chapter_number=1)
    db.add(chapter)
    db.commit()
    db.refresh(story)
    return story


@app.get("/api/stories", response_model=list[StoryOut])
def list_stories(db: Session = Depends(get_db)):
    return db.query(Story).filter(Story.deleted_at.is_(None)).order_by(Story.created_at.desc()).all()


@app.get("/api/stories/{story_id}", response_model=StoryOut)
def get_story(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story or story.deleted_at is not None:
        raise HTTPException(404, "Story not found")
    return story


@app.put("/api/stories/{story_id}", response_model=StoryOut)
def update_story(story_id: int, body: StoryUpdate, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    if body.title is not None:
        story.title = body.title
    if body.description is not None:
        story.description = body.description
    db.commit()
    db.refresh(story)
    return story


@app.delete("/api/stories/{story_id}")
def delete_story(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story or story.deleted_at is not None:
        raise HTTPException(404, "Story not found")
    now = datetime.datetime.now(datetime.timezone.utc)
    story.deleted_at = now
    for chapter in story.chapters:
        chapter.deleted_at = now
    db.commit()
    return {"ok": True}


@app.post("/api/stories/{story_id}/upload-cover")
async def upload_story_cover(story_id: int, request: Request, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    b64 = await _read_json_image_upload(request)
    img_bytes = _decode_png_upload(b64)
    covers_dir = COVERS_DIR
    covers_dir.mkdir(parents=True, exist_ok=True)
    filename = f"cover_{story_id}_{uuid.uuid4().hex[:8]}.png"
    _write_bytes_or_conflict(covers_dir / filename, img_bytes, "story cover")
    # Delete old cover file if exists
    if story.cover_image:
        old = Path(__file__).resolve().parent / story.cover_image
        try:
            _unlink_file(old, "old story cover")
        except HTTPException:
            logger.warning("Old story cover remained after replacement: %s", old)
    story.cover_image = f"manga_outputs/covers/{filename}"
    db.commit()
    db.refresh(story)
    return {"cover_image": story.cover_image}


# ─── Chapter CRUD ───────────────────────────────────────────

@app.get("/api/stories/{story_id}/chapters", response_model=list[ChapterOut])
def list_chapters(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story or story.deleted_at is not None:
        raise HTTPException(404, "Story not found")
    return (
        db.query(Chapter)
        .filter(Chapter.story_id == story_id, Chapter.deleted_at.is_(None))
        .order_by(Chapter.chapter_number)
        .all()
    )


@app.get("/api/chapters/{chapter_id}", response_model=ChapterOut)
def get_chapter(chapter_id: int, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.deleted_at is not None:
        raise HTTPException(404, "Chapter not found")
    return chapter


@app.post("/api/stories/{story_id}/chapters", response_model=ChapterOut)
def create_next_chapter(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")

    last_error: Exception | None = None
    for _attempt in range(3):
        max_num = (
            db.query(Chapter.chapter_number)
            .filter(Chapter.story_id == story_id)
            .order_by(Chapter.chapter_number.desc())
            .first()
        )
        next_num = (max_num[0] + 1) if max_num else 1
        chapter = Chapter(story_id=story_id, chapter_number=next_num)
        db.add(chapter)
        try:
            db.commit()
            db.refresh(chapter)
            return chapter
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            last_error = exc
            logger.warning("Retrying chapter creation after database conflict: %s", exc)

    raise HTTPException(409, f"Could not create next chapter due to database conflict: {last_error}")


@app.delete("/api/chapters/{chapter_id}")
def delete_chapter(chapter_id: int, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter or chapter.deleted_at is not None:
        raise HTTPException(404, "Chapter not found")
    chapter.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    return {"ok": True}


# ─── Chat (SSE streaming) ──────────────────────────────────

@app.post("/api/chapters/{chapter_id}/chat")
async def chat(chapter_id: int, body: ChatMessageIn, request: Request, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    if chapter.content_source == "import":
        raise HTTPException(409, "This chapter was imported from existing novel text and cannot use AI chat")

    # Save user message
    chapter.content_source = "chat"
    user_msg = ChatMessage(chapter_id=chapter_id, role="user", content=body.content)
    db.add(user_msg)
    db.commit()

    # Build message history including all previous chapters in same story
    all_chapters = (
        db.query(Chapter)
        .filter(Chapter.story_id == chapter.story_id, Chapter.chapter_number <= chapter.chapter_number)
        .order_by(Chapter.chapter_number)
        .all()
    )
    history = []
    for ch in all_chapters:
        for m in ch.messages:
            history.append({"role": m.role, "content": m.content})

    collected: list[str] = []

    def save_interrupted_assistant() -> None:
        full_content = "".join(collected).strip()
        if not full_content:
            return
        assistant_msg = ChatMessage(
            chapter_id=chapter_id,
            role="assistant",
            content=f"{full_content}\n\n[已中止]",
        )
        db.add(assistant_msg)
        db.commit()

    async def event_generator():
        try:
            async for token in chat_stream(history, api_key=_user_deepseek_api_key(request)):
                collected.append(token)
                yield {"event": "token", "data": json.dumps({"content": token}, ensure_ascii=False)}
            # Save assistant message
            full_content = "".join(collected)
            assistant_msg = ChatMessage(chapter_id=chapter_id, role="assistant", content=full_content)
            db.add(assistant_msg)
            db.commit()
            yield {"event": "done", "data": json.dumps({"content": full_content}, ensure_ascii=False)}
        except asyncio.CancelledError:
            db.rollback()
            save_interrupted_assistant()
            raise
        except Exception as e:
            db.rollback()
            persisted_user_msg = db.get(ChatMessage, user_msg.id)
            if persisted_user_msg:
                db.delete(persisted_user_msg)
                db.commit()
            yield {"event": "error", "data": json.dumps({"error": str(e)}, ensure_ascii=False)}

    return EventSourceResponse(event_generator())


# ─── Generate Novel ─────────────────────────────────────────

@app.post("/api/chapters/{chapter_id}/generate-novel", response_model=ChapterOut)
async def generate_novel_endpoint(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    if chapter.content_source == "import":
        raise HTTPException(409, "This chapter was imported from existing novel text and cannot generate AI novel text")

    history = [{"role": m.role, "content": m.content} for m in chapter.messages]
    if not history:
        raise HTTPException(400, "No chat history to generate novel from")

    novel_content = await generate_novel(history, api_key=_user_deepseek_api_key(request))
    chapter.novel_content = novel_content
    chapter.content_source = "chat"

    # Also save as assistant message
    msg = ChatMessage(chapter_id=chapter_id, role="assistant", content=novel_content)
    db.add(msg)
    db.commit()
    db.refresh(chapter)
    return chapter


MAX_IMPORTED_NOVEL_CHARS = settings.max_imported_novel_chars


@app.post("/api/chapters/{chapter_id}/import-novel", response_model=ChapterOut)
async def import_novel_endpoint(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    """Replace chapter chat history with a single user-imported novel text.

    The imported text is saved both as a single chat message (so the existing
    `generate-scenes` flow works unchanged) and as `chapter.novel_content`.
    """
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")

    body = await request.json()
    raw = body.get("content", "")
    if not isinstance(raw, str):
        raise HTTPException(400, "content must be a string")
    text = raw.strip()
    if not text:
        raise HTTPException(400, "content is empty")
    if len(text) > MAX_IMPORTED_NOVEL_CHARS:
        raise HTTPException(413, f"Novel is too long. Max length is {MAX_IMPORTED_NOVEL_CHARS} characters")
    if chapter.content_source == "chat" or chapter.messages:
        raise HTTPException(409, "This chapter already uses AI chat. Create a new chapter to import existing novel text.")
    if chapter.images:
        raise HTTPException(409, "This chapter already has manga images. Create a new chapter to import existing novel text.")

    _clear_chapter_manga_state(chapter, db)
    db.query(ChatMessage).filter(ChatMessage.chapter_id == chapter_id).delete()
    db.add(ChatMessage(chapter_id=chapter_id, role="user", content=text))
    chapter.novel_content = text
    chapter.content_source = "import"
    db.commit()
    db.refresh(chapter)
    return chapter


# ─── Character Profiles ───────────────────────────────────

def _characters_path(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "characters.txt"


def _asset_group_name(body: dict, fallback: str = "设定组") -> str:
    raw = body.get("name", fallback)
    if not isinstance(raw, str):
        raise HTTPException(400, "name must be a string")
    name = raw.strip() or fallback
    return name[:120]


def _selected_asset_group(chapter: Chapter | None) -> StoryAssetGroup | None:
    if not chapter or not chapter.asset_group_id:
        return None
    group = chapter.asset_group
    if group and group.story_id == chapter.story_id:
        return group
    return None


def _load_characters(chapter_id: int, db: Session | None = None) -> str:
    """Load character profiles: chapter override, legacy file, selected story group, then story default."""
    if db:
        chapter = db.get(Chapter, chapter_id)
        if chapter and chapter.character_profiles:
            text = chapter.character_profiles.strip()
            if text:
                return text
    p = _characters_path(chapter_id)
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        if text:
            return text
    if db:
        chapter = db.get(Chapter, chapter_id)
        group = _selected_asset_group(chapter)
        if group and group.character_profiles:
            text = group.character_profiles.strip()
            if text:
                return text
        if chapter and chapter.story and chapter.story.character_profiles:
            return chapter.story.character_profiles.strip()
    return ""


# Story-level character profiles
@app.get("/api/stories/{story_id}/characters")
async def get_story_characters(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    return {"characters": (story.character_profiles or "").strip()}


@app.put("/api/stories/{story_id}/characters")
async def save_story_characters(story_id: int, body: dict, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    story.character_profiles = _character_profile_text(body)
    db.commit()
    return {"ok": True}


# Chapter-level character profiles (with fallback info)
@app.get("/api/chapters/{chapter_id}/characters")
async def get_characters(chapter_id: int, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    own_text = (chapter.character_profiles or "").strip()
    if own_text:
        return {"characters": own_text, "source": "chapter"}
    p = _characters_path(chapter_id)
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        if text:
            return {"characters": text, "source": "chapter"}
    group = _selected_asset_group(chapter)
    group_text = (group.character_profiles or "").strip() if group else ""
    if group_text:
        return {"characters": group_text, "source": "asset_group", "group_id": group.id, "group_name": group.name}
    text = (chapter.story.character_profiles or "").strip() if chapter.story else ""
    return {"characters": text, "source": "story" if text else "none"}


@app.put("/api/chapters/{chapter_id}/characters")
async def save_characters(chapter_id: int, body: dict, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    text = _character_profile_text(body)
    chapter.character_profiles = text
    p = _characters_path(chapter_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    db.commit()
    return {"ok": True}


@app.delete("/api/chapters/{chapter_id}/characters")
async def reset_chapter_characters(chapter_id: int, db: Session = Depends(get_db)):
    """Delete chapter-level override so it falls back to story-level."""
    chapter = _require_chapter(chapter_id, db)
    p = _characters_path(chapter_id)
    if p.exists():
        _unlink_file(p, "character profile")
    chapter.character_profiles = None
    db.commit()
    return {"ok": True}


# ─── Reference Image (垫图，支持多图) ──────────────────────────

MAX_REF_IMAGES_PER_LEVEL = settings.max_ref_images_per_level


def _story_ref_dir(story_id: int) -> Path:
    return story_ref_dir(story_id)


def _chapter_ref_dir(chapter_id: int) -> Path:
    return chapter_ref_dir(chapter_id)


def _asset_group_ref_dir(group_id: int) -> Path:
    return asset_group_ref_dir(group_id)


def _legacy_story_ref_image(story_id: int) -> Path:
    """Old single-file location, kept for backward compat / lazy migration."""
    return legacy_story_ref_image(story_id)


def _legacy_chapter_ref_image(chapter_id: int) -> Path:
    return legacy_chapter_ref_image(chapter_id)


def _migrate_legacy_ref(legacy: Path, ref_dir: Path) -> None:
    """Move legacy single ref_image.png into ref_images/ as ref_legacy.png on demand."""
    if not legacy.exists():
        return
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
        target = ref_dir / "ref_legacy.png"
        if target.exists():
            target = ref_dir / f"ref_legacy_{uuid.uuid4().hex[:8]}.png"
        legacy.rename(target)
        logger.info("Migrated legacy ref image %s -> %s", legacy, target)
    except OSError as exc:
        logger.warning("Failed to migrate legacy ref image %s: %s", legacy, exc)


def _story_ref_image_db_path(story: Story) -> Path | None:
    if not story.ref_image:
        return None
    p = Path(__file__).resolve().parent / story.ref_image
    return p if p.exists() else None


def _chapter_ref_image_db_path(chapter: Chapter) -> Path | None:
    if not chapter.ref_image:
        return None
    p = Path(__file__).resolve().parent / chapter.ref_image
    return p if p.exists() else None


def _list_ref_files(ref_dir: Path) -> list[Path]:
    if not ref_dir.exists():
        return []
    return sorted([p for p in ref_dir.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}])


def _safe_ref_file(ref_dir: Path, filename: str) -> Path:
    if not SAFE_FILENAME_RE.fullmatch(filename) or Path(filename).name != filename:
        raise HTTPException(400, "invalid filename")
    target = (ref_dir / filename).resolve()
    base = ref_dir.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "invalid filename")
    return target


def _ref_static_path(ref_dir: Path, filename: str, kind: str, owner_id: int) -> str:
    """Build the static URL-relative path for a ref image."""
    if kind == "story":
        return f"manga_outputs/story_{owner_id}/ref_images/{filename}"
    if kind == "asset_group":
        return f"manga_outputs/asset_groups/group_{owner_id}/ref_images/{filename}"
    return f"manga_outputs/chapter_{owner_id}/ref_images/{filename}"


def _serialize_refs(ref_dir: Path, kind: str, owner_id: int) -> list[dict]:
    out = []
    for p in _list_ref_files(ref_dir):
        out.append({
            "filename": p.name,
            "image_path": _ref_static_path(ref_dir, p.name, kind, owner_id),
            "size_kb": round(p.stat().st_size / 1024),
        })
    return out


def _db_ref_entry(db_ref: Path | None, image_path: str | None) -> dict | None:
    if not db_ref or not image_path:
        return None
    return {
        "filename": Path(image_path).name,
        "image_path": image_path,
        "size_kb": round(db_ref.stat().st_size / 1024),
    }


def _serialize_refs_with_db_ref(ref_dir: Path, kind: str, owner_id: int, db_ref: Path | None, image_path: str | None) -> list[dict]:
    images = _serialize_refs(ref_dir, kind, owner_id)
    entry = _db_ref_entry(db_ref, image_path)
    if entry:
        images.insert(0, entry)
    return images


def _effective_ref_image_paths(chapter_id: int, db: Session) -> list[Path]:
    """Return effective refs: chapter override, selected story group, then default story refs."""
    chapter_dir = _chapter_ref_dir(chapter_id)
    _migrate_legacy_ref(_legacy_chapter_ref_image(chapter_id), chapter_dir)
    chapter_refs = _list_ref_files(chapter_dir)
    if chapter_refs:
        return chapter_refs
    chapter = db.get(Chapter, chapter_id)
    if chapter:
        cp = _chapter_ref_image_db_path(chapter)
        if cp:
            return [cp]
        group = _selected_asset_group(chapter)
        if group:
            group_refs = _list_ref_files(_asset_group_ref_dir(group.id))
            if group_refs:
                return group_refs
        story_dir = _story_ref_dir(chapter.story_id)
        _migrate_legacy_ref(_legacy_story_ref_image(chapter.story_id), story_dir)
        story_refs = _list_ref_files(story_dir)
        if story_refs:
            return story_refs
        if chapter.story:
            sp = _story_ref_image_db_path(chapter.story)
            if sp:
                return [sp]
    return []


def _save_uploaded_ref(ref_dir: Path, img_bytes: bytes, label: str, existing_count: int | None = None) -> str:
    count = len(_list_ref_files(ref_dir)) if existing_count is None else existing_count
    if count >= MAX_REF_IMAGES_PER_LEVEL:
        raise HTTPException(409, f"Maximum {MAX_REF_IMAGES_PER_LEVEL} reference images allowed")
    ref_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ref_{uuid.uuid4().hex[:8]}.png"
    _write_bytes_or_conflict(ref_dir / filename, img_bytes, label)
    return filename


def _default_asset_group_payload(story: Story) -> dict:
    ref_dir = _story_ref_dir(story.id)
    _migrate_legacy_ref(_legacy_story_ref_image(story.id), ref_dir)
    db_ref = _story_ref_image_db_path(story)
    refs = _serialize_refs_with_db_ref(ref_dir, "story", story.id, db_ref, story.ref_image)
    characters = (story.character_profiles or "").strip()
    return {
        "id": None,
        "name": "默认组",
        "is_default": True,
        "character_profiles": characters,
        "has_character_profiles": bool(characters),
        "ref_images": refs,
        "ref_count": len(refs),
    }


def _asset_group_payload(group: StoryAssetGroup) -> dict:
    refs = _serialize_refs(_asset_group_ref_dir(group.id), "asset_group", group.id)
    characters = (group.character_profiles or "").strip()
    return {
        "id": group.id,
        "name": group.name,
        "is_default": False,
        "character_profiles": characters,
        "has_character_profiles": bool(characters),
        "ref_images": refs,
        "ref_count": len(refs),
    }


def _story_asset_groups_payload(story: Story) -> list[dict]:
    groups = [_default_asset_group_payload(story)]
    groups.extend(_asset_group_payload(group) for group in story.asset_groups)
    return groups


def _require_asset_group(story_id: int, group_id: int, db: Session) -> StoryAssetGroup:
    group = db.get(StoryAssetGroup, group_id)
    if not group or group.story_id != story_id:
        raise HTTPException(404, "Asset group not found")
    return group


@app.get("/api/stories/{story_id}/asset-groups")
async def list_story_asset_groups(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    return {"groups": _story_asset_groups_payload(story), "max": MAX_REF_IMAGES_PER_LEVEL}


@app.post("/api/stories/{story_id}/asset-groups")
async def create_story_asset_group(story_id: int, body: dict, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    group = StoryAssetGroup(
        story_id=story_id,
        name=_asset_group_name(body, f"设定组 {len(story.asset_groups) + 1}"),
        character_profiles=_character_profile_text(body) if "characters" in body else "",
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    db.refresh(story)
    return {"group": _asset_group_payload(group), "groups": _story_asset_groups_payload(story)}


@app.put("/api/stories/{story_id}/asset-groups/{group_id}")
async def update_story_asset_group(story_id: int, group_id: int, body: dict, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    group = _require_asset_group(story_id, group_id, db)
    if "name" in body:
        group.name = _asset_group_name(body, group.name)
    if "characters" in body:
        group.character_profiles = _character_profile_text(body)
    db.commit()
    db.refresh(group)
    db.refresh(story)
    return {"group": _asset_group_payload(group), "groups": _story_asset_groups_payload(story)}


@app.delete("/api/stories/{story_id}/asset-groups/{group_id}")
async def delete_story_asset_group(story_id: int, group_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    group = _require_asset_group(story_id, group_id, db)
    db.query(Chapter).filter(Chapter.asset_group_id == group.id).update({Chapter.asset_group_id: None})
    db.delete(group)
    db.commit()
    _rmtree_best_effort(_asset_group_ref_dir(group_id).parent, "asset group ref images")
    return {"groups": _story_asset_groups_payload(story)}


@app.get("/api/stories/{story_id}/asset-groups/{group_id}/ref-images")
async def list_story_asset_group_refs(story_id: int, group_id: int, db: Session = Depends(get_db)):
    _require_asset_group(story_id, group_id, db)
    return {"images": _serialize_refs(_asset_group_ref_dir(group_id), "asset_group", group_id), "max": MAX_REF_IMAGES_PER_LEVEL}


@app.post("/api/stories/{story_id}/asset-groups/{group_id}/ref-images")
async def add_story_asset_group_ref(story_id: int, group_id: int, request: Request, db: Session = Depends(get_db)):
    _require_asset_group(story_id, group_id, db)
    img_bytes = _decode_png_upload(await _read_json_image_upload(request))
    ref_dir = _asset_group_ref_dir(group_id)
    _save_uploaded_ref(ref_dir, img_bytes, "asset group ref image")
    return {"images": _serialize_refs(ref_dir, "asset_group", group_id), "max": MAX_REF_IMAGES_PER_LEVEL}


@app.delete("/api/stories/{story_id}/asset-groups/{group_id}/ref-images/{filename}")
async def delete_story_asset_group_ref(story_id: int, group_id: int, filename: str, db: Session = Depends(get_db)):
    _require_asset_group(story_id, group_id, db)
    ref_dir = _asset_group_ref_dir(group_id)
    _unlink_file(_safe_ref_file(ref_dir, filename), "asset group ref image")
    return {"images": _serialize_refs(ref_dir, "asset_group", group_id), "max": MAX_REF_IMAGES_PER_LEVEL}


@app.get("/api/chapters/{chapter_id}/asset-group")
async def get_chapter_asset_group(chapter_id: int, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    return {
        "selected_group_id": chapter.asset_group_id,
        "groups": _story_asset_groups_payload(chapter.story),
        "max": MAX_REF_IMAGES_PER_LEVEL,
    }


@app.put("/api/chapters/{chapter_id}/asset-group")
async def set_chapter_asset_group(chapter_id: int, body: dict, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    raw_group_id = body.get("group_id")
    if raw_group_id in (None, "", 0, "0"):
        chapter.asset_group_id = None
    else:
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "group_id must be a number")
        _require_asset_group(chapter.story_id, group_id, db)
        chapter.asset_group_id = group_id
    db.commit()
    db.refresh(chapter)
    return {
        "selected_group_id": chapter.asset_group_id,
        "groups": _story_asset_groups_payload(chapter.story),
        "max": MAX_REF_IMAGES_PER_LEVEL,
    }


# ─── Story-level multi ref images ───────────────────────────

@app.get("/api/stories/{story_id}/ref-images")
async def list_story_ref_images(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    ref_dir = _story_ref_dir(story_id)
    _migrate_legacy_ref(_legacy_story_ref_image(story_id), ref_dir)
    db_ref = _story_ref_image_db_path(story)
    images = _serialize_refs_with_db_ref(ref_dir, "story", story_id, db_ref, story.ref_image)
    return {"images": images, "max": MAX_REF_IMAGES_PER_LEVEL}


@app.post("/api/stories/{story_id}/ref-images")
async def add_story_ref_image(story_id: int, request: Request, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    img_bytes = _decode_png_upload(await _read_json_image_upload(request))
    ref_dir = _story_ref_dir(story_id)
    _migrate_legacy_ref(_legacy_story_ref_image(story_id), ref_dir)
    db_ref = _story_ref_image_db_path(story)
    existing_count = len(_list_ref_files(ref_dir)) + (1 if db_ref else 0)
    _save_uploaded_ref(ref_dir, img_bytes, "story ref image", existing_count=existing_count)
    return {"images": _serialize_refs_with_db_ref(ref_dir, "story", story_id, db_ref, story.ref_image), "max": MAX_REF_IMAGES_PER_LEVEL}


@app.delete("/api/stories/{story_id}/ref-images/{filename}")
async def delete_story_ref_image(story_id: int, filename: str, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")
    ref_dir = _story_ref_dir(story_id)
    if story.ref_image and Path(story.ref_image).name == filename:
        p = _story_ref_image_db_path(story)
        if p and p.exists():
            _unlink_file(p, "story ref image")
        story.ref_image = None
        db.commit()
        return {"images": _serialize_refs(ref_dir, "story", story_id), "max": MAX_REF_IMAGES_PER_LEVEL}
    _unlink_file(_safe_ref_file(ref_dir, filename), "story ref image")
    return {
        "images": _serialize_refs_with_db_ref(_story_ref_dir(story_id), "story", story_id, _story_ref_image_db_path(story), story.ref_image),
        "max": MAX_REF_IMAGES_PER_LEVEL,
    }


# ─── Chapter-level multi ref images (with story fallback) ───

@app.get("/api/chapters/{chapter_id}/ref-images")
async def list_chapter_ref_images(chapter_id: int, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    ref_dir = _chapter_ref_dir(chapter_id)
    _migrate_legacy_ref(_legacy_chapter_ref_image(chapter_id), ref_dir)
    cp = _chapter_ref_image_db_path(chapter)
    chapter_refs = _serialize_refs_with_db_ref(ref_dir, "chapter", chapter_id, cp, chapter.ref_image)
    if chapter_refs:
        return {"images": chapter_refs, "source": "chapter", "max": MAX_REF_IMAGES_PER_LEVEL}
    group = _selected_asset_group(chapter)
    if group:
        group_refs = _serialize_refs(_asset_group_ref_dir(group.id), "asset_group", group.id)
        if group_refs:
            return {
                "images": group_refs,
                "source": "asset_group",
                "group_id": group.id,
                "group_name": group.name,
                "max": MAX_REF_IMAGES_PER_LEVEL,
            }
    story_dir = _story_ref_dir(chapter.story_id)
    _migrate_legacy_ref(_legacy_story_ref_image(chapter.story_id), story_dir)
    sp = _story_ref_image_db_path(chapter.story) if chapter.story else None
    story_refs = _serialize_refs_with_db_ref(
        story_dir,
        "story",
        chapter.story_id,
        sp,
        chapter.story.ref_image if chapter.story else None,
    )
    if story_refs:
        return {"images": story_refs, "source": "story", "max": MAX_REF_IMAGES_PER_LEVEL}
    return {"images": [], "source": "none", "max": MAX_REF_IMAGES_PER_LEVEL}


@app.post("/api/chapters/{chapter_id}/ref-images")
async def add_chapter_ref_image(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    img_bytes = _decode_png_upload(await _read_json_image_upload(request))
    ref_dir = _chapter_ref_dir(chapter_id)
    _migrate_legacy_ref(_legacy_chapter_ref_image(chapter_id), ref_dir)
    cp = _chapter_ref_image_db_path(chapter)
    existing_count = len(_list_ref_files(ref_dir)) + (1 if cp else 0)
    _save_uploaded_ref(ref_dir, img_bytes, "chapter ref image", existing_count=existing_count)
    return {
        "images": _serialize_refs_with_db_ref(ref_dir, "chapter", chapter_id, cp, chapter.ref_image),
        "source": "chapter",
        "max": MAX_REF_IMAGES_PER_LEVEL,
    }


@app.delete("/api/chapters/{chapter_id}/ref-images/{filename}")
async def delete_chapter_ref_image(chapter_id: int, filename: str, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    ref_dir = _chapter_ref_dir(chapter_id)
    if chapter.ref_image and Path(chapter.ref_image).name == filename:
        p = _chapter_ref_image_db_path(chapter)
        if p and p.exists():
            _unlink_file(p, "chapter ref image")
        chapter.ref_image = None
        db.commit()
        return {"images": _serialize_refs(_chapter_ref_dir(chapter_id), "chapter", chapter_id), "source": "chapter", "max": MAX_REF_IMAGES_PER_LEVEL}
    _unlink_file(_safe_ref_file(ref_dir, filename), "chapter ref image")
    return {
        "images": _serialize_refs_with_db_ref(_chapter_ref_dir(chapter_id), "chapter", chapter_id, _chapter_ref_image_db_path(chapter), chapter.ref_image),
        "source": "chapter",
        "max": MAX_REF_IMAGES_PER_LEVEL,
    }


# --- Whole-story import / export -------------------------------------------------

EXPORT_FORMAT = "lorevista.story.export"
EXPORT_VERSION = 2
ALLOWED_IMPORT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _safe_zip_name(name: str) -> str:
    clean = name.replace("\\", "/").lstrip("/")
    if not clean or clean.startswith("../") or "/../" in clean or clean == "..":
        raise HTTPException(400, "Invalid export asset path")
    return clean


def _zip_add_path(zf: zipfile.ZipFile, added: set[str], zip_name: str, path: Path | None) -> str | None:
    if not path or not path.exists() or not path.is_file():
        return None
    zip_name = _safe_zip_name(zip_name)
    if zip_name not in added:
        zf.write(path, zip_name)
        added.add(zip_name)
    return zip_name


def _story_ref_paths_for_export(story: Story) -> list[Path]:
    ref_dir = _story_ref_dir(story.id)
    _migrate_legacy_ref(_legacy_story_ref_image(story.id), ref_dir)
    paths = _list_ref_files(ref_dir)
    db_ref = _story_ref_image_db_path(story)
    if db_ref and db_ref not in paths:
        paths.insert(0, db_ref)
    return paths


def _chapter_ref_paths_for_export(chapter: Chapter) -> list[Path]:
    ref_dir = _chapter_ref_dir(chapter.id)
    _migrate_legacy_ref(_legacy_chapter_ref_image(chapter.id), ref_dir)
    paths = _list_ref_files(ref_dir)
    db_ref = _chapter_ref_image_db_path(chapter)
    if db_ref and db_ref not in paths:
        paths.insert(0, db_ref)
    return paths


def _asset_group_ref_paths_for_export(group: StoryAssetGroup) -> list[Path]:
    return _list_ref_files(_asset_group_ref_dir(group.id))


@app.get("/api/stories/{story_id}/export")
def export_story(story_id: int, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "Story not found")

    buf = io.BytesIO()
    manifest: dict = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "story": {
            "title": story.title,
            "description": story.description or "",
            "character_profiles": story.character_profiles or "",
            "created_at": _iso(story.created_at),
            "cover_image": None,
            "ref_images": [],
            "asset_groups": [],
            "chapters": [],
        },
    }

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        added: set[str] = set()
        asset_group_keys: dict[int, str] = {}
        cover_zip = _zip_add_path(
            zf,
            added,
            f"assets/cover{Path(story.cover_image or '').suffix or '.png'}",
            _backend_path(story.cover_image),
        )
        manifest["story"]["cover_image"] = cover_zip

        for idx, path in enumerate(_story_ref_paths_for_export(story), start=1):
            suffix = path.suffix.lower() or ".png"
            asset = _zip_add_path(zf, added, f"assets/story_ref_images/{idx:03d}{suffix}", path)
            if asset:
                manifest["story"]["ref_images"].append(asset)

        for group_idx, group in enumerate(story.asset_groups, start=1):
            group_key = f"group_{group_idx:03d}"
            asset_group_keys[group.id] = group_key
            group_manifest = {
                "key": group_key,
                "name": group.name,
                "character_profiles": group.character_profiles or "",
                "ref_images": [],
            }
            for ref_idx, path in enumerate(_asset_group_ref_paths_for_export(group), start=1):
                suffix = path.suffix.lower() or ".png"
                asset = _zip_add_path(zf, added, f"assets/asset_groups/{group_key}/ref_images/{ref_idx:03d}{suffix}", path)
                if asset:
                    group_manifest["ref_images"].append(asset)
            manifest["story"]["asset_groups"].append(group_manifest)

        for chapter in story.chapters:
            chapter_scenes = _load_chapter_scenes(chapter)
            chapter_manifest = {
                "chapter_number": chapter.chapter_number,
                "novel_content": chapter.novel_content or "",
                "content_source": chapter.content_source,
                "scenes_text": _serialize_scenes(chapter_scenes) if chapter_scenes else "",
                "character_profiles": chapter.character_profiles or "",
                "asset_group_key": asset_group_keys.get(chapter.asset_group_id) if chapter.asset_group_id else None,
                "color_mode": _load_color_mode(chapter.id, db),
                "image_count": _load_image_count(chapter.id, db),
                "created_at": _iso(chapter.created_at),
                "messages": [
                    {"role": msg.role, "content": msg.content, "created_at": _iso(msg.created_at)}
                    for msg in chapter.messages
                ],
                "ref_images": [],
                "images": [],
            }
            chapter_prefix = f"assets/chapters/{chapter.chapter_number:03d}"

            for idx, path in enumerate(_chapter_ref_paths_for_export(chapter), start=1):
                suffix = path.suffix.lower() or ".png"
                asset = _zip_add_path(zf, added, f"{chapter_prefix}/ref_images/{idx:03d}{suffix}", path)
                if asset:
                    chapter_manifest["ref_images"].append(asset)

            for img in chapter.images:
                path = _backend_path(img.image_path)
                suffix = path.suffix.lower() if path else ".png"
                asset = _zip_add_path(zf, added, f"{chapter_prefix}/images/{img.image_number:03d}{suffix}", path)
                chapter_manifest["images"].append({
                    "image_number": img.image_number,
                    "image_path": asset,
                    "prompt": img.prompt or "",
                    "created_at": _iso(img.created_at),
                })

            manifest["story"]["chapters"].append(chapter_manifest)

        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    buf.seek(0)
    safe_title = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in story.title).strip("_") or f"story_{story.id}"
    download_name = f"{safe_title}_ai-cartoon.zip"
    headers = {
        "Content-Disposition": (
            f'attachment; filename="story_{story.id}_ai-cartoon.zip"; '
            f"filename*=UTF-8''{quote(download_name)}"
        )
    }
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


def _require_zip_member(zf: zipfile.ZipFile, name: str | None) -> str | None:
    if not name:
        return None
    clean = _safe_zip_name(name)
    if clean not in zf.namelist():
        raise HTTPException(400, f"Missing asset in import package: {clean}")
    info = zf.getinfo(clean)
    if info.is_dir():
        raise HTTPException(400, f"Asset is a directory: {clean}")
    if Path(clean).suffix.lower() not in ALLOWED_IMPORT_SUFFIXES:
        raise HTTPException(400, f"Unsupported image type in import package: {clean}")
    return clean


def _validate_import_zip(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > settings.max_import_zip_files:
        raise HTTPException(413, f"Import package contains too many files. Max is {settings.max_import_zip_files}")
    total_uncompressed = 0
    for info in infos:
        clean = _safe_zip_name(info.filename)
        if info.is_dir():
            continue
        total_uncompressed += info.file_size
        if info.file_size > settings.max_import_zip_file_bytes and Path(clean).name != "manifest.json":
            raise HTTPException(
                413,
                f"Import package file is too large: {clean}. Max is {settings.max_import_zip_file_bytes // (1024 * 1024)}MB",
            )
        compressed = max(info.compress_size, 1)
        if info.file_size / compressed > settings.max_import_zip_ratio:
            raise HTTPException(400, f"Import package has suspicious compression ratio: {clean}")
    if total_uncompressed > MAX_IMPORT_ZIP_BYTES:
        raise HTTPException(413, f"Import package expands beyond {MAX_IMPORT_ZIP_BYTES // (1024 * 1024)}MB")
    logger.info("Import story package expanded size: %.1f MB", total_uncompressed / (1024 * 1024))


def _normalize_import_manifest(manifest: dict) -> dict:
    version = int(manifest.get("version", 1))
    if version > EXPORT_VERSION:
        raise HTTPException(400, "Import package was created by a newer app version")
    if version == EXPORT_VERSION:
        return manifest
    if version == 1:
        story_data = manifest.get("story")
        if isinstance(story_data, dict):
            story_data.setdefault("ref_images", [])
            story_data.setdefault("asset_groups", [])
            for chapter in story_data.get("chapters", []) or []:
                if isinstance(chapter, dict):
                    chapter.setdefault("ref_images", [])
                    chapter.setdefault("asset_group_key", None)
                    chapter.setdefault("color_mode", "bw")
                    chapter.setdefault("image_count", DEFAULT_IMAGE_COUNT)
            manifest["version"] = EXPORT_VERSION
            logger.info("Adapted import manifest from v1 to v%s", EXPORT_VERSION)
            return manifest
    raise HTTPException(400, f"Unsupported import package version: {version}")


def _copy_zip_asset(zf: zipfile.ZipFile, member: str | None, target: Path, label: str, written: list[Path]) -> bool:
    member = _require_zip_member(zf, member)
    if not member:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(zf.read(member))
    except OSError as exc:
        logger.warning("Failed to import %s %s: %s", label, target, exc)
        raise HTTPException(409, f"{label} file is currently in use. Try again later")
    written.append(target)
    return True


@app.post("/api/stories/import", response_model=StoryOut)
async def import_story(request: Request, db: Session = Depends(get_db)):
    body = await _read_limited_body(request, MAX_IMPORT_ZIP_BYTES)
    if not body:
        raise HTTPException(400, "Import package is empty")
    if len(body) > MAX_IMPORT_ZIP_BYTES:
        raise HTTPException(413, f"Import package is too large. Max size is {MAX_IMPORT_ZIP_BYTES // (1024 * 1024)}MB")
    logger.info("Import story package received: %.1f MB", len(body) / (1024 * 1024))

    written: list[Path] = []
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            if "manifest.json" not in zf.namelist():
                raise HTTPException(400, "Import package is missing manifest.json")
            _validate_import_zip(zf)

            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HTTPException(400, "Invalid manifest.json")

            if manifest.get("format") != EXPORT_FORMAT:
                raise HTTPException(400, "Unsupported import package format")
            manifest = _normalize_import_manifest(manifest)

            story_data = manifest.get("story")
            if not isinstance(story_data, dict):
                raise HTTPException(400, "Invalid story data in manifest")
            chapters_data = story_data.get("chapters", [])
            if not isinstance(chapters_data, list):
                raise HTTPException(400, "Invalid chapters data in manifest")
            chapter_numbers = [int(ch.get("chapter_number", 0)) for ch in chapters_data if isinstance(ch, dict)]
            if len(chapter_numbers) != len(set(chapter_numbers)):
                raise HTTPException(400, "Import package contains duplicate chapter numbers")

            story = Story(
                title=str(story_data.get("title") or "Imported Story"),
                description=str(story_data.get("description") or ""),
                character_profiles=str(story_data.get("character_profiles") or ""),
            )
            db.add(story)
            db.flush()
            logger.info("Importing story id=%s title=%r with %s chapters", story.id, story.title, len(chapters_data))

            cover_member = _require_zip_member(zf, story_data.get("cover_image"))
            if cover_member:
                suffix = Path(cover_member).suffix.lower() or ".png"
                filename = f"cover_{story.id}_{uuid.uuid4().hex[:8]}{suffix}"
                target = manga_dir / "covers" / filename
                _copy_zip_asset(zf, cover_member, target, "story cover", written)
                story.cover_image = f"manga_outputs/covers/{filename}"

            for ref_member in story_data.get("ref_images", []) or []:
                member = _require_zip_member(zf, ref_member)
                if not member:
                    continue
                suffix = Path(member).suffix.lower() or ".png"
                filename = f"ref_{uuid.uuid4().hex[:8]}{suffix}"
                _copy_zip_asset(zf, member, _story_ref_dir(story.id) / filename, "story ref image", written)

            group_key_to_id: dict[str, int] = {}
            for group_idx, group_data in enumerate(story_data.get("asset_groups", []) or [], start=1):
                if not isinstance(group_data, dict):
                    continue
                group_key = str(group_data.get("key") or f"group_{group_idx:03d}")
                group = StoryAssetGroup(
                    story_id=story.id,
                    name=str(group_data.get("name") or f"设定组 {group_idx}")[:120],
                    character_profiles=str(group_data.get("character_profiles") or ""),
                )
                db.add(group)
                db.flush()
                group_key_to_id[group_key] = group.id
                for ref_member in group_data.get("ref_images", []) or []:
                    member = _require_zip_member(zf, ref_member)
                    if not member:
                        continue
                    suffix = Path(member).suffix.lower() or ".png"
                    filename = f"ref_{uuid.uuid4().hex[:8]}{suffix}"
                    _copy_zip_asset(zf, member, _asset_group_ref_dir(group.id) / filename, "asset group ref image", written)

            for chapter_data in sorted(chapters_data, key=lambda ch: int(ch.get("chapter_number", 0))):
                if not isinstance(chapter_data, dict):
                    raise HTTPException(400, "Invalid chapter entry in manifest")
                chapter_number = int(chapter_data.get("chapter_number") or 0)
                if chapter_number <= 0:
                    raise HTTPException(400, "Chapter numbers must be positive")
                logger.info(
                    "Importing story id=%s chapter=%s images=%s",
                    story.id,
                    chapter_number,
                    len(chapter_data.get("images", []) or []),
                )

                chapter = Chapter(
                    story_id=story.id,
                    chapter_number=chapter_number,
                    novel_content=str(chapter_data.get("novel_content") or ""),
                    content_source=chapter_data.get("content_source"),
                    scenes_text=str(chapter_data.get("scenes_text") or ""),
                    character_profiles=str(chapter_data.get("character_profiles") or "") or None,
                    asset_group_id=group_key_to_id.get(str(chapter_data.get("asset_group_key") or "")) or None,
                    color_mode=chapter_data.get("color_mode") if chapter_data.get("color_mode") in ("bw", "color") else None,
                    image_count=chapter_data.get("image_count") if chapter_data.get("image_count") in ALLOWED_IMAGE_COUNTS else None,
                )
                db.add(chapter)
                db.flush()

                for msg_data in chapter_data.get("messages", []) or []:
                    if not isinstance(msg_data, dict):
                        continue
                    role = str(msg_data.get("role") or "")
                    content = str(msg_data.get("content") or "")
                    if role in ("user", "assistant", "system") and content:
                        db.add(ChatMessage(chapter_id=chapter.id, role=role, content=content))

                for ref_member in chapter_data.get("ref_images", []) or []:
                    member = _require_zip_member(zf, ref_member)
                    if not member:
                        continue
                    suffix = Path(member).suffix.lower() or ".png"
                    filename = f"ref_{uuid.uuid4().hex[:8]}{suffix}"
                    _copy_zip_asset(zf, member, _chapter_ref_dir(chapter.id) / filename, "chapter ref image", written)

                for img_data in sorted(chapter_data.get("images", []) or [], key=lambda img: int(img.get("image_number", 0))):
                    if not isinstance(img_data, dict):
                        continue
                    image_number = int(img_data.get("image_number") or 0)
                    if image_number <= 0:
                        continue
                    member = _require_zip_member(zf, img_data.get("image_path"))
                    if not member:
                        continue
                    suffix = Path(member).suffix.lower() or ".png"
                    filename = f"panel_{image_number:02d}_{uuid.uuid4().hex[:8]}{suffix}"
                    _copy_zip_asset(zf, member, _chapter_dir(chapter.id) / filename, "manga image", written)
                    db.add(MangaImage(
                        chapter_id=chapter.id,
                        image_number=image_number,
                        image_path=f"manga_outputs/chapter_{chapter.id}/{filename}",
                        prompt=str(img_data.get("prompt") or ""),
                    ))

            if not chapters_data:
                db.add(Chapter(story_id=story.id, chapter_number=1))

            logger.info("Committing imported story id=%s", story.id)
            db.commit()
            db.refresh(story)
            logger.info("Imported story id=%s title=%r", story.id, story.title)
            return story
    except zipfile.BadZipFile:
        db.rollback()
        logger.warning("Import story package rejected: invalid zip")
        raise HTTPException(400, "Import package is not a valid zip file")
    except HTTPException:
        db.rollback()
        for path in written:
            with contextlib.suppress(OSError):
                if path.exists():
                    path.unlink()
        raise
    except Exception:
        db.rollback()
        for path in written:
            with contextlib.suppress(OSError):
                if path.exists():
                    path.unlink()
        logger.exception("Failed to import story package")
        raise


# ─── Color Mode ─────────────────────────────────────────────


def _color_mode_path(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "color_mode.txt"


def _load_color_mode(chapter_id: int, db: Session | None = None) -> str:
    if db:
        chapter = db.get(Chapter, chapter_id)
        if chapter and chapter.color_mode in ("bw", "color"):
            return chapter.color_mode
    p = _color_mode_path(chapter_id)
    if p.exists():
        mode = p.read_text(encoding="utf-8").strip()
        if mode in ("bw", "color"):
            return mode
    return "bw"


def _save_color_mode(chapter_id: int, mode: str):
    p = _color_mode_path(chapter_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(mode, encoding="utf-8")


@app.get("/api/chapters/{chapter_id}/color-mode")
async def get_color_mode(chapter_id: int, db: Session = Depends(get_db)):
    _require_chapter(chapter_id, db)
    return {"color_mode": _load_color_mode(chapter_id, db)}


@app.put("/api/chapters/{chapter_id}/color-mode")
async def set_color_mode(chapter_id: int, body: dict, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    mode = body.get("color_mode", "bw")
    if mode not in ("bw", "color"):
        raise HTTPException(400, "color_mode must be 'bw' or 'color'")
    chapter.color_mode = mode
    _save_color_mode(chapter_id, mode)
    db.commit()
    return {"ok": True}


# ─── Image Count ───────────────────────────────────────────

DEFAULT_IMAGE_COUNT = 10
ALLOWED_IMAGE_COUNTS = [4, 6, 8, 10, 12, 15, 20]


def _image_count_path(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "image_count.txt"


def _load_image_count(chapter_id: int, db: Session | None = None) -> int:
    if db:
        chapter = db.get(Chapter, chapter_id)
        if chapter and chapter.image_count in ALLOWED_IMAGE_COUNTS:
            return int(chapter.image_count)
    p = _image_count_path(chapter_id)
    if p.exists():
        try:
            count = int(p.read_text(encoding="utf-8").strip())
            if count in ALLOWED_IMAGE_COUNTS:
                return count
        except (ValueError, OSError):
            pass
    return DEFAULT_IMAGE_COUNT


def _scenes_path(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "scenes.txt"


def _serialize_scenes(scenes: list[str]) -> str:
    return "\n\n".join(f"=== 第{idx}格 ===\n{s}" for idx, s in enumerate(scenes, 1)).strip() + "\n"


def _parse_scenes_text(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    import re
    parts = re.split(r"=== 第\d+格 ===\n", raw)
    return [p.strip() for p in parts if p.strip()]


def _load_chapter_scenes(chapter: Chapter) -> list[str]:
    scenes = _parse_scenes_text(chapter.scenes_text)
    if scenes:
        return scenes
    scenes_file = _scenes_path(chapter.id)
    if scenes_file.exists():
        try:
            return _parse_scenes_text(scenes_file.read_text(encoding="utf-8"))
        except OSError:
            return []
    return []


def _save_chapter_scenes(chapter: Chapter, scenes: list[str]) -> None:
    raw = _serialize_scenes(scenes)
    chapter.scenes_text = raw
    prompts_file = _scenes_path(chapter.id)
    prompts_file.parent.mkdir(parents=True, exist_ok=True)
    prompts_file.write_text(raw, encoding="utf-8")


@app.get("/api/chapters/{chapter_id}/image-count")
async def get_image_count(chapter_id: int, db: Session = Depends(get_db)):
    _require_chapter(chapter_id, db)
    return {"image_count": _load_image_count(chapter_id, db)}


@app.put("/api/chapters/{chapter_id}/image-count")
async def set_image_count(chapter_id: int, body: dict, db: Session = Depends(get_db)):
    chapter = _require_chapter(chapter_id, db)
    if chapter.images or _load_chapter_scenes(chapter):
        raise HTTPException(409, "Cannot change image count after scenes or images have been created")
    count = body.get("image_count", DEFAULT_IMAGE_COUNT)
    if count not in ALLOWED_IMAGE_COUNTS:
        raise HTTPException(400, f"image_count must be one of {ALLOWED_IMAGE_COUNTS}")
    chapter.image_count = count
    p = _image_count_path(chapter_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(count), encoding="utf-8")
    db.commit()
    return {"ok": True}


# ─── Scene generation & management ───────────────────────

@app.post("/api/chapters/{chapter_id}/generate-scenes")
async def generate_scenes_endpoint(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    """Generate scene prompts from chat history. Returns them for user review."""
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    if not chapter.messages:
        raise HTTPException(400, "No chat messages yet")

    image_count = _load_image_count(chapter_id, db)
    chat_history = [{"role": m.role, "content": m.content} for m in chapter.messages]
    split_task = asyncio.create_task(
        split_scenes(
            chat_history,
            character_profiles=_load_characters(chapter_id, db),
            page_count=image_count,
            api_key=_user_deepseek_api_key(request),
        )
    )
    try:
        while not split_task.done():
            if await request.is_disconnected():
                split_task.cancel()
                raise HTTPException(499, "Scene generation was cancelled")
            await asyncio.sleep(0.25)
        scenes = await split_task
        if await request.is_disconnected():
            raise HTTPException(499, "Scene generation was cancelled")
    except ValueError as exc:
        logger.warning("Failed to parse scene split response for chapter %s: %s", chapter_id, exc)
        raise HTTPException(502, "AI returned invalid scene JSON. Please retry generating scenes.")
    except asyncio.CancelledError:
        raise HTTPException(499, "Scene generation was cancelled")
    finally:
        if not split_task.done():
            split_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await split_task

    _save_chapter_scenes(chapter, scenes)
    db.commit()

    return {"scenes": scenes}


@app.get("/api/chapters/{chapter_id}/scenes")
async def get_scenes_endpoint(chapter_id: int, db: Session = Depends(get_db)):
    """Load saved scene prompts from the database, with legacy file fallback."""
    chapter = _require_chapter(chapter_id, db)
    return {"scenes": _load_chapter_scenes(chapter)}


@app.put("/api/chapters/{chapter_id}/scenes")
async def update_scenes_endpoint(chapter_id: int, body: dict, db: Session = Depends(get_db)):
    """Save user-edited scene prompts."""
    chapter = _require_chapter(chapter_id, db)
    scenes = body.get("scenes", [])
    image_count = _load_image_count(chapter_id, db)
    if not isinstance(scenes, list) or len(scenes) != image_count or not all(isinstance(s, str) and s.strip() for s in scenes):
        raise HTTPException(400, f"Must provide exactly {image_count} scenes")

    _save_chapter_scenes(chapter, scenes)
    db.commit()

    return {"ok": True}


# ─── SSE for manga image generation ─────────────────────

class MangaGenerationJob:
    def __init__(self, chapter_id: int, total: int):
        self.chapter_id = chapter_id
        self.total = total
        self.active = True
        self.events: list[dict[str, str]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.orphan_cancel_task: asyncio.Task | None = None
        self.character_profiles = ""
        self.ref_image_paths: list[str] = []
        self.color_mode = "bw"

    async def publish(self, event: str, data: dict):
        payload = {"event": event, "data": json.dumps(data, ensure_ascii=False)}
        self.events.append(payload)
        if len(self.events) > settings.job_event_history_limit:
            self.events = self.events[-settings.job_event_history_limit:]
        for queue in list(self.subscribers):
            try:
                await asyncio.wait_for(queue.put(payload), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("Dropping slow SSE subscriber for chapter %s", self.chapter_id)
                self.subscribers.discard(queue)

    def cancel_orphaned_later(self, delay: float = 15.0) -> None:
        if self.orphan_cancel_task and not self.orphan_cancel_task.done():
            return

        async def _cancel_if_still_orphaned():
            await asyncio.sleep(delay)
            if self.active and not self.subscribers and self.task and not self.task.done():
                logger.warning("Cancelling orphaned manga generation job chapter=%s", self.chapter_id)
                self.task.cancel()

        self.orphan_cancel_task = asyncio.create_task(_cancel_if_still_orphaned())

    def mark_subscribed(self) -> None:
        if self.orphan_cancel_task and not self.orphan_cancel_task.done():
            self.orphan_cancel_task.cancel()


async def _run_manga_generation_job(job: MangaGenerationJob, chapter_id: int, image_count: int, scenes: list[str], api_key: str | None):
    db = SessionLocal()
    job_status = "error"
    try:
        async with MANGA_GENERATION_SEMAPHORE:
            with manga_job_timer():
                await job.publish("scenes", {"scenes": scenes})
                await job.publish("status", {"message": "漫画生成任务已进入队列"})
                chapter = db.get(Chapter, chapter_id)
                if not chapter:
                    await job.publish("error", {"error": "Chapter not found"})
                    return

                ref_imgs = _effective_ref_image_paths(chapter_id, db)
                job.ref_image_paths = [str(p) for p in ref_imgs]
                job.character_profiles = _load_characters(chapter_id, db)
                job.color_mode = _load_color_mode(chapter_id, db)

                existing_images = {
                    img.image_number: img
                    for img in db.query(MangaImage).filter(MangaImage.chapter_id == chapter_id).all()
                }

                await job.publish("status", {
                    "message": "漫画生成配置已加载",
                    "ref_count": len(job.ref_image_paths),
                    "color_mode": job.color_mode,
                })

                for i, scene_prompt in enumerate(scenes, start=1):
                    if i in existing_images:
                        img = existing_images[i]
                        await job.publish("image", {
                            "id": img.id,
                            "image_number": i,
                            "image_path": img.image_path,
                            "prompt": img.prompt or scene_prompt,
                        })
                        continue

                    await job.publish("progress", {"current": i, "total": image_count, "prompt": scene_prompt})

                    async def image_progress(event: str, data: dict, image_number: int = i) -> None:
                        await job.publish(event, {"image_number": image_number, **data})

                    try:
                        image_path = await generate_manga_image(
                            scene_prompt,
                            chapter_id,
                            i,
                            all_scenes=scenes,
                            character_profiles=job.character_profiles,
                            ref_image_paths=job.ref_image_paths or None,
                            color_mode=job.color_mode,
                            api_key=api_key,
                            on_progress=image_progress,
                        )
                    except Exception as img_err:
                        await job.publish("error", {"error": f"第 {i} 张生成失败: {img_err}"})
                        return

                    manga = MangaImage(
                        chapter_id=chapter_id,
                        image_number=i,
                        image_path=image_path,
                        prompt=scene_prompt,
                    )
                    db.add(manga)
                    db.commit()
                    db.refresh(manga)
                    await job.publish("image", {
                        "id": manga.id,
                        "image_number": i,
                        "image_path": image_path,
                        "prompt": scene_prompt,
                    })

                job_status = "success"
                await job.publish("done", {"message": "漫画生成完成！"})
    except asyncio.CancelledError:
        job_status = "cancelled"
        await job.publish("error", {"error": "漫画生成已取消"})
        raise
    except Exception as exc:
        logger.exception("Manga generation job failed for chapter %s", chapter_id)
        await job.publish("error", {"error": str(exc)})
    finally:
        record_manga_job(job_status)
        db.close()
        job.active = False
        ACTIVE_MANGA_GENERATIONS.discard(chapter_id)


async def _stream_manga_generation_job(job: MangaGenerationJob):
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.sse_queue_size)
    for payload in job.events:
        yield payload
    if not job.active:
        return

    job.subscribers.add(queue)
    job.mark_subscribed()
    try:
        while True:
            payload = await queue.get()
            yield payload
            if payload.get("event") in {"done", "error"}:
                return
    finally:
        job.subscribers.discard(queue)
        if not job.subscribers:
            job.cancel_orphaned_later()


@app.post("/api/chapters/{chapter_id}/generate-manga-stream")
async def generate_manga_stream(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")

    image_count = _load_image_count(chapter_id, db)
    scenes = _load_chapter_scenes(chapter)
    if not scenes:
        raise HTTPException(400, "No scene prompts found. Generate scenes first.")
    if len(scenes) != image_count:
        raise HTTPException(400, f"Expected {image_count} scenes, found {len(scenes)}")

    job = MANGA_GENERATION_JOBS.get(chapter_id)
    if not job or not job.active:
        ACTIVE_MANGA_GENERATIONS.add(chapter_id)
        job = MangaGenerationJob(chapter_id, image_count)
        MANGA_GENERATION_JOBS[chapter_id] = job
        job.task = asyncio.create_task(
            _run_manga_generation_job(
                job,
                chapter_id,
                image_count,
                scenes,
                _user_image_api_key(request),
            )
        )

    return EventSourceResponse(_stream_manga_generation_job(job), ping=10)


# ─── Regenerate single image ─────────────────────────────

@app.post("/api/chapters/{chapter_id}/regenerate-image/{image_number}")
async def regenerate_single_image(chapter_id: int, image_number: int, body: dict, request: Request, db: Session = Depends(get_db)):
    """Regenerate a single panel image with an updated prompt."""
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    image_count = _load_image_count(chapter_id, db)
    if image_number < 1 or image_number > image_count:
        raise HTTPException(400, f"image_number must be 1-{image_count}")

    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    all_scenes = _load_chapter_scenes(chapter)
    if len(all_scenes) == image_count:
        all_scenes[image_number - 1] = prompt
        _save_chapter_scenes(chapter, all_scenes)
    else:
        all_scenes = None

    # Keep old DB/file intact until the new image is generated successfully.
    old_img = db.query(MangaImage).filter(
        MangaImage.chapter_id == chapter_id,
        MangaImage.image_number == image_number,
    ).first()
    old_path = Path(__file__).resolve().parent / old_img.image_path if old_img else None

    # Generate new image
    ref_imgs = _effective_ref_image_paths(chapter_id, db)
    async with MANGA_GENERATION_SEMAPHORE:
        image_path = await generate_manga_image(
            prompt,
            chapter_id,
            image_number,
            all_scenes=all_scenes,
            character_profiles=_load_characters(chapter_id, db),
            ref_image_paths=[str(p) for p in ref_imgs] if ref_imgs else None,
            color_mode=_load_color_mode(chapter_id, db),
            api_key=_user_image_api_key(request),
        )

    if old_img:
        manga = old_img
        manga.image_path = image_path
        manga.prompt = prompt
    else:
        manga = MangaImage(
            chapter_id=chapter_id,
            image_number=image_number,
            image_path=image_path,
            prompt=prompt,
        )
        db.add(manga)
    db.commit()
    db.refresh(manga)

    if old_path and old_path.exists() and old_path != Path(__file__).resolve().parent / image_path:
        try:
            old_path.unlink()
        except OSError as exc:
            logger.warning("Failed to delete old regenerated image %s: %s", old_path, exc)

    return {
        "id": manga.id,
        "image_number": image_number,
        "image_path": image_path,
        "prompt": prompt,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
    )
