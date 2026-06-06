import time
from contextlib import contextmanager

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except Exception:  # pragma: no cover - optional dependency fallback
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    Counter = Histogram = None  # type: ignore[assignment]

    def generate_latest() -> bytes:  # type: ignore[override]
        return b"# prometheus_client is not installed\n"


if Counter and Histogram:
    REQUEST_COUNT = Counter("lorevista_http_requests_total", "HTTP requests", ["method", "path", "status"])
    IMAGE_RETRY_COUNT = Counter("lorevista_image_retries_total", "Image API retries", ["reason", "endpoint"])
    MANGA_JOB_COUNT = Counter("lorevista_manga_jobs_total", "Manga generation jobs", ["status"])
    MANGA_JOB_SECONDS = Histogram("lorevista_manga_job_seconds", "Manga generation job duration")
else:
    REQUEST_COUNT = IMAGE_RETRY_COUNT = MANGA_JOB_COUNT = MANGA_JOB_SECONDS = None


def record_request(method: str, path: str, status: int) -> None:
    if REQUEST_COUNT:
        REQUEST_COUNT.labels(method=method, path=path, status=str(status)).inc()


def record_image_retry(reason: str, endpoint: str) -> None:
    if IMAGE_RETRY_COUNT:
        IMAGE_RETRY_COUNT.labels(reason=reason, endpoint=endpoint).inc()


def record_manga_job(status: str) -> None:
    if MANGA_JOB_COUNT:
        MANGA_JOB_COUNT.labels(status=status).inc()


@contextmanager
def manga_job_timer():
    start = time.perf_counter()
    try:
        yield
    finally:
        if MANGA_JOB_SECONDS:
            MANGA_JOB_SECONDS.observe(time.perf_counter() - start)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
