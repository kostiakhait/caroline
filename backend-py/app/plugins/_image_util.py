"""Crop/downscale/PNG for the screenshot tools on every platform (2026-10-24):
the same rules the old capture.exe/windowscreenshot.exe applied -- the crop
rectangle is clamped to the image (an empty intersection is an error), then a
proportional bicubic downscale when wider than max_width."""

from __future__ import annotations

import io
from typing import Any


def crop_and_downscale(image: Any, crop: tuple[int, int, int, int] | None, max_width: int | None) -> Any:
    from PIL import Image

    if crop is not None:
        cx, cy, cw, ch = crop
        left, top = max(cx, 0), max(cy, 0)
        right, bottom = min(cx + cw, image.width), min(cy + ch, image.height)
        if right <= left or bottom <= top:
            raise ValueError("Crop rectangle does not intersect the captured bitmap.")
        image = image.crop((left, top, right, bottom))
    if max_width is not None and image.width > max_width:
        new_height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, new_height), Image.BICUBIC)
    return image


def to_png(image: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
