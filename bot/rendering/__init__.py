"""Rendering package. Asset paths are anchored to the project root so the bot
works regardless of the current working directory."""

from pathlib import Path

ASSETS_ROOT = Path(__file__).resolve().parents[2] / "assets"


def asset_path(*parts: str) -> str:
    """Return an absolute path inside the assets directory."""
    return str(ASSETS_ROOT.joinpath(*parts))
