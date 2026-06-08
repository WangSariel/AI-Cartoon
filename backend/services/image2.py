from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

from config import settings
from metrics import record_image_retry
from paths import MANGA_OUTPUTS_DIR, chapter_dir
from .errors import MissingApiKeyError

logger = logging.getLogger("image2")

DEFAULT_IMAGE_API_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
NON_RETRYABLE_STATUS_CODES = {400, 401, 402, 403, 404}
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
UNKNOWN_EXCEPTION_RETRIES = 1

ProgressCallback = Callable[[str, dict], Awaitable[None]]


class NonRetryableImageApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class EndpointState:
    url: str
    name: str


def normalize_image_api_base_url(base_url: str | None) -> str:
    base = (base_url or DEFAULT_IMAGE_API_BASE_URL).strip().rstrip("/")
    return re.sub(r"(?i)(/v1)+$", "/v1", base)


IMAGE_API_BASE_URL = normalize_image_api_base_url(settings.image_api_base_url)
IMAGE_API_BASE_URL_FALLBACK = (
    normalize_image_api_base_url(settings.image_api_base_url_fallback)
    if settings.image_api_base_url_fallback
    else ""
)
IMAGE_API_KEY = settings.image_api_key
IMAGE_MODEL = settings.image_model
IMAGE_SIZE = settings.image_size
IMAGE_OUTPUT_FORMAT = settings.image_output_format
IMAGE_WATERMARK = settings.image_watermark
OUTPUT_DIR = MANGA_OUTPUTS_DIR
MAX_RETRIES = max(1, settings.image_max_retries)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_image_client() -> httpx.AsyncClient:
    """Reuse one AsyncClient so image generation can benefit from pooling."""
    global _client
    if _client and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client and not _client.is_closed:
            return _client
        timeout = httpx.Timeout(
            timeout=settings.image_request_timeout_seconds,
            connect=settings.image_connect_timeout_seconds,
            read=settings.image_read_timeout_seconds,
            write=settings.image_write_timeout_seconds,
            pool=settings.image_pool_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=settings.image_max_connections,
            max_keepalive_connections=settings.image_max_keepalive_connections,
            keepalive_expiry=30,
        )
        _client = httpx.AsyncClient(timeout=timeout, limits=limits)
        return _client


async def close_image_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _image_auth_headers(api_key: str | None = None, json_content: bool = False) -> dict[str, str]:
    key = (api_key or IMAGE_API_KEY or "").strip()
    if not key:
        raise MissingApiKeyError("图片 API")
    headers = {"Authorization": f"Bearer {key}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def normalize_image_bytes(image_bytes: bytes) -> bytes:
    """Validate image bytes and return normalized PNG bytes."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(image_bytes)) as img:
            output = io.BytesIO()
            img.convert("RGBA").save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception as exc:
        raise RuntimeError("Generated image response is not a valid image") from exc


def _retry_delay(attempt_index: int) -> float:
    base = settings.image_retry_base_seconds * (2 ** attempt_index)
    return min(settings.image_retry_max_seconds, base) + random.uniform(0, settings.image_retry_jitter_seconds)


def _status_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        detail = data.get("error") or data.get("detail") or data
        return f"HTTP {resp.status_code}: {detail}"
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:500]}"


def _friendly_status_message(resp: httpx.Response) -> str:
    raw = _status_error_message(resp)
    messages = {
        400: "图片 API 参数不被上游接受，请检查 IMAGE_MODEL、IMAGE_SIZE 或参考图格式。",
        401: "图片 API Key 无效或缺失，请检查 IMAGE_API_KEY 或网页右下角填写的图片 Key。",
        402: "图片 API 余额不足，请充值或更换可用的图片 API Key。",
        403: "图片 API 当前无权限访问该模型或端点，请检查账号权限和 endpoint。",
        404: "图片 API endpoint 或模型不存在，请检查 IMAGE_API_BASE_URL 和 IMAGE_MODEL。",
    }
    prefix = messages.get(resp.status_code)
    return f"{prefix}（{raw}）" if prefix else raw


def _is_ark_endpoint(endpoint_url: str) -> bool:
    return "ark.cn-beijing.volces.com/api/v3" in endpoint_url.lower()


def _friendly_retry_reason(reason: str) -> str:
    if reason in {"RemoteProtocolError", "ReadError"}:
        return "上游连接中断"
    if reason in {"ReadTimeout", "ConnectTimeout", "TimeoutException"}:
        return "上游响应超时"
    if reason.startswith("http_429"):
        return "上游限流"
    if reason.startswith("http_5") or reason in {"http_500", "http_502", "http_503", "http_504"}:
        return "上游服务暂时不可用"
    if reason.startswith("http_"):
        return f"上游返回 {reason.replace('_', ' ').upper()}"
    if reason.startswith("unknown_"):
        return "图片服务出现未知异常"
    return reason


def _is_retryable_http_error(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code in RETRYABLE_STATUS_CODES or 500 <= exc.response.status_code <= 599


def _is_retryable_transport_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.NetworkError,
        ),
    )


def _endpoint_plan() -> list[EndpointState]:
    endpoints = [EndpointState(IMAGE_API_BASE_URL, "primary")]
    if IMAGE_API_BASE_URL_FALLBACK and IMAGE_API_BASE_URL_FALLBACK != IMAGE_API_BASE_URL:
        endpoints.append(EndpointState(IMAGE_API_BASE_URL_FALLBACK, "fallback"))
    return endpoints


def _should_use_fallback(endpoints: list[EndpointState], primary_failures: int) -> bool:
    return (
        len(endpoints) > 1
        and primary_failures >= max(1, settings.image_primary_failures_before_fallback)
    )


async def _emit(on_progress: ProgressCallback | None, event: str, data: dict) -> None:
    if on_progress:
        await on_progress(event, data)


async def _heartbeat(label: str, start_ts: float, on_progress: ProgressCallback | None) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            waited = int(time.time() - start_ts)
            logger.info("[%s] 等待中... 已等待 %ss（上游可能排队，继续等）", label, waited)
            await _emit(on_progress, "waiting", {"message": f"上游仍在处理，已等待 {waited}s", "waited": waited})
    except asyncio.CancelledError:
        return


def _build_prompt(
    prompt: str,
    image_number: int,
    total_pages: int,
    all_scenes: list[str] | None,
    character_profiles: str,
    use_ref: bool,
    color_mode: str,
) -> str:
    char_block = ""
    if character_profiles and not use_ref:
        char_block = f"【角色外貌设定（每张图必须严格遵守）】\n{character_profiles}\n\n"

    ref_block = ""
    if use_ref:
        ref_block = (
            "【最重要：人物一致性】\n"
            "本次提供了参考图，必须严格保持参考图中主角的外貌特征："
            "包括发型、发色、瞳色、脸型、五官比例、服装风格。"
            "所有分镜格中的人物都必须是参考图中的同一批人物，禁止凭空创造新的人物外貌。\n\n"
        )

    if color_mode == "color":
        manga_style = (
            "日式彩色漫画插画页，竖向多格分镜布局，每页包含4-6个分镜格，"
            "格子高度不等，每个分镜格之间有清晰的边框分隔，包含圆形/椭圆形对话气泡和中文台词，"
            "包含漫画音效字，全彩高饱和度配色，日系动漫赛璐珞上色风格，柔和光影与高光，"
            "人物绘制精美，表情生动，动作有力度感"
        )
    else:
        manga_style = (
            "日式黑白漫画页，竖向多格分镜布局，每页包含4-6个分镜格，"
            "格子高度不等，每个分镜格之间有清晰的黑色边框分隔，包含圆形/椭圆形白色对话气泡和中文台词，"
            "包含漫画音效字，黑白高对比度，戏剧性光影，精细线条和网点，"
            "人物绘制精美，表情生动，动作有力度感"
        )

    if all_scenes:
        script_context = "\n".join(f"第{i + 1}页：{scene}" for i, scene in enumerate(all_scenes))
        return (
            f"{ref_block}{char_block}"
            f"你正在绘制一部日式漫画的第{image_number}页（共{total_pages}页）。\n"
            f"以下是完整的{total_pages}页分镜脚本，请保持人物外貌、服装、风格一致：\n\n"
            f"{script_context}\n\n"
            f"现在请绘制第{image_number}页：\n{manga_style}\n{prompt}"
        )
    return f"{ref_block}{char_block}{manga_style}\n{prompt}"


async def _load_reference_blobs(ref_image_paths: list[str] | None, progress_label: str) -> list[tuple[str, bytes]]:
    valid_refs = [Path(p) for p in (ref_image_paths or []) if p and Path(p).exists()]
    blobs: list[tuple[str, bytes]] = []
    for idx, ref_path in enumerate(valid_refs, start=1):
        try:
            def _read() -> bytes:
                with Image.open(ref_path) as img:
                    max_side = 1024
                    ratio = min(max_side / img.width, max_side / img.height)
                    if ratio < 1:
                        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.convert("RGBA").save(buf, format="PNG", optimize=True)
                    return buf.getvalue()

            blob = await asyncio.to_thread(_read)
            blobs.append((f"ref{idx}.png", blob))
            logger.info("[%s] 参考图 %s 已加载: %s -> %.0f KB", progress_label, idx, ref_path.name, len(blob) / 1024)
        except Exception as exc:
            logger.warning("[%s] 参考图 %s 加载失败: %s", progress_label, ref_path, exc)
    return blobs


async def _post_image_request(
    endpoint: EndpointState,
    ref_blobs: list[tuple[str, bytes]],
    full_prompt: str,
    api_key: str | None,
) -> httpx.Response:
    client = await get_image_client()
    if ref_blobs and not _is_ark_endpoint(endpoint.url):
        files = (
            [("image", (ref_blobs[0][0], io.BytesIO(ref_blobs[0][1]), "image/png"))]
            if len(ref_blobs) == 1
            else [("image[]", (name, io.BytesIO(blob), "image/png")) for name, blob in ref_blobs]
        )
        return await client.post(
            f"{endpoint.url}/images/edits",
            files=files,
            data={"model": IMAGE_MODEL, "prompt": full_prompt, "size": IMAGE_SIZE},
            headers=_image_auth_headers(api_key),
        )
    payload = {
        "model": IMAGE_MODEL,
        "prompt": full_prompt,
        "size": IMAGE_SIZE,
    }
    if _is_ark_endpoint(endpoint.url):
        payload.update({
            "output_format": IMAGE_OUTPUT_FORMAT,
            "watermark": IMAGE_WATERMARK,
        })
    return await client.post(
        f"{endpoint.url}/images/generations",
        json=payload,
        headers=_image_auth_headers(api_key, json_content=True),
    )


async def _download_image(url: str, on_progress: ProgressCallback | None) -> bytes:
    await _emit(on_progress, "downloading", {"message": "正在下载上游生成的图片"})
    client = await get_image_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def generate_manga_image(
    prompt: str,
    chapter_id: int,
    image_number: int,
    all_scenes: list[str] | None = None,
    character_profiles: str = "",
    ref_image_paths: list[str] | None = None,
    color_mode: str = "bw",
    api_key: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Generate a single manga image and save it. Returns the relative file path."""
    total_pages = len(all_scenes) if all_scenes else 1
    progress_label = f"{image_number}/{total_pages}"
    ref_blobs = await _load_reference_blobs(ref_image_paths, progress_label)
    use_ref_images = bool(ref_blobs) and not _is_ark_endpoint(IMAGE_API_BASE_URL)
    if ref_blobs and not use_ref_images:
        logger.info("[%s] Ark images/generations 暂不发送垫图文件，将使用角色文字设定保持一致性", progress_label)
    full_prompt = _build_prompt(
        prompt=prompt,
        image_number=image_number,
        total_pages=total_pages,
        all_scenes=all_scenes,
        character_profiles=character_profiles,
        use_ref=use_ref_images,
        color_mode=color_mode,
    )

    endpoints = _endpoint_plan()
    primary_failures = 0
    unknown_failures = 0
    last_err: BaseException | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        if _should_use_fallback(endpoints, primary_failures):
            endpoint = endpoints[1]
        else:
            endpoint = endpoints[0]

        try:
            mode = f"edits({len(ref_blobs)}图垫图)" if ref_blobs and not _is_ark_endpoint(endpoint.url) else "generations"
            logger.info(
                "[%s] 开始调用图片 API [%s] endpoint=%s attempt=%s/%s",
                progress_label,
                mode,
                endpoint.name,
                attempt,
                MAX_RETRIES,
            )
            await _emit(on_progress, "status", {
                "message": f"正在请求图片服务（{endpoint.name}，第 {attempt}/{MAX_RETRIES} 次）",
                "attempt": attempt,
                "endpoint": endpoint.name,
            })
            t0 = time.time()
            heartbeat_task = asyncio.create_task(_heartbeat(progress_label, t0, on_progress))
            try:
                resp = await _post_image_request(endpoint, ref_blobs, full_prompt, api_key)
                if resp.status_code in NON_RETRYABLE_STATUS_CODES:
                    raise NonRetryableImageApiError(_friendly_status_message(resp))
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if _is_retryable_http_error(exc):
                        raise
                    raise NonRetryableImageApiError(_friendly_status_message(resp)) from exc
                data = resp.json()
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):  # type: ignore[name-defined]
                    await heartbeat_task

            elapsed = time.time() - t0
            logger.info("[%s] API 返回成功，耗时 %.1fs endpoint=%s", progress_label, elapsed, endpoint.name)
            image_entry = data["data"][0]

            if image_entry.get("b64_json"):
                image_bytes = base64.b64decode(image_entry["b64_json"])
                logger.info("[%s] 收到 b64_json，大小 %s bytes", progress_label, len(image_bytes))
            elif image_entry.get("url"):
                image_bytes = await _download_image(image_entry["url"], on_progress)
                logger.info("[%s] 下载完成，大小 %s bytes", progress_label, len(image_bytes))
            else:
                raise RuntimeError("No b64_json or url in image response")

            image_bytes = await asyncio.to_thread(normalize_image_bytes, image_bytes)
            logger.info("[%s] 图片验证通过，PNG 大小 %s bytes", progress_label, len(image_bytes))

            target_dir = chapter_dir(chapter_id)
            await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
            filename = f"panel_{image_number:02d}_{uuid.uuid4().hex[:8]}.png"
            filepath = target_dir / filename
            await asyncio.to_thread(filepath.write_bytes, image_bytes)
            logger.info("[%s] 已保存到 %s", progress_label, filepath)
            return f"manga_outputs/chapter_{chapter_id}/{filename}"

        except NonRetryableImageApiError as exc:
            last_err = exc
            logger.error("[%s] 不可重试错误: %s", progress_label, exc)
            raise
        except httpx.HTTPStatusError as exc:
            last_err = exc
            status = exc.response.status_code
            if status in NON_RETRYABLE_STATUS_CODES:
                raise NonRetryableImageApiError(_friendly_status_message(exc.response)) from exc
            reason = f"http_{status}"
        except Exception as exc:
            last_err = exc
            if _is_retryable_transport_error(exc):
                reason = exc.__class__.__name__
            else:
                unknown_failures += 1
                if unknown_failures > UNKNOWN_EXCEPTION_RETRIES:
                    raise RuntimeError(f"Image API failed with non-retryable unknown error: {exc}") from exc
                reason = f"unknown_{exc.__class__.__name__}"

        if endpoint.name == "primary":
            primary_failures += 1

        if attempt >= MAX_RETRIES:
            break

        delay = _retry_delay(attempt - 1)
        friendly_reason = _friendly_retry_reason(reason)
        record_image_retry(reason, endpoint.name)
        logger.warning(
            "[%s] 尝试 %s/%s 失败（%s），%.1fs 后重试",
            progress_label,
            attempt,
            MAX_RETRIES,
            reason,
            delay,
        )
        await _emit(on_progress, "retrying", {
            "message": f"{friendly_reason}，{delay:.1f}s 后重试",
            "attempt": attempt,
            "next_attempt": attempt + 1,
            "reason": reason,
            "reason_label": friendly_reason,
            "delay": delay,
            "endpoint": endpoint.name,
            "switching_to_fallback": _should_use_fallback(endpoints, primary_failures),
        })
        await asyncio.sleep(delay)

    raise RuntimeError(
        f"第{image_number}张图片生成失败（已尝试{MAX_RETRIES}次）。"
        f"请检查 IMAGE_API_BASE_URL、IMAGE_API_BASE_URL_FALLBACK、IMAGE_MODEL、图片 API Key 和网络代理。"
        f"最后一次错误: {last_err}"
    )
