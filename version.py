"""
version.py — Single source of truth for AutoDoc's version string.

Bump this AND tag the matching commit (git tag v1.2.0 && git push --tags)
before drafting a GitHub release — updater.py compares this value against
the latest release's tag_name to decide whether "Check for Updates" has
anything to offer. See scripts/make_release.py for the packaging step and
HANDOFF.md for the full release process.
"""

VERSION = "1.3.2"
