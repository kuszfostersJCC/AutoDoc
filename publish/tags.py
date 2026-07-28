"""
publish/tags.py — JCC's controlled tag vocabulary.

Source of truth: tagging-best-practices.md. PROVISIONAL — Foster is still
deciding what the tagging should actually look like for AutoDoc pages, so treat
this as a placeholder that is easy to purge or rewrite. It exists now only so
pages arrive tagged rather than untagged.

Two rules that should survive whatever the vocabulary becomes:

1. **Values are validated against the enum, never passed through.** A controlled
   vocabulary's whole value is that filters partition cleanly. One `it_ops` or
   `IT-Ops` splits a filter in two and does so invisibly — nobody notices until a
   shelf is quietly missing pages. An unrecognised value is dropped with a log
   line.
2. **No model-generated values.** Also note the source doc's own Rule 2 ("don't
   repeat the title") rules out keyword and topic tags entirely, and Owner /
   Audience are facts about the org chart rather than about the document, so
   there is far less here that could be inferred than it first appears.

Status is forced to `draft` on publish and is not configurable: AutoDoc output is
a first draft of an AI-generated document that nobody has necessarily read, and
`published` means "active and accurate" per the source doc. Promotion happens in
BookStack, by a human.
"""

import logging

log = logging.getLogger(__name__)

DOC_TYPE = ("sop", "policy", "troubleshooting", "reference", "meeting-notes")
STATUS = ("draft", "review", "published", "deprecated")
OWNER = ("it-ops", "registrar", "human-resources", "finance", "facilities")
AUDIENCE = ("all-staff", "faculty", "admins", "leadership")
REVIEW_CYCLE = ("quarterly", "annual", "ad-hoc")

# Tag NAMES exactly as they appear in BookStack, mapped to their allowed values.
VOCABULARY = {
    "Doc-Type": DOC_TYPE,
    "Status": STATUS,
    "Owner": OWNER,
    "Audience": AUDIENCE,
    "Review-Cycle": REVIEW_CYCLE,
}

PUBLISH_STATUS = "draft"


def _valid(name: str, value: str) -> bool:
    allowed = VOCABULARY.get(name)
    if allowed is None:
        log.warning("bookstack tags: unknown tag name %r — dropped", name)
        return False
    if value not in allowed:
        log.warning(
            "bookstack tags: %r is not a valid %s value (allowed: %s) — dropped",
            value,
            name,
            ", ".join(allowed),
        )
        return False
    return True


def build_tags(
    doc_type: str = "sop",
    owner: str = "it-ops",
    audience: str = "all-staff",
    review_cycle: str = "ad-hoc",
) -> list[dict]:
    """
    Build the BookStack tags payload: [{"name": ..., "value": ...}, ...].

    Invalid values are dropped rather than corrected or passed through, so a bad
    config value costs one tag, not a split filter. An empty string skips the tag
    entirely without a warning — that's how "don't tag this dimension" is
    expressed in config.
    """
    candidates = [
        ("Doc-Type", doc_type),
        ("Status", PUBLISH_STATUS),
        ("Owner", owner),
        ("Audience", audience),
        ("Review-Cycle", review_cycle),
    ]
    return [
        {"name": name, "value": value}
        for name, value in candidates
        if value and _valid(name, value)
    ]
