"""Packages dist/AutoDoc/ into releases/AutoDoc-v<VERSION>.zip for the in-app updater.

Built from the working tree's dist/, never from a git tag; config.json, models/,
sessions/ and output/ are stripped.
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from version import VERSION  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist" / "AutoDoc"
RELEASES_DIR = PROJECT_ROOT / "releases"

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
