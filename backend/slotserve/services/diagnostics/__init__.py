"""Error pattern library: failure output -> likely cause and next steps (en and zh-CN), with the raw evidence.

Usage (any layer below the API may call it; it needs no app, database or network)::

    from slotserve.services.diagnostics import diagnose

    d = diagnose(log_text_or_lines, error="llama.cpp server exited with code 3 while loading", exit_code=3)
    d.recognized, d.id, d.localized("zh-CN").cause, d.model_dump()

See matcher.py for the rules and patterns.json for the patterns.
"""

from .matcher import (
    LOCALES,
    UNKNOWN,
    Diagnosis,
    DiagnosisText,
    catalog,
    diagnose,
    pattern_ids,
)

__all__ = ["LOCALES", "UNKNOWN", "Diagnosis", "DiagnosisText", "catalog", "diagnose", "pattern_ids"]
