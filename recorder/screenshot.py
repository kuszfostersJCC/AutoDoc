"""
recorder/screenshot.py — Screen capture (raw) + on-demand render effects.

ARCHITECTURE (changed 2026-07-08):
Screenshots are captured and stored RAW. Privacy blur / click-circle effects
are applied at RENDER time — editor preview, export, and generation — never
destructively at capture. This is what makes the effect selectable and
reversible per-session in the editor after the recording exists.

Consequence the admin accepted deliberately: the unmodified screen content
sits on disk in the session folder. The pre-2026-07-08 behaviour (destructive
blur at capture) protected the disk copy; the new default direction is local
model generation where images never leave the machine, making the disk copy
the same trust boundary as the machine itself.

Render modes ("screenshot_mode" in config.json; per-session override
"screenshot_render_mode" in session.json):
    "raw"     — untouched capture
    "blurred" — Gaussian blur everywhere except a clear circle at the click
    "circled" — untouched capture + a light red circle marking the click.
                Not just for humans: vision models are demonstrably steered
                by drawn marks (red-circle visual prompting, set-of-mark).

Click coordinates: events store click_img_x/click_img_y — the click position
in the SAVED image's pixel space (post-downscale) — so effects can be
re-rendered at any time. Events without these keys are legacy sessions whose
PNGs were already blurred at capture; render_event_image() returns those
as-is regardless of mode.

Multi-monitor: only the monitor containing the click is captured.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

import mss
from PIL import Image, ImageDraw, ImageFilter

RENDER_MODES = ("raw", "blurred", "circled")

# Pixelation block size for manual redaction regions (PRD F3). Mosaic, not
# Gaussian blur: strong Gaussian blurs of short text are partially invertible;
# 16px mosaic blocks are not, at screenshot resolutions.
_REDACT_BLOCK_PX = 16


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


# ------------------------------------------------------------------ #
# Capture (raw)                                                        #
# ------------------------------------------------------------------ #

def capture_raw(x: int, y: int) -> Tuple[Image.Image, int, int]:
    """
    Capture the monitor containing (x, y) with no effects applied.

    Args:
        x, y: Click coordinates in Windows virtual screen space.

    Returns:
        (image, img_x, img_y) — the raw RGB capture and the click position
        translated into that image's pixel coordinates.
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
    """
    Downscale (if needed) and save a RAW screenshot as PNG.

    Downsampling reduces file size and vision-model token cost. The click
    coordinates are scaled by the same ratio so later renders line up.

    Returns:
        (click_img_x, click_img_y) — click position in the SAVED image's
        pixel space. Persist these on the event; every render needs them.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if img.width > max_dimension or img.height > max_dimension:
        ratio = max_dimension / max(img.width, img.height)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        click_x, click_y = int(click_x * ratio), int(click_y * ratio)

    img.save(str(dest_path), "PNG")
    return click_x, click_y


# ------------------------------------------------------------------ #
# Rendering (applied on demand, never destructive)                    #
# ------------------------------------------------------------------ #

def render_image(
    img: Image.Image,
    click_x: int,
    click_y: int,
    mode: str,
    focus_radius: int = 200,
    blur_strength: int = 15,
    circle_radius: int = 36,
) -> Image.Image:
    """
    Apply a render mode to a raw screenshot. Coordinates are in the given
    image's pixel space. Returns a new image ("raw" returns the input).
    """
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
        # Feather the circle edge for a natural vignette effect.
        mask = mask.filter(ImageFilter.GaussianBlur(radius=20))
        return Image.composite(img, blurred, mask)

    if mode == "circled":
        # Light red highlight: translucent fill + solid outline. Drawn on an
        # RGBA overlay so the fill doesn't fully occlude what was clicked.
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
    """
    Pixelate rectangular regions (image-space [x, y, w, h] lists — PRD F3).

    Applied BEFORE any render-mode effect and in EVERY mode including "raw":
    a redaction is a privacy decision, not a display preference. The raw PNG
    on disk is untouched — redaction is render-time, like everything else in
    this pipeline — so a region can always be removed or adjusted later.
    """
    if not regions:
        return img
    img = img.copy()
    for region in regions:
        try:
            x, y, w, h = (int(v) for v in region)
        except (TypeError, ValueError):
            continue
        # Clamp to image bounds; skip degenerate boxes.
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


def resolve_render_mode(session_data: dict, config) -> str:
    """Session override wins; config default otherwise; sanity-checked."""
    mode = (session_data.get("screenshot_render_mode") or getattr(config, "screenshot_mode", "blurred")).strip().lower()
    return mode if mode in RENDER_MODES else "blurred"


def event_is_legacy(event: dict) -> bool:
    """
    True if this screenshot event predates raw storage (its PNG was already
    blurred destructively at capture and stores no image-space click coords).
    Legacy images are shown/exported as-is; render modes can't apply.
    """
    return "click_img_x" not in event.get("data", {})


def render_event_image(session_dir: Path, event: dict, mode: str, config) -> Optional[Image.Image]:
    """
    Load a screenshot event's PNG, apply redaction regions, then the render
    mode. Returns None if the file is missing. Legacy events skip the mode
    effect (already blurred at capture) but still get redactions.
    """
    img_path = session_dir / event["data"]["filename"]
    if not img_path.exists():
        return None
    img = Image.open(img_path).convert("RGB")

    # Redactions first — they must survive every mode, including raw.
    img = apply_redactions(img, event["data"].get("redact_regions") or [])

    if mode == "raw" or event_is_legacy(event):
        return img
    return render_image(
        img,
        event["data"]["click_img_x"],
        event["data"]["click_img_y"],
        mode,
        focus_radius=getattr(config, "focus_radius_px", 200),
        blur_strength=getattr(config, "blur_strength", 15),
        circle_radius=getattr(config, "circle_radius_px", 36),
    )


def render_event_to_cache(session_dir: Path, event: dict, mode: str, config) -> Optional[Path]:
    """
    Render an event's screenshot to session_dir/rendered/<mode>/... and return
    that path. Returns the original path only when there is truly nothing to
    apply (raw/legacy AND no redaction regions). Cached renders are reused if
    newer than the source PNG; the cache filename includes a hash of the
    redaction regions, because editing regions changes the render without
    touching the source file's mtime.

    Used by generation and export, which need file paths rather than PIL
    images. The rendered/ folder is derived data — safe to delete anytime.
    """
    src = session_dir / event["data"]["filename"]
    if not src.exists():
        return None

    regions = event["data"].get("redact_regions") or []
    nothing_to_apply = (mode == "raw" or event_is_legacy(event)) and not regions
    if nothing_to_apply:
        return src

    name = Path(event["data"]["filename"]).name
    if regions:
        key = hashlib.md5(json.dumps(regions, sort_keys=True).encode()).hexdigest()[:8]
        name = f"{Path(name).stem}_r{key}.png"
    dest = session_dir / "rendered" / mode / name
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest

    img = render_event_image(session_dir, event, mode, config)
    if img is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(dest), "PNG")
    return dest
