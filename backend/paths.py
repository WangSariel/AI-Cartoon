from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
MANGA_OUTPUTS_DIR = BACKEND_DIR / "manga_outputs"
COVERS_DIR = MANGA_OUTPUTS_DIR / "covers"
THUMB_DIR = MANGA_OUTPUTS_DIR / ".thumbs"


def chapter_dir(chapter_id: int) -> Path:
    return MANGA_OUTPUTS_DIR / f"chapter_{chapter_id}"


def story_dir(story_id: int) -> Path:
    return MANGA_OUTPUTS_DIR / f"story_{story_id}"


def story_ref_dir(story_id: int) -> Path:
    return story_dir(story_id) / "ref_images"


def chapter_ref_dir(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "ref_images"


def asset_group_ref_dir(group_id: int) -> Path:
    return MANGA_OUTPUTS_DIR / "asset_groups" / f"group_{group_id}" / "ref_images"


def legacy_story_ref_image(story_id: int) -> Path:
    return story_dir(story_id) / "ref_image.png"


def legacy_chapter_ref_image(chapter_id: int) -> Path:
    return chapter_dir(chapter_id) / "ref_image.png"


def backend_path(relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    path = (BACKEND_DIR / relative_path).resolve()
    try:
        path.relative_to(BACKEND_DIR.resolve())
    except ValueError:
        return None
    return path


def manga_relative(path: Path) -> str:
    return str(path.relative_to(BACKEND_DIR).as_posix())
