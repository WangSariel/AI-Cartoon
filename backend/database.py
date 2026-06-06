import logging
import shutil
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings

load_dotenv()

DATABASE_URL = settings.database_url
BASE_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("database")

engine_kwargs = {"echo": False, "pool_pre_ping": True}
if DATABASE_URL.startswith("postgresql"):
    engine_kwargs.update({
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
    })
elif DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Add missing columns to existing tables (poor-man's migration)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    is_sqlite = engine.dialect.name == "sqlite"   # 1. 定义 is_sqlite

    if "stories" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("stories")}
        with engine.begin() as conn:
            if "description" not in cols:
                conn.execute(text("ALTER TABLE stories ADD COLUMN description TEXT DEFAULT ''"))
            if "cover_image" not in cols:
                conn.execute(text("ALTER TABLE stories ADD COLUMN cover_image VARCHAR(500)"))
            if "ref_image" not in cols:
                conn.execute(text("ALTER TABLE stories ADD COLUMN ref_image VARCHAR(500)"))
            if "character_profiles" not in cols:
                conn.execute(text("ALTER TABLE stories ADD COLUMN character_profiles TEXT DEFAULT ''"))
            if "deleted_at" not in cols:
                conn.execute(text("ALTER TABLE stories ADD COLUMN deleted_at TIMESTAMP"))

    if "chapters" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("chapters")}
        with engine.begin() as conn:
            if "content_source" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN content_source VARCHAR(20)"))
            if "scenes_text" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN scenes_text TEXT"))
            if "character_profiles" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN character_profiles TEXT"))
            if "asset_group_id" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN asset_group_id INTEGER"))
            if "ref_image" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN ref_image VARCHAR(500)"))
            if "color_mode" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN color_mode VARCHAR(20)"))
            if "image_count" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN image_count INTEGER"))
            if "deleted_at" not in cols:
                conn.execute(text("ALTER TABLE chapters ADD COLUMN deleted_at TIMESTAMP"))

        # 2. 更新 content_source，放在 if "chapters" 块内，单独使用一个连接
        trim_func = "TRIM" if is_sqlite else "btrim"
        with engine.begin() as conn:
            conn.execute(text(f"""
                UPDATE chapters
                SET content_source = CASE
                    WHEN novel_content IS NOT NULL AND {trim_func}(novel_content) <> ''
                         AND (
                            SELECT COUNT(*) FROM chat_messages
                            WHERE chat_messages.chapter_id = chapters.id
                         ) = 1
                         AND EXISTS (
                            SELECT 1 FROM chat_messages
                            WHERE chat_messages.chapter_id = chapters.id
                              AND chat_messages.role = 'user'
                              AND chat_messages.content = chapters.novel_content
                         )
                    THEN 'import'
                    WHEN EXISTS (
                        SELECT 1 FROM chat_messages
                        WHERE chat_messages.chapter_id = chapters.id
                    )
                    THEN 'chat'
                    ELSE NULL
                END
                WHERE content_source IS NULL
            """))

    # 3. 清理重复数据（这些函数内部会检查表是否存在，放在最后执行）
    _cleanup_duplicate_chapters()
    _cleanup_duplicate_manga_images()
    _add_unique_constraints()

def _safe_unlink(path: Path):
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("Failed to delete duplicate image file %s: %s", path, exc)


def _safe_rmtree(path: Path):
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError as exc:
        logger.warning("Failed to delete duplicate chapter directory %s: %s", path, exc)


def _cleanup_duplicate_manga_images():
    """Keep the newest image record per chapter/image number and remove older duplicates."""
    from sqlalchemy import text

    with engine.begin() as conn:
        groups = conn.execute(text("""
            SELECT chapter_id, image_number
            FROM manga_images
            GROUP BY chapter_id, image_number
            HAVING COUNT(*) > 1
        """)).mappings().all()

        for group in groups:
            rows = conn.execute(text("""
                SELECT id, image_path, created_at
                FROM manga_images
                WHERE chapter_id = :chapter_id AND image_number = :image_number
                ORDER BY created_at IS NULL ASC, created_at DESC, id DESC
            """), dict(group)).mappings().all()
            for row in rows[1:]:
                _safe_unlink(BASE_DIR / row["image_path"])
                conn.execute(text("DELETE FROM manga_images WHERE id = :id"), {"id": row["id"]})
                logger.warning(
                    "Deleted duplicate manga image id=%s chapter_id=%s image_number=%s",
                    row["id"],
                    group["chapter_id"],
                    group["image_number"],
                )


def _chapter_score(chapter: dict, has_dir: bool) -> tuple[int, int, int, int]:
    novel_score = 1 if chapter["novel_content"] else 0
    return (
        novel_score,
        int(chapter["message_count"] or 0),
        int(chapter["image_count"] or 0),
        1 if has_dir else 0,
    )


def _cleanup_duplicate_chapters():
    """Keep the most content-rich chapter per story/chapter number and remove duplicates."""
    from sqlalchemy import text

    with engine.begin() as conn:
        groups = conn.execute(text("""
            SELECT story_id, chapter_number
            FROM chapters
            GROUP BY story_id, chapter_number
            HAVING COUNT(*) > 1
        """)).mappings().all()

        for group in groups:
            chapters = conn.execute(text("""
                SELECT
                    c.id,
                    c.novel_content,
                    c.created_at,
                    COUNT(DISTINCT cm.id) AS message_count,
                    COUNT(DISTINCT mi.id) AS image_count
                FROM chapters c
                LEFT JOIN chat_messages cm ON cm.chapter_id = c.id
                LEFT JOIN manga_images mi ON mi.chapter_id = c.id
                WHERE c.story_id = :story_id AND c.chapter_number = :chapter_number
                GROUP BY c.id
                ORDER BY c.created_at IS NULL ASC, c.created_at DESC, c.id DESC
            """), dict(group)).mappings().all()

            def sort_key(chapter):
                has_dir = (BASE_DIR / "manga_outputs" / f"chapter_{chapter['id']}").exists()
                return (*_chapter_score(chapter, has_dir), chapter["created_at"], chapter["id"])

            keep = max(chapters, key=sort_key)
            for chapter in chapters:
                if chapter["id"] == keep["id"]:
                    continue
                images = conn.execute(text("""
                    SELECT image_path
                    FROM manga_images
                    WHERE chapter_id = :chapter_id
                """), {"chapter_id": chapter["id"]}).mappings().all()
                for image in images:
                    _safe_unlink(BASE_DIR / image["image_path"])
                _safe_rmtree(BASE_DIR / "manga_outputs" / f"chapter_{chapter['id']}")
                conn.execute(text("DELETE FROM chat_messages WHERE chapter_id = :chapter_id"), {"chapter_id": chapter["id"]})
                conn.execute(text("DELETE FROM manga_images WHERE chapter_id = :chapter_id"), {"chapter_id": chapter["id"]})
                conn.execute(text("DELETE FROM chapters WHERE id = :chapter_id"), {"chapter_id": chapter["id"]})
                logger.warning(
                    "Deleted duplicate chapter id=%s story_id=%s chapter_number=%s; kept id=%s",
                    chapter["id"],
                    group["story_id"],
                    group["chapter_number"],
                    keep["id"],
                )


def _add_unique_constraints():
    """Apply model unique constraints to existing PostgreSQL tables."""
    from sqlalchemy import inspect, text

    if engine.dialect.name != "postgresql":
        logger.warning("Skipping unique constraint migration for non-PostgreSQL database: %s", engine.dialect.name)
        return

    insp = inspect(engine)
    table_names = set(insp.get_table_names())
    if not {"chapters", "manga_images"}.issubset(table_names):
        return

    existing = {
        constraint["name"]
        for table in ("chapters", "manga_images")
        for constraint in insp.get_unique_constraints(table)
    }

    with engine.begin() as conn:
        if "uq_chapters_story_number" not in existing:
            conn.execute(text("""
                ALTER TABLE chapters
                ADD CONSTRAINT uq_chapters_story_number
                UNIQUE (story_id, chapter_number)
            """))
        if "uq_manga_images_chapter_number" not in existing:
            conn.execute(text("""
                ALTER TABLE manga_images
                ADD CONSTRAINT uq_manga_images_chapter_number
                UNIQUE (chapter_id, image_number)
            """))
