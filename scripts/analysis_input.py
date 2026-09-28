"""Parse user-driven phase-two analysis input into an AnalysisSpec."""

from __future__ import annotations

import json
import uuid
from typing import Any

try:
    from .analysis_spec import (
        AVAILABLE_SECTIONS,
        AnalysisInputRequired,
        AnalysisSpec,
        parse_analysis_spec,
        validate_analysis_spec,
    )
except ImportError:  # pragma: no cover - direct script imports
    from analysis_spec import (
        AVAILABLE_SECTIONS,
        AnalysisInputRequired,
        AnalysisSpec,
        parse_analysis_spec,
        validate_analysis_spec,
    )


ALLOWED_METRICS = {
    "exposure",
    "impressions",
    "show_cnt",
    "click",
    "click_rate",
    "ctr",
    "interaction",
    "interaction_rate",
    "interact_rate",
    "cvr",
    "pvr",
    "play_3s_rate",
    "play_5s_rate",
    "play_over_rate",
    "play_duration_avg",
    "like_rate",
    "comment_rate",
    "share_rate",
    "product_wish_button_buy_cart_rate",
    "点赞指数",
    "流失指数",
    "点击指数",
    "互动指数",
    "评论指数",
}
READY_STATUSES = {"ready_for_analysis", "analysis_failed", "analysis_completed"}
_INPUT_FIELDS = {
    "analysis_id",
    "run_id",
    "sections",
    "brand_filter",
    "material_filter",
    "metric_focus",
    "business_question",
    "business_question_source",
    "user_confirmed",
    "report_mode",
    "custom_sections",
    "filters",
}


def _run_id(run_status: dict, manifest: dict) -> str | None:
    for source in (run_status, manifest):
        if not isinstance(source, dict):
            continue
        value = source.get("run_id") or source.get("runId")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_object(payload: dict | str) -> dict:
    if isinstance(payload, dict):
        return dict(payload)
    if not isinstance(payload, str) or not payload.strip():
        raise AnalysisInputRequired(
            "business_question is required; please provide an analysis question"
        )
    text = payload.strip()
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("analysis input JSON is invalid") from exc
        if not isinstance(decoded, dict):
            raise ValueError("analysis input JSON must be an object")
        return decoded
    return {"business_question": text}


def parse_analysis_input(payload: dict | str, run_status: dict, manifest: dict) -> AnalysisSpec:
    """Convert natural language or structured phase-two input to AnalysisSpec."""
    raw = _payload_object(payload)
    unexpected = set(raw) - _INPUT_FIELDS
    if unexpected:
        raise ValueError("unsupported analysis input fields: " + ", ".join(sorted(unexpected)))

    filters = raw.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    filters = dict(filters)
    if "material_filter" in raw:
        material_filter = raw["material_filter"]
        if not isinstance(material_filter, dict):
            raise ValueError("material_filter must be an object")
        filters.update(material_filter)
    if "brand_filter" in raw:
        top_level_brands = raw["brand_filter"]
        if "brand_filter" in filters and filters["brand_filter"] != top_level_brands:
            raise ValueError("brand_filter is specified twice with different values")
        filters["brand_filter"] = top_level_brands

    question = raw.get("business_question")
    question_source = raw.get("business_question_source", "user")
    user_confirmed = raw.get("user_confirmed", question_source == "user")
    # Defaults follow the persisted capabilities; explicit requests still fail
    # validation when the requested package is unavailable.
    sections = raw.get("sections", [section for section in AVAILABLE_SECTIONS
                                    if section in manifest.get("packages", {})])
    ranking = manifest.get("packages", {}).get("material_ranking", {})
    count = ranking.get("record_count") if isinstance(ranking, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        filters.setdefault("rank_range", [1, min(count, 1000)])
    resolved_run_id = raw.get("run_id") or _run_id(run_status, manifest)
    analysis_id = raw.get("analysis_id") or f"analysis-{uuid.uuid4().hex}"
    return parse_analysis_spec({
        "analysis_id": analysis_id,
        "run_id": resolved_run_id,
        "sections": sections,
        "business_question": question,
        "business_question_source": question_source,
        "user_confirmed": user_confirmed,
        "metric_focus": raw.get("metric_focus", []),
        "filters": filters,
        "report_mode": raw.get("report_mode", "standard"),
        "custom_sections": raw.get("custom_sections", []),
    })


def _status_value(run_status: dict) -> str | None:
    if not isinstance(run_status, dict):
        return None
    nested = run_status.get("workflow")
    return run_status.get("status") or run_status.get("stage") or (
        nested.get("stage") if isinstance(nested, dict) else None
    )


def _manifest_material_ids(manifest: dict, sections: tuple[str, ...]) -> set[str]:
    result: set[str] = set()
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    if not isinstance(packages, dict):
        return result
    for section in sections:
        package = packages.get(section)
        if not isinstance(package, dict):
            continue
        ids = package.get("material_ids")
        if isinstance(ids, list):
            result.update(str(item) for item in ids if str(item).strip())
    return result


def validate_analysis_input(spec: AnalysisSpec, run_status: dict, manifest: dict) -> list[str]:
    """Return all phase-two gate errors without reading or changing source data."""
    errors: list[str] = []
    status = _status_value(run_status)
    if status not in READY_STATUSES:
        errors.append("run must be ready_for_analysis before analysis can start")

    errors.extend(validate_analysis_spec(spec, manifest))
    if not isinstance(spec, AnalysisSpec):
        return errors

    manifest_run_id = manifest.get("run_id") or manifest.get("runId") if isinstance(manifest, dict) else None
    status_run_id = _run_id(run_status, {})
    if status_run_id and spec.run_id and spec.run_id != status_run_id:
        errors.append(f"run_id does not match workflow status: {spec.run_id} != {status_run_id}")
    if manifest_run_id and spec.run_id and spec.run_id != manifest_run_id:
        errors.append(f"run_id does not match facts manifest: {spec.run_id} != {manifest_run_id}")

    invalid_metrics = sorted(set(spec.metric_focus) - ALLOWED_METRICS)
    if invalid_metrics:
        errors.append("metric_focus contains unsupported metrics: " + ", ".join(invalid_metrics))

    material_ids = tuple(spec.filters.get("material_ids", ()))
    invalid_id_format = sorted(item for item in material_ids if not item.isdigit())
    if invalid_id_format:
        errors.append("material_ids must contain digits only: " + ", ".join(invalid_id_format))
    available_ids = _manifest_material_ids(manifest, spec.sections)
    if available_ids:
        unknown_ids = sorted(set(material_ids) - available_ids)
        if unknown_ids:
            errors.append("material_ids are outside selected fact packages: " + ", ".join(unknown_ids))
    return errors
