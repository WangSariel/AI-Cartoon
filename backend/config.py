import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./manga.db")
    db_pool_size: int = _int_env("DB_POOL_SIZE", 20)
    db_max_overflow: int = _int_env("DB_MAX_OVERFLOW", 10)

    cors_origins: list[str] = None  # type: ignore[assignment]
    api_tokens: list[str] = None  # type: ignore[assignment]
    require_api_token_for_remote: bool = os.getenv("REQUIRE_API_TOKEN_FOR_REMOTE", "true").lower() == "true"

    max_upload_bytes: int = _int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    max_import_zip_bytes: int = _int_env("MAX_IMPORT_ZIP_BYTES", 500 * 1024 * 1024)
    max_import_zip_files: int = _int_env("MAX_IMPORT_ZIP_FILES", 500)
    max_import_zip_file_bytes: int = _int_env("MAX_IMPORT_ZIP_FILE_BYTES", 10 * 1024 * 1024)
    max_import_zip_ratio: float = _float_env("MAX_IMPORT_ZIP_RATIO", 100.0)
    max_character_profile_chars: int = _int_env("MAX_CHARACTER_PROFILE_CHARS", 20000)
    max_imported_novel_chars: int = _int_env("MAX_IMPORTED_NOVEL_CHARS", 50000)
    max_ref_images_per_level: int = _int_env("MAX_REF_IMAGES_PER_LEVEL", 4)

    max_manga_generation_jobs: int = _int_env("MAX_MANGA_GENERATION_JOBS", 3)
    sse_queue_size: int = _int_env("SSE_QUEUE_SIZE", 300)
    job_event_history_limit: int = _int_env("JOB_EVENT_HISTORY_LIMIT", 300)

    image_api_base_url: str = os.getenv("IMAGE_API_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    image_api_base_url_fallback: str = os.getenv("IMAGE_API_BASE_URL_FALLBACK", "")
    image_api_key: str = os.getenv("IMAGE_API_KEY", os.getenv("ARK_API_KEY", ""))
    image_model: str = os.getenv("IMAGE_MODEL", "doubao-seedream-5-0-260128")
    image_size: str = os.getenv("IMAGE_SIZE", "2K")
    image_output_format: str = os.getenv("IMAGE_OUTPUT_FORMAT", "png")
    image_watermark: bool = _bool_env("IMAGE_WATERMARK", False)
    image_max_retries: int = _int_env("IMAGE_MAX_RETRIES", 6)
    image_retry_base_seconds: float = _float_env("IMAGE_RETRY_BASE_SECONDS", 1.0)
    image_retry_max_seconds: float = _float_env("IMAGE_RETRY_MAX_SECONDS", 30.0)
    image_retry_jitter_seconds: float = _float_env("IMAGE_RETRY_JITTER_SECONDS", 2.0)
    image_request_timeout_seconds: float = _float_env("IMAGE_REQUEST_TIMEOUT_SECONDS", 300.0)
    image_connect_timeout_seconds: float = _float_env("IMAGE_CONNECT_TIMEOUT_SECONDS", 30.0)
    image_read_timeout_seconds: float = _float_env("IMAGE_READ_TIMEOUT_SECONDS", 300.0)
    image_write_timeout_seconds: float = _float_env("IMAGE_WRITE_TIMEOUT_SECONDS", 120.0)
    image_pool_timeout_seconds: float = _float_env("IMAGE_POOL_TIMEOUT_SECONDS", 30.0)
    image_max_connections: int = _int_env("IMAGE_MAX_CONNECTIONS", 20)
    image_max_keepalive_connections: int = _int_env("IMAGE_MAX_KEEPALIVE_CONNECTIONS", 8)
    image_primary_failures_before_fallback: int = _int_env("IMAGE_PRIMARY_FAILURES_BEFORE_FALLBACK", 2)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cors_origins",
            _csv_env("CORS_ORIGINS") or ["http://localhost:5173", "http://127.0.0.1:5173"],
        )
        tokens = _csv_env("API_TOKENS") or _csv_env("API_TOKEN")
        object.__setattr__(self, "api_tokens", tokens)


settings = Settings()
