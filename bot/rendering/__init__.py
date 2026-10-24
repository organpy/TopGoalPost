"""Rendering package. Asset paths are anchored to the project root so the bot
works regardless of the current working directory."""

import logging
from pathlib import Path

ASSETS_ROOT = Path(__file__).resolve().parents[2] / "assets"
logger = logging.getLogger(__name__)


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
