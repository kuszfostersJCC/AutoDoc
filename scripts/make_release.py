"""
scripts/make_release.py — Package dist/AutoDoc/ into a GitHub-release-ready
zip for the in-app "Check for Updates" feature (see updater.py at the
project root).

Usage — run AFTER `pyinstaller autodoc.spec` has produced dist/AutoDoc/:

    python scripts/make_release.py

Produces releases/AutoDoc-v<version>.zip (version comes from version.py's
VERSION constant — bump that first, don't pass it separately, or the shipped
zip and the tag you create for it will disagree).

WHY THIS EXCLUDES config.json, models/, sessions/, output/, exports/:
Every install has its own config.json with its own per-machine values, its own
cached Whisper model, and possibly its own recorded sessions if this dist/ folder
has ever been run as a working install rather than a fresh build output.
updater.py's apply step uses a PLAIN (non-mirroring) robocopy specifically so it
only ever adds/overwrites files present in the release zip — leaving those paths
out here is what keeps an update from clobbering any of it.

`sessions/`, `output/` and `exports/` are the ones that genuinely must never
escape: they can contain student data, and a public release asset cannot be
un-published from GitHub after the fact. This list is deliberately generous
rather than minimal for that reason. config.json is excluded to protect
per-machine settings, NOT to hide the AI server's metering key — that key ships
in config.py's defaults by design (see HANDOFF.md, "The key is in the public repo
on purpose").

THIS SCRIPT ZIPS THE WORKING TREE'S BUILD OUTPUT. GIT IS NEVER CONSULTED.
Two consequences, and the second one bites:

  * A branch or tag mistake cannot ship wrong code to users — the bytes come
    from whatever `pyinstaller` last built locally. (v1.3.2 was tagged on `dev`
    without merging to `main`; users were unaffected. See HANDOFF.md.)
  * The zip therefore reflects UNCOMMITTED edits too. A release can contain code
    that exists in no commit anywhere, and nothing here can detect that. Run
    `git status` before `pyinstaller` — that is the only safeguard.

The "Next steps" this prints deliberately includes the merge to `main`, because
this output is the checklist that actually gets followed, and the merge is the
step that got skipped when it lived only in HANDOFF.md.
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from version import VERSION  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist" / "AutoDoc"
RELEASES_DIR = PROJECT_ROOT / "releases"

# Never ship these, even if present in dist/AutoDoc/ (see module docstring).
_EXCLUDE_DIRS = {"models", "sessions", "output", "exports", "_update_download", "_update_staging"}
_EXCLUDE_FILES = {"config.json", "autodoc.log", "autodoc_update.log", "_apply_update.bat"}


def main() -> None:
    if not DIST_DIR.exists():
        print(f"ERROR: {DIST_DIR} not found — run `pyinstaller autodoc.spec` first.")
        sys.exit(1)

    RELEASES_DIR.mkdir(exist_ok=True)
    zip_path = RELEASES_DIR / f"AutoDoc-v{VERSION}.zip"
    if zip_path.exists():
        print(f"ERROR: {zip_path} already exists — bump VERSION in version.py first.")
        sys.exit(1)

    included = 0
    skipped_dirs = set()
    skipped_files = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in DIST_DIR.rglob("*"):
            rel = path.relative_to(DIST_DIR)
            if path.is_dir():
                continue
            top = rel.parts[0]
            if top in _EXCLUDE_DIRS:
                skipped_dirs.add(top)
                continue
            if rel.name in _EXCLUDE_FILES and len(rel.parts) == 1:
                skipped_files.append(str(rel))
                continue
            zf.write(path, rel)
            included += 1

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Wrote {zip_path} ({size_mb:.1f} MB, {included} files).")
    if skipped_dirs:
        print(f"Excluded directories: {', '.join(sorted(skipped_dirs))}")
    if skipped_files:
        print(f"Excluded files: {', '.join(skipped_files)}")
    print()
    print("Next steps:")
    print("  0. Confirm this zip matches a commit — `git status` should be clean.")
    print("     This zip came from dist/, not from git; uncommitted edits are IN it.")
    print("  1. Merge dev -> main, and tag the resulting main commit.")
    print("     Skipping this doesn't affect users, but it leaves main (and the")
    print("     README that GitHub shows) behind what's deployed. See HANDOFF.md,")
    print("     'Release process'.")
    print(f"  2. git tag v{VERSION} && git push origin v{VERSION}")
    print(
        f'  3. gh release create v{VERSION} "{zip_path}" '
        f'--title "v{VERSION}" --notes "..."'
    )
    print("     Notes are what the in-app update dialog shows users — write them")
    print("     for a non-technical reader.")


if __name__ == "__main__":
    main()
