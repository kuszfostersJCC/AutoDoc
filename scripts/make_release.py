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
Every install already has its own config.json (the AI server's metering key /
local_endpoint — never meant to be public, and the repo is public: this
exclusion is the only thing keeping the doc-generator key out of a GitHub
release asset) and its own cached Whisper model, and possibly
its own recorded sessions if this dist/ folder has ever been run as a working
install rather than a fresh build output. updater.py's apply step uses a
PLAIN (non-mirroring) robocopy specifically so it only ever adds/overwrites
files present in the release zip — leaving those paths out here is what
makes that safe. If any of this ends up in a public release asset, it can't
be un-published from GitHub after the fact, so this list is deliberately
generous rather than minimal.

After this script runs:
    1. git tag v<version> && git push origin v<version>
    2. gh release create v<version> releases/AutoDoc-v<version>.zip \\
           --title "v<version>" --notes "<what changed, for users to read>"
       (or do the same by hand: GitHub -> Releases -> Draft a new release ->
       attach the zip, same tag)
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
    print(f"  git tag v{VERSION} && git push origin v{VERSION}")
    print(f'  gh release create v{VERSION} "{zip_path}" --title "v{VERSION}" --notes "..."')


if __name__ == "__main__":
    main()
