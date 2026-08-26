"""Screen capture plus non-destructive render effects (mode, redaction, crop).

Redaction and crop rectangles are image-space pixels, not display-space.
Crop is applied LAST (redactions -> mode effect -> crop); upstream coords are uncropped.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

import mss
from PIL import Image, ImageDraw, ImageFilter

RENDER_MODES = ("raw", "blurred", "circled")

# Crops narrower than this in either axis are ignored rather than applied.
MIN_CROP_PX = 24

# Mosaic rather than Gaussian blur: strong blurs of short text are invertible.
_REDACT_BLOCK_PX = 16


def _find_clicked_monitor(sct: mss.mss, x: int, y: int) -> dict:
    """The mss monitor containing (x, y), falling back to the primary one."""
    for monitor in sct.monitors[1:]:   # monitors[0] is the virtual desktop
        if (
            monitor["left"] <= x < monitor["left"] + monitor["width"]
            and monitor["top"] <= y < monitor["top"] + monitor["height"]
        ):
            return monitor
    return sct.monitors[1]


def capture_raw(x: int, y: int) -> Tuple[Image.Image, int, int]:
    """Capture the monitor containing (x, y), in Windows virtual screen space.

    Returns (image, img_x, img_y) — the click translated into image coordinates.
    """
    with mss.mss() as sct:
        monitor = _find_clicked_monitor(sct, x, y)
        raw = sct.grab(monitor)

    img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
    return img, x - monitor["left"], y - monitor["top"]


def save_screenshot_raw(
    img: Image.Image,
    click_x: int,
    click_y: int,
    dest_path: Path,
    max_dimension: int = 1280,
) -> Tuple[int, int]:
    """Downscale if needed and save a raw PNG; returns the click position in the
    SAVED image's pixel space (persist it on the event — every render needs it)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if img.width > max_dimension or img.height > max_dimension:
        ratio = max_dimension / max(img.width, img.height)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        click_x, click_y = int(click_x * ratio), int(click_y * ratio)

    img.save(str(dest_path), "PNG")
    return click_x, click_y


def render_image(
    img: Image.Image,
    click_x: int,
    click_y: int,
    mode: str,
    focus_radius: int = 200,
    blur_strength: int = 15,
    circle_radius: int = 36,
) -> Image.Image:
    """Apply a render mode. Coordinates are in the given image's pixel space."""
    if mode == "raw":
        return img

    if mode == "blurred":
        blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_strength))
        # Grayscale mask: white (255) = show original, black (0) = show blurred.
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse(
            [
                click_x - focus_radius,
                click_y - focus_radius,
                click_x + focus_radius,
                click_y + focus_radius,
            ],
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=20))
        return Image.composite(img, blurred, mask)

    if mode == "circled":
        # RGBA overlay so the translucent fill doesn't occlude what was clicked.
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        r = circle_radius
        draw.ellipse(
            [click_x - r, click_y - r, click_x + r, click_y + r],
            fill=(220, 50, 47, 45),
            outline=(220, 50, 47, 230),
            width=4,
        )
        return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    raise ValueError(f"Unknown screenshot render mode: {mode!r} (expected one of {RENDER_MODES})")


def apply_redactions(img: Image.Image, regions: list) -> Image.Image:
    """Pixelate rectangular regions (image-space [x, y, w, h]).

    Applied before any render-mode effect and in every mode, including "raw".
    """
    if not regions:
        return img
    img = img.copy()
    for region in regions:
        try:
            x, y, w, h = (int(v) for v in region)
        except (TypeError, ValueError):
            continue
        x, y = max(0, x), max(0, y)
        w = min(w, img.width - x)
        h = min(h, img.height - y)
        if w < 2 or h < 2:
            continue
        patch = img.crop((x, y, x + w, y + h))
        small = patch.resize(
            (max(1, w // _REDACT_BLOCK_PX), max(1, h // _REDACT_BLOCK_PX)),
            Image.NEAREST,
        )
        img.paste(small.resize((w, h), Image.NEAREST), (x, y))
    return img


def normalize_crop(crop, img_width: int, img_height: int) -> Optional[tuple]:
    """Clamp a stored [x, y, w, h] crop to the image; None means "no usable crop",
    which every caller treats as "nothing to apply" rather than an error."""
    if not crop:
        return None
    try:
        x, y, w, h = (int(v) for v in crop)
    except (TypeError, ValueError):
        return None
    x, y = max(0, x), max(0, y)
    w = min(w, img_width - x)
    h = min(h, img_height - y)
    if w < MIN_CROP_PX or h < MIN_CROP_PX:
        return None
    # A full-image crop is not a crop; None keeps the callers' fast path reachable.
    if x == 0 and y == 0 and w == img_width and h == img_height:
        return None
    return x, y, w, h


def apply_crop(img: Image.Image, crop) -> Image.Image:
    """Crop to a stored [x, y, w, h] region; last step of the render pipeline."""
    box = normalize_crop(crop, img.width, img.height)
    if box is None:
        return img
    x, y, w, h = box
    return img.crop((x, y, x + w, y + h))


def event_crop(event: dict) -> list:
    return event.get("data", {}).get("crop") or []


def event_note(event: dict) -> str:
    """The person's free-text note about this screenshot, or "". Not a render input."""
    return (event.get("data", {}).get("note") or "").strip()


def resolve_render_mode(session_data: dict, config) -> str:
    """Session override wins; config default otherwise; sanity-checked."""
    mode = (session_data.get("screenshot_render_mode") or getattr(config, "screenshot_mode", "circled")).strip().lower()
    return mode if mode in RENDER_MODES else "circled"


def event_is_legacy(event: dict) -> bool:
    """True for events predating raw storage: the PNG was already blurred at capture
    and carries no image-space click coords, so render modes cannot apply."""
    return "click_img_x" not in event.get("data", {})


def render_event_image(
    session_dir: Path,
    event: dict,
    mode: str,
    config,
    with_crop: bool = True,
) -> Optional[Image.Image]:
    """Load an event's PNG and apply redactions, then the render mode, then the crop.

    with_crop=False is for the editor's crop tool, which overlays the crop instead.
    """
    img_path = session_dir / event["data"]["filename"]
    if not img_path.exists():
        return None
    img = Image.open(img_path).convert("RGB")

    img = apply_redactions(img, event["data"].get("redact_regions") or [])

    if not (mode == "raw" or event_is_legacy(event)):
        img = render_image(
            img,
            event["data"]["click_img_x"],
            event["data"]["click_img_y"],
            mode,
            focus_radius=getattr(config, "focus_radius_px", 200),
            blur_strength=getattr(config, "blur_strength", 15),
            circle_radius=getattr(config, "circle_radius_px", 36),
        )

    return apply_crop(img, event_crop(event)) if with_crop else img


def render_event_to_cache(session_dir: Path, event: dict, mode: str, config) -> Optional[Path]:
    """Render to session_dir/rendered/<mode>/ and return that path, or the source
    path when there is nothing to apply. rendered/ is derived data."""
    src = session_dir / event["data"]["filename"]
    if not src.exists():
        return None

    regions = event["data"].get("redact_regions") or []
    crop = event_crop(event)
    effect_applies = not (mode == "raw" or event_is_legacy(event))
    if not effect_applies and not regions and not crop:
        return src

    # The key must cover everything that changes the output — regions, crop, and
    # the mode's own radii. Omit any and a Settings change is invisible here,
    # because the source PNG's mtime never moves.
    key_src = {"r": regions, "c": crop}
    if effect_applies:
        key_src["p"] = [
            getattr(config, "focus_radius_px", 200),
            getattr(config, "blur_strength", 15),
            getattr(config, "circle_radius_px", 36),
        ]
    key = hashlib.md5(json.dumps(key_src, sort_keys=True).encode()).hexdigest()[:8]
    name = f"{Path(event['data']['filename']).stem}_r{key}.png"
    dest = session_dir / "rendered" / mode / name
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest

    img = render_event_image(session_dir, event, mode, config)
    if img is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(dest), "PNG")
    return dest
