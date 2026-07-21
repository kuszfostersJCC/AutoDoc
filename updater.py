"""
updater.py — Manual "Check for Updates" against GitHub Releases.

Deliberately manual, not automatic: users decide per-release whether they
want whatever changed (see Settings tab). There is no background polling
and no silent auto-install.

WHY A DETACHED SCRIPT DOES THE ACTUAL FILE SWAP (read before "simplifying"):
While AutoDoc.exe is running, Windows holds an open handle on the exe itself
and every DLL/pyd under _internal/ — nothing, including AutoDoc's own code,
can overwrite those files in place. The only way to update them is to hand
off to a SEPARATE process, exit AutoDoc so its locks are released, let that
process replace the files, then relaunch. build_update_script() writes a
small detached .bat that does exactly that; launch_update_and_exit() spawns
it and the caller (Settings tab) is responsible for quitting the app
immediately afterward — see sessions_window/settings_panel.py's
_apply_update().

WHY PLAIN ROBOCOPY, NOT /MIR: the release zip (see scripts/make_release.py)
never contains config.json, models/, sessions/, output/, or exports/ — only
code (AutoDoc.exe, _internal/, assets/). A plain (non-mirroring) robocopy
therefore only ever ADDS/OVERWRITES files that exist in the new release; it
never deletes anything, so there's no exclusion list to keep in sync and no
way for an update to eat a user's config or recordings. The one accepted
cost: a file removed between versions (e.g. a dropped DLL) is left behind as
harmless orphaned disk usage — same tradeoff already made for orphaned
imported-screenshot files (see recorder/session.py::import_screenshots).

No new dependency: plain stdlib urllib for the GitHub API + download,
consistent with generation/backends/local_vision_backend.py. Extraction
shells out to PowerShell's Expand-Archive (built into Windows 10+) from the
generated .bat rather than pulling Python's zipfile into the "runs after
AutoDoc has already exited" script — keeps that script a single flat file
with no Python interpreter dependency at all.
"""

import json
import logging
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import get_app_dir
from version import VERSION

log = logging.getLogger(__name__)

GITHUB_OWNER = "kuszfostersJCC"
GITHUB_REPO = "AutoDoc"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_REQUEST_TIMEOUT_S = 10
_DOWNLOAD_TIMEOUT_S = 180

_DOWNLOAD_DIRNAME = "_update_download"
_STAGING_DIRNAME = "_update_staging"
_SCRIPT_NAME = "_apply_update.bat"


@dataclass
class UpdateInfo:
    version: str          # e.g. "1.2.0" — leading 'v' already stripped
    notes: str             # release body (changelog), shown verbatim to the user
    download_url: str      # zip asset's browser_download_url
    asset_name: str
    asset_size: int         # bytes, for a "~12 MB" hint in the UI


class UpdateCheckError(Exception):
    """
    Network/parse failure while checking for or downloading an update.
    Always caught by the caller and shown as a friendly status message —
    GitHub being unreachable (offline machine, district network blocking
    github.com, GitHub itself being down) must never be treated as "no
    update available".
    """


def _parse_version(v: str) -> tuple:
    """'v1.10.2' -> (1, 10, 2). Non-numeric trailing junk (e.g. '-beta') is
    dropped from that segment rather than raising, so a release note that
    breaks strict semver still compares as best-effort."""
    v = v.strip().lstrip("vV")
    parts = []
    for segment in v.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str = VERSION) -> bool:
    return _parse_version(remote) > _parse_version(local)


def check_for_update() -> Optional[UpdateInfo]:
    """
    Query GitHub's public "latest release" endpoint (no auth — the repo is
    public, and this keeps AutoDoc.exe from ever needing a token baked in).
    Returns None when already on the latest version. Raises UpdateCheckError
    on any failure to reach/parse the API.
    """
    request = urllib.request.Request(
        _RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AutoDoc-Updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(
            f"GitHub returned HTTP {exc.code} checking for updates."
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise UpdateCheckError(
            f"Could not reach GitHub to check for updates: {exc}"
        ) from exc

    tag = (body.get("tag_name") or "").strip()
    if not tag:
        raise UpdateCheckError("GitHub's latest release has no tag name.")

    if not is_newer(tag):
        return None

    assets = body.get("assets") or []
    zip_asset = next(
        (a for a in assets if (a.get("name") or "").lower().endswith(".zip")),
        None,
    )
    if zip_asset is None:
        raise UpdateCheckError(
            f"Release {tag} has no .zip file attached — nothing to install."
        )

    return UpdateInfo(
        version=tag.lstrip("vV"),
        notes=(body.get("body") or "").strip() or "(No release notes provided.)",
        download_url=zip_asset["browser_download_url"],
        asset_name=zip_asset["name"],
        asset_size=int(zip_asset.get("size") or 0),
    )


def download_update(info: UpdateInfo) -> Path:
    """
    Download the release zip into a scratch folder beside the exe. Returns
    the local zip path. Raises UpdateCheckError on failure; any partial file
    is removed first.
    """
    dest_dir = get_app_dir() / _DOWNLOAD_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / info.asset_name

    request = urllib.request.Request(
        info.download_url, headers={"User-Agent": "AutoDoc-Updater"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        zip_path.unlink(missing_ok=True)
        raise UpdateCheckError(f"Update download failed: {exc}") from exc
    return zip_path


def build_update_script(zip_path: Path, app_dir: Path, exe_name: str = "AutoDoc.exe") -> Path:
    """
    Write the detached .bat that performs the actual update after AutoDoc
    exits: extract the downloaded zip, robocopy it over app_dir (adds/
    overwrites only — see module docstring for why this is deliberately not
    /MIR), relaunch the exe, then delete its own downloaded artifacts
    including itself.
    """
    staging = app_dir / _STAGING_DIRNAME
    script_path = app_dir / _SCRIPT_NAME
    log_path = app_dir / "autodoc_update.log"

    script = f"""@echo off
REM Auto-generated by AutoDoc's "Check for Updates" — safe to delete if found
REM stray (it deletes itself on completion; only present mid-update or after
REM an update was interrupted, e.g. the machine losing power).
echo === AutoDoc update started %date% %time% === > "{log_path}"

REM Give AutoDoc.exe a moment to fully release its file handles after exit.
timeout /t 2 /nobreak >nul

if exist "{staging}" rmdir /s /q "{staging}"
mkdir "{staging}"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -Path '{zip_path}' -DestinationPath '{staging}'" >> "{log_path}" 2>&1
if errorlevel 1 (
    echo Extraction failed - aborting update, leaving old install untouched. >> "{log_path}"
    goto :relaunch
)

REM /E = copy all subfolders incl. empty ones. No /MIR: never deletes
REM anything in app_dir, so config.json/sessions/output/exports/models are
REM untouched purely because the zip never contains them. /R:5 /W:2 gives
REM a freshly-written exe/DLL time to clear antivirus scanning — this app's
REM global mouse hooks are already a known false-positive trigger (see
REM autodoc.spec's build notes), so the first seconds after extraction are
REM exactly when a lock is most likely.
robocopy "{staging}" "{app_dir}" /E /R:5 /W:2 >> "{log_path}" 2>&1

rmdir /s /q "{staging}" 2>nul
del "{zip_path}" 2>nul

:relaunch
echo === relaunching AutoDoc === >> "{log_path}"
start "" "{app_dir / exe_name}"
del "%~f0"
"""
    script_path.write_text(script, encoding="utf-8")
    return script_path


def launch_update_and_exit(zip_path: Path) -> None:
    """
    Spawn the detached updater script. Does NOT quit AutoDoc itself — the
    caller (Settings tab) must call the app's quit routine immediately
    afterward so the script's 2s wait is enough. Nothing after that quit
    call should assume this process is still alive.
    """
    app_dir = get_app_dir()
    script_path = build_update_script(zip_path, app_dir)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["cmd.exe", "/c", str(script_path)],
        cwd=str(app_dir),
        creationflags=creationflags,
        close_fds=True,
    )
    log.info("updater: spawned %s for release download at %s", script_path, zip_path)
