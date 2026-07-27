"""
sessions_window/image_bridge.py — PIL.Image → QPixmap conversion.

`recorder/screenshot.py::render_event_image()` returns a PIL Image (the render
pipeline is PIL-based and deliberately Qt-free — the service layer must not
import a UI toolkit). Qt needs a QPixmap. This module is the single crossing
point.

WHY NOT PIL.ImageQt: it's in `autodoc.spec`'s excludes, and it hard-couples
Pillow's build to the Qt binding present at import time (it probes for
PySide6/PyQt5/PyQt6 in order). A ~10-line explicit conversion has no such
coupling and no extra dependency.

THE OWNERSHIP TRAP — read before touching pil_to_qpixmap():
`QImage(bytes, w, h, stride, fmt)` does NOT copy the buffer; it borrows it and
assumes the caller keeps it alive. Build a QImage over a temporary Python
`bytes` and the buffer is freed as soon as that local goes out of scope,
leaving Qt painting from released memory: garbage pixels, or a hard crash,
often only under memory pressure or after a few dozen thumbnails — i.e. it
"works" in a quick test and fails on a real session. `.copy()` forces Qt to
take its own copy, after which the Python buffer is irrelevant. Do not
"optimise" that call away.

Also: the stride argument (bytes per line) must be passed explicitly. Letting
Qt infer it works only when the row length happens to match Qt's 32-bit
alignment assumption, which is why omitting it produces images that look
correctly coloured but progressively skewed — a diagonal shear.
"""

import logging
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

log = logging.getLogger(__name__)


def pil_to_qpixmap(img: Image.Image) -> Optional[QPixmap]:
    """
    Convert a PIL Image to a QPixmap that owns its own pixel data.

    Returns None on failure rather than raising: a single unreadable screenshot
    should leave a gap in the thumbnail grid, not tear down the editor.
    """
    try:
        rgba = img.convert("RGBA")
        data = rgba.tobytes("raw", "RGBA")
        stride = rgba.width * 4          # RGBA8888 → 4 bytes per pixel
        qimage = QImage(
            data, rgba.width, rgba.height, stride, QImage.Format.Format_RGBA8888
        ).copy()                          # .copy() = Qt owns the buffer. Required.
        if qimage.isNull():
            log.warning("pil_to_qpixmap: produced a null QImage (%dx%d)", rgba.width, rgba.height)
            return None
        return QPixmap.fromImage(qimage)
    except Exception:
        log.exception("pil_to_qpixmap: conversion failed")
        return None


def scaled_to_fit(
    pixmap: QPixmap, width: int, height: int, device_pixel_ratio: float = 1.0
) -> QPixmap:
    """
    Aspect-correct downscale into a width×height box of LOGICAL pixels.

    ALWAYS call this against the ORIGINAL pixmap, never against a previously
    scaled result — repeated resampling visibly degrades the image, and on a
    window the user drags around it degrades a little more on every resize
    event. Callers keep the original and re-derive from it each time.

    HIGH-DPI — the reason device_pixel_ratio exists (fixed 2026-07-24):
    Qt widget geometry is in LOGICAL pixels, so a thumbnail label reports e.g.
    224x120 regardless of display scaling. Scaling the source to exactly that
    and handing it to Qt on a 150% display means Qt then stretches a 224px-wide
    image across 336 physical pixels — a 1.5x upscale of an already-downscaled
    screenshot. The result looked markedly worse in the editor than the same PNG
    opened from the sessions folder, which is exactly the symptom reported.

    The fix is to render at PHYSICAL resolution (logical x dpr) and tag the
    pixmap with its ratio, after which Qt draws it 1:1 at the intended logical
    size with no resampling. Callers pass widget.devicePixelRatioF().

    Note this is unrelated to main.py's DPI-awareness call: that governs whether
    Windows tells us the true DPI at all (and keeps mss/pynput coordinates
    physical). This governs whether we then USE that DPI when rasterising.
    """
    dpr = max(float(device_pixel_ratio), 1.0)
    out = pixmap.scaled(
        int(width * dpr),
        int(height * dpr),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    out.setDevicePixelRatio(dpr)
    return out
