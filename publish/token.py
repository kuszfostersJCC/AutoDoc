"""
publish/token.py — locate and decode the shared BookStack bot token.

The token is deliberately NOT a config.json key. config.json is rewritten in
full from memory every time the Settings tab saves (invariant #9), so a key
present in the file but absent from write_config()'s dict is silently deleted the
first time a user changes an unrelated setting. For a shared credential that
reads to the user as "publishing worked yesterday and today it doesn't", with no
error and no log line. A separate file cannot be clobbered that way.

FILE FORMAT

A BookStack API credential is a PAIR — a Token ID and a Token Secret — and both
are required. Three accepted forms, in the order they're tried:

  1. Key/value lines (recommended — impossible to get wrong):
         token_id = abc123
         token_secret = def456
  2. One line "<token_id>:<token_secret>".
  3. One line of base64 that decodes to form 2.

Blank lines and lines starting with # are ignored, so the file can carry
instructions. Form 3 is obfuscation, not protection — it exists only so the
credential isn't readable at a glance, which is all that was asked for; anyone who
wants it can trivially decode it, and that's an accepted position for this
deployment (see HANDOFF.md).

Forms 1 and 2 exist because form 3 alone was a mistake: it required running a
helper to produce the value, and the first real attempt at using it pasted a
single 32-char half of the pair. A credential format that needs a tool to write
correctly will be written incorrectly. All three forms fail SAFE (publishing
disabled, specific reason logged) rather than sending a malformed Authorization
header.

WHERE IT LIVES
  Frozen : _internal/bookstack.token, resolved sys._MEIPASS-relative — same
           mechanism and same reasoning as theme/ (see theme/loader.py's
           _theme_dir). This location matters: _internal/ is exactly what the
           updater's non-mirroring robocopy overwrites, so a token shipped in a
           release zip reaches every machine on "Check for Updates" with no USB
           pass. It also means a manually-placed token SURVIVES an update
           (non-mirroring copies don't delete files absent from the zip), so
           dropping one in by hand is a valid deployment too.
  Source : project root, beside config.json.

  Gitignored in both cases — the repo is public.

A missing or malformed token is NOT an error. It disables publishing and logs
once. Rationale: a dev checkout and a fresh build both legitimately lack it, and
crashing the app over an optional publishing destination would be worse than the
feature being unavailable.
"""

import base64
import binascii
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN_FILENAME = "bookstack.token"


def token_path() -> Path:
    """Where the token file is expected, for source and frozen runs."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / TOKEN_FILENAME
    return Path(__file__).resolve().parent.parent / TOKEN_FILENAME


def load_token() -> tuple[str, str] | None:
    """
    Return (token_id, token_secret), or None if unavailable for any reason.

    Never raises. Every failure path logs at WARNING with the resolved path, so
    "publishing silently does nothing" is always diagnosable from the log.
    """
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
        # Never logs either value, only which half is missing.
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
    """Form 1: token_id / token_secret assignment lines. None if not that form."""
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
    """Form 2 (plain "id:secret") or form 3 (base64 of the same)."""
    if ":" in payload:
        token_id, _, secret = payload.partition(":")
        return token_id.strip(), secret.strip()

    # No colon: the only remaining valid form is base64. Note a bare 32-char
    # BookStack token half is often ITSELF valid base64, so a successful decode
    # proves nothing — the colon check below is what actually validates it.
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
    """
    Produce the obfuscated (form 3) single-line value from a token pair.

    Optional — the key/value form is just as valid and needs no tooling. Kept
    because it's the only way to write form 3 without getting the separator or a
    trailing newline wrong:

        python -c "from publish.token import encode_token; \
                   print(encode_token('ID','SECRET'))"
    """
    return base64.b64encode(f"{token_id}:{secret}".encode("utf-8")).decode("ascii")
