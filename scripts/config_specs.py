"""Resolve and persist the public query and highlight configuration contracts."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import math

try:
    from .date_policy import validate_custom_period
except ImportError:
    from date_policy import validate_custom_period

try:
    from .material_field_registry import (
        MaterialFieldRegistry,
        MaterialFieldRegistryError,
        default_material_field_registry,
    )
except ImportError:
    from material_field_registry import (
        MaterialFieldRegistry,
        MaterialFieldRegistryError,
        default_material_field_registry,
    )

try:
    from .rpa_io import write_json_atomically
except ImportError:
    from rpa_io import write_json_atomically


SOURCE = "juliang_yuntu_industry_content_rankings"
DATE_TYPES = {"LAST_7_DAYS", "LAST_30_DAYS", "CUSTOM"}
RANKING_METRICS = {
    "EXPOSURE_TOP1000",
    "CTR_TOP1000",
    "INTERACTION_RATE_TOP1000",
    "COMPLETION_RATE_TOP1000",
}
BRAND_SCOPES = {"ALL_INDUSTRY", "SPECIFIED_BRANDS"}
AGE_VALUES = {
    "AGE_18_19", "AGE_20_23", "AGE_24_30", "AGE_31_35", "AGE_36_40",
    "AGE_41_45", "AGE_46_50", "AGE_51_55", "AGE_56_59", "AGE_60_PLUS",
}
GENDER_VALUES = {"MALE", "FEMALE"}
CROWD_GROUP_VALUES = {
    "TOWN_YOUTH", "GENZ", "SENIOR_MIDDLE", "REFINED_MOM", "NEW_WHITE_COLLAR",
    "URBAN_SILVER", "TOWN_MIDDLE_ELDER", "URBAN_BLUE_COLLAR",
}
HIGHLIGHT_METRICS = ("点赞指数", "流失指数", "点击指数", "互动指数", "评论指数")
HIGHLIGHT_STRATEGIES = {"local_peaks", "global_top_values"}
_LEGACY_HIGHLIGHT_STRATEGIES = {"top_values": "global_top_values"}
DEFAULT_MATERIAL_LIST = {
    "filters": [], "sort": {"field": "rank", "direction": "asc"}
}


class ConfigSpecError(ValueError):
    """Raised when a public configuration cannot be resolved safely."""


def _material_value(value, field, label):
    if field.kind in {"text", "identifier", "string_list"}:
        if not isinstance(value, str) or not value.strip():
            raise ConfigSpecError(f"{label} must be a nonblank string")
        return value.strip()
    if field.kind == "date":
        return _date(value, label).isoformat()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigSpecError(f"{label} must be a number")
    if not math.isfinite(value):
        raise ConfigSpecError(f"{label} must be finite")
    if field.kind == "integer" and not isinstance(value, int):
        raise ConfigSpecError(f"{label} must be an integer")
    return value


def resolve_material_list(payload: object | None, registry: MaterialFieldRegistry | None = None) -> dict:
    if payload is None:
        payload = {}
    payload = _object(payload, "material_list")
    _keys(payload, {"filters", "sort"}, "material_list")
    registry = default_material_field_registry() if registry is None else registry
    raw_filters = payload.get("filters", [])
    if not isinstance(raw_filters, list):
        raise ConfigSpecError("material_list.filters must be an array")
    filters = []
    seen = set()
    for index, raw_filter in enumerate(raw_filters):
        item = _object(raw_filter, f"material_list.filters[{index}]")
        _keys(item, {"field", "operator", "value"}, f"material_list.filters[{index}]")
        try:
            field = registry.resolve(item.get("field"))
        except MaterialFieldRegistryError as exc:
            raise ConfigSpecError(f"unsupported material_list field: {item.get('field')}") from exc
        operator = item.get("operator")
        if operator not in field.filter_operators:
            raise ConfigSpecError(f"unsupported material_list operator for {field.id}: {operator}")
        value = _material_value(item.get("value"), field, f"material_list.filters[{index}].value")
        normalized = {"field": field.id, "operator": operator, "value": value}
        identity = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if identity in seen:
            raise ConfigSpecError("material_list.filters cannot contain duplicate conditions")
        seen.add(identity)
        filters.append(normalized)
    raw_sort = payload.get("sort", {"field": "rank", "direction": "asc"})
    sort = _object(raw_sort, "material_list.sort")
    _keys(sort, {"field", "direction"}, "material_list.sort")
    try:
        sort_field = registry.resolve(sort.get("field"))
    except MaterialFieldRegistryError as exc:
        raise ConfigSpecError(f"unsupported material_list sort field: {sort.get('field')}") from exc
    if not sort_field.sortable:
        raise ConfigSpecError(f"material_list sort field is not sortable: {sort_field.id}")
    direction = sort.get("direction")
    if direction not in {"asc", "desc"}:
        raise ConfigSpecError("material_list.sort.direction must be asc or desc")
    return {"filters": filters, "sort": {"field": sort_field.id, "direction": direction}}


def _object(value, label):
    if not isinstance(value, dict):
        raise ConfigSpecError(f"{label} must be an object")
    return value


def _keys(value, allowed, label):
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigSpecError(f"unknown {label} fields: {', '.join(sorted(unknown))}")


def _strings(value, label, allowed=None):
    if value is None or value == []:
        return []
    if not isinstance(value, list) or not value:
        raise ConfigSpecError(f"{label} must be a non-empty array")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigSpecError(f"{label} values must be nonblank strings")
        item = item.strip()
        if item in result:
            raise ConfigSpecError(f"{label} cannot contain duplicates")
        if allowed is not None and item not in allowed:
            raise ConfigSpecError(f"{label} contains an unsupported value: {item}")
        result.append(item)
    return result


def _date(value, label):
    if not isinstance(value, str):
        raise ConfigSpecError(f"{label} must use YYYYMMDD or YYYY-MM-DD format")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ConfigSpecError(f"{label} must use YYYYMMDD or YYYY-MM-DD format")


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigSpecError(f"{label} must be a positive integer")
    return value


def resolve_query_spec(payload: dict) -> dict:
    payload = _object(payload, "QuerySpec")
    _keys(payload, {"schema_version", "source", "industry", "period", "filters", "ranking", "collection", "material_list"}, "QuerySpec")
    if payload.get("schema_version") != 1:
        raise ConfigSpecError("schema_version must be 1")
    if payload.get("source") != SOURCE:
        raise ConfigSpecError(f"source must be {SOURCE}")
    industry = payload.get("industry")
    if not isinstance(industry, str) or not industry.strip():
        raise ConfigSpecError("industry must be a nonblank string")

    period = _object(payload.get("period"), "period")
    _keys(period, {"type", "start_date", "end_date"}, "period")
    period_type = period.get("type")
    if period_type not in DATE_TYPES:
        raise ConfigSpecError("period.type must be LAST_7_DAYS, LAST_30_DAYS, or CUSTOM")
    normalized_period = {"type": period_type}
    if period_type == "CUSTOM":
        if not period.get("start_date") or not period.get("end_date"):
            raise ConfigSpecError("CUSTOM requires period.start_date and period.end_date")
        start = _date(period["start_date"], "period.start_date")
        end = _date(period["end_date"], "period.end_date")
        try:
            validate_custom_period(start, end)
        except ValueError as exc:
            raise ConfigSpecError(str(exc)) from exc
        normalized_period.update(start_date=period["start_date"], end_date=period["end_date"])
    elif period.get("start_date") is not None or period.get("end_date") is not None:
        raise ConfigSpecError("period dates are only valid for CUSTOM")

    filters = _object(payload.get("filters", {}), "filters")
    _keys(filters, {"ages", "genders", "crowd_groups", "brand_scope"}, "filters")
    brand_scope = _object(filters.get("brand_scope", {}), "brand_scope")
    _keys(brand_scope, {"type", "brands"}, "brand_scope")
    scope_type = brand_scope.get("type", "ALL_INDUSTRY")
    if scope_type not in BRAND_SCOPES:
        raise ConfigSpecError("brand_scope.type must be ALL_INDUSTRY or SPECIFIED_BRANDS")
    brands = _strings(brand_scope.get("brands", []), "brand_scope.brands")
    if scope_type == "SPECIFIED_BRANDS" and not 3 <= len(brands) <= 10:
        raise ConfigSpecError("SPECIFIED_BRANDS requires 3 to 10 brands")
    if scope_type == "ALL_INDUSTRY" and brands:
        raise ConfigSpecError("ALL_INDUSTRY cannot include brands")
    normalized_filters = {
        "ages": _strings(filters.get("ages", []), "filters.ages", AGE_VALUES),
        "genders": _strings(filters.get("genders", []), "filters.genders", GENDER_VALUES),
        "crowd_groups": _strings(filters.get("crowd_groups", []), "filters.crowd_groups", CROWD_GROUP_VALUES),
        "brand_scope": {"type": scope_type, "brands": brands},
    }

    ranking = _object(payload.get("ranking"), "ranking")
    _keys(ranking, {"metric"}, "ranking")
    if ranking.get("metric") not in RANKING_METRICS:
        raise ConfigSpecError("ranking.metric is unsupported")
    collection = _object(payload.get("collection", {}), "collection")
    _keys(collection, {"target_top_n", "candidate_top_n"}, "collection")
    target = _positive(collection.get("target_top_n", 10), "collection.target_top_n")
    if target > 1000:
        raise ConfigSpecError("collection.target_top_n cannot exceed 1000")
    candidate = _positive(collection.get("candidate_top_n", 1000), "collection.candidate_top_n")
    if candidate != 1000:
        raise ConfigSpecError("collection.candidate_top_n must be 1000")
    material_list = resolve_material_list(payload.get("material_list"))

    return {
        "schema_version": 1,
        "source": SOURCE,
        "industry": industry.strip(),
        "period": normalized_period,
        "filters": normalized_filters,
        "ranking": {"metric": ranking["metric"]},
        "collection": {"target_top_n": target, "candidate_top_n": candidate},
        "material_list": material_list,
    }


def query_to_rpa_params(resolved: dict) -> dict:
    period = resolved["period"]
    params = {"industry": resolved["industry"], "date_type": period["type"], "ranking_limit_type": resolved["ranking"]["metric"]}
    if period["type"] == "CUSTOM":
        params.update(custom_start_date=period["start_date"], custom_end_date=period["end_date"])
    filters = resolved["filters"]
    for key in ("ages", "genders", "crowd_groups"):
        if filters[key]:
            params[key] = filters[key]
    scope = filters["brand_scope"]
    params["brand_scope_type"] = scope["type"]
    if scope["brands"]:
        params["brands"] = scope["brands"]
    return params


def detail_to_rpa_params(resolved: dict) -> dict:
    period = resolved["period"]
    params = {"date_type": period["type"]}
    if period["type"] == "CUSTOM":
        params.update(custom_start_date=period["start_date"], custom_end_date=period["end_date"])
    return params


def resolve_highlight_spec(payload: dict | None) -> dict:
    """Normalize the shared high-frame contract, accepting prior Yuntu runs."""
    payload = {} if payload is None else _object(payload, "HighlightSpec")
    if "schemaVersion" not in payload:
        _keys(payload, {"schema_version", "include_cover", "metrics"}, "legacy HighlightSpec")
        if payload.get("schema_version", 1) != 1:
            raise ConfigSpecError("legacy HighlightSpec schema_version must be 1")
        if "include_cover" not in payload or not isinstance(payload["include_cover"], bool):
            raise ConfigSpecError("legacy HighlightSpec.include_cover must be boolean")
        legacy_metrics = _object(payload.get("metrics"), "legacy metrics")
        unknown = set(legacy_metrics) - set(HIGHLIGHT_METRICS)
        if unknown:
            raise ConfigSpecError(f"unsupported highlight metrics: {', '.join(sorted(unknown))}")
        for name, item in legacy_metrics.items():
            item = _object(item, f"legacy metrics.{name}")
            _keys(item, {"strategy", "limit"}, f"legacy metrics.{name}")
        payload = {
            "schemaVersion": 1,
            "includeZeroSecond": payload["include_cover"],
            "metrics": {
                name: (
                    {
                        "enabled": True,
                        "strategy": _LEGACY_HIGHLIGHT_STRATEGIES.get(legacy_metrics[name].get("strategy"), legacy_metrics[name].get("strategy")),
                        "maxFrames": legacy_metrics[name].get("limit"),
                    }
                    if name in legacy_metrics else {"enabled": False}
                )
                for name in HIGHLIGHT_METRICS
            },
        }

    _keys(payload, {"schemaVersion", "includeZeroSecond", "metrics"}, "HighlightSpec")
    if payload.get("schemaVersion") != 1:
        raise ConfigSpecError("HighlightSpec schemaVersion must be 1")
    if not isinstance(payload.get("includeZeroSecond"), bool):
        raise ConfigSpecError("HighlightSpec.includeZeroSecond must be boolean")
    custom = _object(payload.get("metrics"), "metrics")
    if set(custom) != set(HIGHLIGHT_METRICS):
        raise ConfigSpecError("HighlightSpec.metrics must explicitly configure all Yuntu metrics")

    metrics = {}
    for name in HIGHLIGHT_METRICS:
        item = _object(custom[name], f"metrics.{name}")
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigSpecError(f"metrics.{name}.enabled must be boolean")
        if not enabled:
            _keys(item, {"enabled"}, f"metrics.{name}")
            metrics[name] = {"enabled": False}
            continue
        _keys(item, {"enabled", "strategy", "maxFrames"}, f"metrics.{name}")
        if item.get("strategy") not in HIGHLIGHT_STRATEGIES:
            raise ConfigSpecError(f"metrics.{name}.strategy is unsupported")
        metrics[name] = {
            "enabled": True,
            "strategy": item["strategy"],
            "maxFrames": _positive(item.get("maxFrames"), f"metrics.{name}.maxFrames"),
        }
    if not any(item["enabled"] for item in metrics.values()):
        raise ConfigSpecError("HighlightSpec.metrics must enable at least one metric")
    return {"schemaVersion": 1, "includeZeroSecond": payload["includeZeroSecond"], "metrics": metrics}


def resolve_highlight_request(payload: dict | None, *, enabled: bool | None = None) -> dict:
    """Normalize the stage-one image choice without weakening HighlightSpec validation."""
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigSpecError("highlight enabled must be boolean")
    if payload is None:
        if enabled is True:
            raise ConfigSpecError("highlight_spec_required")
        return {
            "enabled": False,
            "spec": None,
            "request_source": "explicit_off" if enabled is False else "default_off",
        }
    if enabled is False:
        raise ConfigSpecError("highlight_request_conflict")
    return {
        "enabled": True,
        "spec": resolve_highlight_spec(payload),
        "request_source": "explicit_on" if enabled is True else "legacy_spec",
    }


def spec_fingerprint(query: dict, highlights: dict | None) -> str:
    query_for_fingerprint = dict(query)
    if query_for_fingerprint.get("material_list") == DEFAULT_MATERIAL_LIST:
        # The default is semantically identical to the pre-material-list contract.
        query_for_fingerprint.pop("material_list", None)
    encoded = json.dumps({"query": query_for_fingerprint, "highlights": highlights}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_resolved_specs(output_dir, query: dict, highlights: dict | None) -> tuple[Path, Path | None]:
    root = Path(output_dir)
    query_path = root / "resolved-query-spec.json"
    highlight_path = root / "resolved-highlight-spec.json"
    write_json_atomically(query, query_path)
    if highlights is not None:
        write_json_atomically(highlights, highlight_path)
        return query_path, highlight_path
    return query_path, None
