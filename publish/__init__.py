"""
publish/ — destinations for generated documentation.

Sits beside generation/, not inside it: this package consumes generation output
but contains no generation logic, which keeps the property that recorder/,
transcription/, and generation/ import no UI and no publishing code.

Nothing here touches Qt or ui_service. Callers run it on a worker thread and own
all progress reporting (invariant #1 in HANDOFF.md).
"""
