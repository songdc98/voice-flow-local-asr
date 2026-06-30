#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from PIL import Image


BASE_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = BASE_DIR / "macos" / "assets"
ICONSET_DIR = ASSETS_DIR / "VoiceFlow.iconset"
SOURCE_ICON = ASSETS_DIR / "sources" / "voicebot_cat_original.png"


def square_crop(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def draw_app_icon(size: int) -> Image.Image:
    source = square_crop(Image.open(SOURCE_ICON))
    return source.resize((size, size), Image.Resampling.LANCZOS)


def draw_status_icon(size: int) -> Image.Image:
    source = square_crop(Image.open(SOURCE_ICON))
    return source.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    if not SOURCE_ICON.exists():
        raise FileNotFoundError(f"Missing source icon: {SOURCE_ICON}")

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    ICONSET_DIR.mkdir(parents=True, exist_ok=True)

    icon_specs = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    for filename, size in icon_specs:
        draw_app_icon(size).save(ICONSET_DIR / filename)

    draw_status_icon(44).save(ASSETS_DIR / "status_wave.png")


if __name__ == "__main__":
    main()
