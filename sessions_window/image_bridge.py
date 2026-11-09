"""PIL.Image → QPixmap conversion; the single crossing point into Qt."""

import logging
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

log = logging.getLogger(__name__)


def pil_to_qpixmap(img: Image.Image) -> Optional[QPixmap]:
    """Convert a PIL Image to a QPixmap; returns None on failure instead of raising."""
    try:
        rgba = img.convert("RGBA")
        data = rgba.tobytes("raw", "RGBA")
        stride = rgba.width * 4
        qimage = QImage(
            data, rgba.width, rgba.height, stride, QImage.Format.Format_RGBA8888
        ).copy()  # QImage does not copy the buffer it is given, so .copy() is required.
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
    """Aspect-correct downscale into a width×height box of LOGICAL pixels.

    Rasterises at the display's physical density; pass widget.devicePixelRatioF().
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
