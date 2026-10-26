"""Rendering package. Asset paths are anchored to the project root so the bot
works regardless of the current working directory."""

import logging
import os
import tempfile
import threading
from pathlib import Path

ASSETS_ROOT = Path(__file__).resolve().parents[2] / "assets"
logger = logging.getLogger(__name__)


_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


def save_png_atomic(img, filename: str) -> str:
    """Write ``img`` as PNG without ever exposing a half-written file.

    Renders run in worker threads; two renders of the same image are
    serialized, and readers only ever see the old or the new complete file.
    """
    path = Path(filename)
    with _lock_for(path):
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                img.save(fh, format="png")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return filename


def asset_path(*parts: str) -> str:
    """Return an absolute path inside the assets directory."""
    return str(ASSETS_ROOT.joinpath(*parts))


async def prerender(stage: str, season: int) -> None:
    """Warm the image cache for a finished stage without blocking progression."""
    try:
        if stage == "OPEN":
            from .StandingsMaker import get_open

            await get_open(season=season)
        elif stage == "PLAYIN":
            from .StandingsMaker import get_playin

            await get_playin(season=season)
        elif stage == "SWISS":
            from .StandingsMaker import get_swiss

            await get_swiss(season=season)
    except Exception:
        logger.exception("Failed to pre-render %s for season %s", stage, season)
