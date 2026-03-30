"""Shared Chinese/CJK text rendering utilities.

Both ``GuiApp`` and ``OverlayRenderer`` need to draw non-ASCII text on
OpenCV frames via PIL.  This module provides a shared font cache and
drawing helper so the font-search logic is not duplicated.
"""

from __future__ import annotations

import os
from typing import Any

import cv2
import numpy as np

_FONT_SEARCH_PATHS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

_font_cache: dict[int, Any] = {}


def get_cn_font(size: int) -> Any:
    """Return a PIL ``ImageFont`` that supports CJK, cached by *size*."""
    if size in _font_cache:
        return _font_cache[size]
    try:
        from PIL import ImageFont
    except Exception:
        return None
    font = None
    for path in _FONT_SEARCH_PATHS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size=size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def draw_cn_text(
    frame: np.ndarray,
    text: str,
    org: tuple[int, int],
    bgr_color: tuple[int, int, int],
    font_size: int = 18,
) -> None:
    """Draw *text* on *frame*.  Falls back to ``cv2.putText`` for pure ASCII."""
    if text.isascii():
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, bgr_color, 2)
        return
    try:
        from PIL import Image, ImageDraw
    except Exception:
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, bgr_color, 2)
        return
    font = get_cn_font(font_size)
    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    b, g, r = bgr_color
    draw.text(org, text, font=font, fill=(r, g, b))
    frame[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
