"""BookStack tag vocabulary. TODO: provisional placeholder, not yet signed off."""

import logging

log = logging.getLogger(__name__)

DOC_TYPE = ("sop", "policy", "troubleshooting", "reference", "meeting-notes")
STATUS = ("draft", "review", "published", "deprecated")
OWNER = ("it-ops", "registrar", "human-resources", "finance", "facilities")
AUDIENCE = ("all-staff", "faculty", "admins", "leadership")
REVIEW_CYCLE = ("quarterly", "annual", "ad-hoc")

# Keys are tag names spelled exactly as BookStack stores them.
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
    """Build the tags payload; invalid values are dropped, empty ones skipped."""
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
