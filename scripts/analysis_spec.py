"""Analysis specification parsing and manifest-level validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


AVAILABLE_SECTIONS = (
    "material_ranking",
    "audience",
    "script",
    "highlight_frames",
    "content_tags",
    "interaction_series",
)
REPORT_MODES = {"standard", "standard_with_custom_sections"}
BUSINESS_QUESTION_SOURCES = {"user", "user_confirmed_default"}
MAX_RANK = 1000


class AnalysisInputRequired(ValueError):
    """Raised when phase two has no user-provided business question."""


@dataclass(frozen=True)
class AnalysisSpec:
    analysis_id: str | None
    run_id: str | None
    sections: tuple[str, ...]
    business_question: str
    business_question_source: str = "user"
    user_confirmed: bool = True
    metric_focus: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    report_mode: str = "standard"
    custom_sections: tuple[Any, ...] = ()

    @property
    def brand_filter(self) -> tuple[str, ...]:
        """Expose the phase-two brand scope without breaking nested filters."""
        return tuple(self.filters.get("brand_filter", ()))

    def to_dict(self) -> dict[str, Any]:
        filters = dict(self.filters)
        if "rank_range" in filters:
            filters["rank_range"] = list(filters["rank_range"])
        if "material_ids" in filters:
            filters["material_ids"] = list(filters["material_ids"])
        if "brand_filter" in filters:
            filters["brand_filter"] = list(filters["brand_filter"])
        return {
            "analysis_id": self.analysis_id,
            "run_id": self.run_id,
            "sections": list(self.sections),
            "business_question": self.business_question,
            "business_question_source": self.business_question_source,
            "user_confirmed": self.user_confirmed,
            "metric_focus": list(self.metric_focus),
            "filters": filters,
            "report_mode": self.report_mode,
            "custom_sections": list(self.custom_sections),
        }


def _nonblank(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")
    return value.strip()


def _string_tuple(value: Any, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    result = []
    for item in value:
        result.append(_nonblank(item, f"{field_name} item"))
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(result)


def _filters(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("filters must be an object")
    unexpected = set(raw) - {"rank_range", "material_ids", "brand_filter"}
    if unexpected:
        raise ValueError("unsupported filters: " + ", ".join(sorted(unexpected)))

    rank_range = raw.get("rank_range", [1, MAX_RANK])
    if not isinstance(rank_range, (list, tuple)) or len(rank_range) != 2:
        raise ValueError("rank_range must contain two integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in rank_range):
        raise ValueError("rank_range must contain two integers")
    start, end = rank_range
    if start < 1 or end > MAX_RANK or start > end:
        raise ValueError(f"rank_range must be within 1..{MAX_RANK} and ordered")

    material_ids = _string_tuple(raw.get("material_ids", []), "material_ids")
    brand_filter = _string_tuple(raw.get("brand_filter", []), "brand_filter")
    return {
        "rank_range": (start, end),
        "material_ids": material_ids,
        "brand_filter": brand_filter,
    }


def parse_analysis_spec(payload: dict) -> AnalysisSpec:
    """Parse a JSON-compatible payload without silently widening its scope."""
    if not isinstance(payload, dict):
        raise ValueError("analysis spec must be an object")
    payload = dict(payload)
    filters = payload.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    filters = dict(filters)
    if "material_filter" in payload:
        material_filter = payload["material_filter"]
        if not isinstance(material_filter, dict):
            raise ValueError("material_filter must be an object")
        filters.update(material_filter)
    if "brand_filter" in payload:
        if "brand_filter" in filters and filters["brand_filter"] != payload["brand_filter"]:
            raise ValueError("brand_filter is specified twice with different values")
        filters["brand_filter"] = payload["brand_filter"]
    sections = _string_tuple(payload.get("sections"), "sections", allow_empty=False)
    unsupported = set(sections) - set(AVAILABLE_SECTIONS)
    if unsupported:
        raise ValueError("unsupported section: " + ", ".join(sorted(unsupported)))
    raw_question = payload.get("business_question")
    if not isinstance(raw_question, str) or not raw_question.strip():
        raise AnalysisInputRequired(
            "business_question is required; please provide an analysis question"
        )
    business_question = raw_question.strip()
    business_question_source = payload.get("business_question_source", "user")
    if business_question_source not in BUSINESS_QUESTION_SOURCES:
        raise ValueError(
            "business_question_source must be user or user_confirmed_default"
        )
    user_confirmed = payload.get(
        "user_confirmed", business_question_source == "user"
    )
    if not isinstance(user_confirmed, bool):
        raise ValueError("user_confirmed must be a boolean")
    if business_question_source == "user_confirmed_default" and not user_confirmed:
        raise ValueError(
            "user_confirmed_default business_question requires explicit confirmation"
        )
    report_mode = payload.get("report_mode", "standard")
    if report_mode not in REPORT_MODES:
        raise ValueError("report_mode must be standard or standard_with_custom_sections")
    custom_sections = payload.get("custom_sections", [])
    if not isinstance(custom_sections, (list, tuple)):
        raise ValueError("custom_sections must be an array")
    for item in custom_sections:
        if isinstance(item, str):
            _nonblank(item, "custom_sections item")
        elif isinstance(item, dict):
            _nonblank(item.get("name"), "custom_sections.name")
        else:
            raise ValueError("custom_sections items must be strings or objects with name")
    if len(custom_sections) != len({repr(item) for item in custom_sections}):
        raise ValueError("custom_sections must not contain duplicates")
    if report_mode == "standard" and custom_sections:
        raise ValueError("custom_sections requires standard_with_custom_sections")
    return AnalysisSpec(
        analysis_id=(payload.get("analysis_id") or None),
        run_id=(payload.get("run_id") or None),
        sections=sections,
        business_question=business_question,
        business_question_source=business_question_source,
        user_confirmed=user_confirmed,
        metric_focus=_string_tuple(payload.get("metric_focus", []), "metric_focus"),
        filters=_filters(filters),
        report_mode=report_mode,
        custom_sections=tuple(custom_sections),
    )


def validate_analysis_spec(spec: AnalysisSpec, manifest: dict) -> list[str]:
    """Return all spec/manifest errors so callers can show one actionable report."""
    errors: list[str] = []
    if not isinstance(spec, AnalysisSpec):
        return ["spec must be an AnalysisSpec"]
    if not spec.analysis_id:
        errors.append("analysis_id is required")
    if not spec.run_id:
        errors.append("run_id is required")
    if not isinstance(manifest, dict):
        return errors + ["manifest must be an object"]
    brand_filter = spec.filters.get("brand_filter", ())
    if brand_filter:
        brand_metadata = manifest.get("brand_classification")
        candidates = brand_metadata.get("candidate_brands") if isinstance(brand_metadata, dict) else None
        if not isinstance(candidates, list):
            errors.append("brand_filter requires manifest brand_classification metadata")
        else:
            allowed_brands = {str(item) for item in candidates} | {"未知品牌"}
            unsupported_brands = sorted(set(brand_filter) - allowed_brands)
            if unsupported_brands:
                errors.append(
                    "brand_filter contains brands outside manifest scope: "
                    + ", ".join(unsupported_brands)
                )
    manifest_run_id = manifest.get("run_id") or manifest.get("runId")
    if spec.run_id and manifest_run_id and spec.run_id != manifest_run_id:
        errors.append(f"run_id does not match manifest: {spec.run_id} != {manifest_run_id}")
    packages = manifest.get("packages")
    if not isinstance(packages, dict):
        return errors + ["manifest packages must be an object"]
    for section in spec.sections:
        package = packages.get(section)
        if not isinstance(package, dict):
            errors.append(f"section {section} is unavailable in manifest")
            continue
        if not isinstance(package.get("path"), str) or not package["path"].strip():
            errors.append(f"section {section} has no package path")
        if not isinstance(package.get("record_count"), int) or package["record_count"] < 0:
            errors.append(f"section {section} has invalid record_count")
    start, end = spec.filters["rank_range"]
    ranking = packages.get("material_ranking", {})
    available = ranking.get("record_count") if isinstance(ranking, dict) else None
    if "material_ranking" in spec.sections and isinstance(available, int) and available:
        if start > available:
            errors.append(f"rank_range starts after available ranking records: {start} > {available}")
        if end > available:
            errors.append(f"rank_range exceeds available ranking records: {end} > {available}")
    return errors
