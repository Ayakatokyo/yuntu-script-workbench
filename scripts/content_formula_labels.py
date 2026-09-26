"""Resolve formula display names without using the catalog as a data gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


_DISPLAY_ALIAS_PATH = Path(__file__).resolve().parents[1] / "config/yuntu-content-formula-taxonomy-v1.json"


def _display_aliases() -> dict[str, dict[str, str]]:
    """Load optional known display aliases; unknown source IDs remain valid."""
    try:
        payload = json.loads(_DISPLAY_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"primary": {}, "secondary": {}}
    return {
        level: {
            str(key): value.strip()
            for key, value in (payload.get(level) or {}).items()
            if isinstance(value, str) and value.strip()
        }
        for level in ("primary", "secondary")
    }


def formula_detail_labels(formula: Mapping[str, Any]) -> dict[str, list[str]]:
    """Collect concrete names attached to formula IDs in source detail blocks."""
    values: dict[str, list[str]] = {}
    sources = formula.get("sources") if isinstance(formula, Mapping) else None
    if not isinstance(sources, Mapping):
        return values
    for source in sources.values():
        details = source.get("detail") if isinstance(source, Mapping) else None
        if not isinstance(details, Mapping):
            continue
        for raw_id, raw_terms in details.items():
            terms = raw_terms if isinstance(raw_terms, list) else [raw_terms]
            bucket = values.setdefault(str(raw_id), [])
            for term in terms:
                if not isinstance(term, str) or not term.strip():
                    continue
                value = term.strip()
                if value not in bucket:
                    bucket.append(value)
    return values


def resolve_formula_ref(
    value: object,
    level: str,
    detail_labels: Mapping[str, list[str]] | None = None,
) -> tuple[str | None, str | None]:
    """Return an ID and display name, preserving unknown source IDs."""
    if isinstance(value, Mapping):
        ref_id = value.get("id")
        source_label = value.get("label")
        if type(ref_id) is int and isinstance(source_label, str) and source_label.strip():
            return str(ref_id), source_label.strip()
        return None, None
    if type(value) is not int:
        return None, None
    ref_id = str(value)
    aliases = _display_aliases().get(level, {})
    if ref_id in aliases:
        return ref_id, aliases[ref_id]
    if level == "secondary" and detail_labels:
        terms = detail_labels.get(ref_id) or []
        if terms:
            return ref_id, " / ".join(terms[:3])
    return ref_id, None
