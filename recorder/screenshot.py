"""
recorder/screenshot.py — Screen capture with a privacy-preserving blur.

The screen is captured and then Gaussian-blurred everywhere except for a
soft-edged circular region centred on the user's click coordinates. This
preserves visual context around the point of interaction while obscuring
surrounding content (e.g. student data) before the image is stored or sent
to a cloud AI service.

Multi-monitor: only the monitor that contains the click is captured. This
keeps screenshots focused and avoids sending unrelated screens to Claude.
"""

from pathlib import Path

import mss
from PIL import Image, ImageDraw, ImageFilter


def _find_clicked_monitor(sct: mss.mss, x: int, y: int) -> dict:
    """
    Return the mss monitor dict that contains the point (x, y).
    Falls back to the primary monitor (monitors[1]) if none matches —
    which can happen if the click fires just as a window crosses a boundary.
    """
    for monitor in sct.monitors[1:]:   # monitors[0] is the virtual desktop
        if (
            monitor["left"] <= x < monitor["left"] + monitor["width"]
            and monitor["top"] <= y < monitor["top"] + monitor["height"]
        ):
            return monitor
    return sct.monitors[1]


def capture_and_blur(
    x: int,
    y: int,
    focus_radius: int = 200,
    blur_strength: int = 15,
) -> Image.Image:
    """
    Capture only the monitor the user clicked on, then return a blurred PIL
    Image with a clear circular window around (x, y).

    Args:
        x, y:           Click coordinates in Windows virtual screen space.
        focus_radius:   Radius (px) of the unblurred circle around the click.
        blur_strength:  GaussianBlur radius applied to the background.

    Returns:
        Composited PIL Image (RGB).
    """
    with mss.mss() as sct:
        monitor = _find_clicked_monitor(sct, x, y)
        raw = sct.grab(monitor)

    img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)

    # Translate Windows virtual-screen coords → pixel coords within this monitor.
    img_x = x - monitor["left"]
    img_y = y - monitor["top"]

    # Full-image blur for the background.
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_strength))

    # Grayscale mask: white (255) = show original, black (0) = show blurred.
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        [
            img_x - focus_radius,
            img_y - focus_radius,
            img_x + focus_radius,
            img_y + focus_radius,
        ],
        fill=255,
    )
    # Feather the circle edge for a natural vignette effect.
    mask = mask.filter(ImageFilter.GaussianBlur(radius=20))

    # Composite: original where mask is bright, blurred where mask is dark.
    result = Image.composite(img, blurred, mask)
    return result


def save_screenshot(
    img: Image.Image,
    dest_path: Path,
    max_dimension: int = 1280,
) -> None:
    """
    Resize (if needed) and save a screenshot as PNG.

    Downsampling reduces file size and Claude API token cost. Blur and
    composite are always applied at the original resolution first.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if img.width > max_dimension or img.height > max_dimension:
        ratio = max_dimension / max(img.width, img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    img.save(str(dest_path), "PNG")
