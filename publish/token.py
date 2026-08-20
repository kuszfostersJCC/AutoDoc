"""Locates and decodes the shared BookStack bot token file."""

import base64
import binascii
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN_FILENAME = "bookstack.token"


def token_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / TOKEN_FILENAME
    return Path(__file__).resolve().parent.parent / TOKEN_FILENAME


def load_token() -> tuple[str, str] | None:
    """Returns (token_id, token_secret), or None. Never raises."""
    path = token_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("bookstack: no token file at %s — publishing disabled", path)
        return None
    except OSError as exc:
        log.warning("bookstack: could not read %s (%s) — publishing disabled", path, exc)
        return None

    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        log.warning("bookstack: %s has no token in it — publishing disabled", path)
        return None

    pair = _parse_key_values(lines) or _parse_single_line(lines[0], path)
    if pair is None:
        return None

    token_id, secret = pair
    if not token_id or not secret:
        missing = "token_id" if not token_id else "token_secret"
        log.warning(
            "bookstack: %s is missing %s — a BookStack credential needs BOTH the "
            "Token ID and the Token Secret. Publishing disabled.",
            path,
            missing,
        )
        return None
    return token_id, secret


def _parse_key_values(lines: list[str]) -> tuple[str, str] | None:
    if not any("=" in line for line in lines):
        return None
    found: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        found[key.strip().lower().replace("-", "_")] = value.strip().strip("\"'")
    if "token_id" in found or "token_secret" in found:
        return found.get("token_id", ""), found.get("token_secret", "")
    return None


def _parse_single_line(payload: str, path: Path) -> tuple[str, str] | None:
    if ":" in payload:
        token_id, _, secret = payload.partition(":")
        return token_id.strip(), secret.strip()

    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8").strip()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        log.warning(
            "bookstack: %s doesn't contain a usable token. Expected either "
            "'token_id = ...' and 'token_secret = ...' lines, or a single "
            "'<token_id>:<token_secret>' line. Publishing disabled.",
            path,
        )
        return None

    if ":" not in decoded:
        log.warning(
            "bookstack: the value in %s decoded but contains no ':' separator — "
            "this looks like only ONE half of the credential. BookStack needs both "
            "the Token ID and the Token Secret. Publishing disabled.",
            path,
        )
        return None
    token_id, _, secret = decoded.partition(":")
    return token_id.strip(), secret.strip()


def encode_token(token_id: str, secret: str) -> str:
    return base64.b64encode(f"{token_id}:{secret}".encode("utf-8")).decode("ascii")
