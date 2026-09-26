"""Build a verified, redacted and self-contained Yuntu leaderboard overview."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

try:
    from material_field_registry import MaterialFieldRegistry, MaterialFieldRegistryError
except ImportError:
    from .material_field_registry import MaterialFieldRegistry, MaterialFieldRegistryError

try:
    from command_io import CommandFileCache
except ImportError:  # pragma: no cover
    from .command_io import CommandFileCache

try:
    from brand_dimension import BrandDimensionError, build_brand_dimension
except ImportError:
    from .brand_dimension import BrandDimensionError, build_brand_dimension

try:
    from audience_overview import AudienceOverviewError, build_audience_overview
    from marketing_selling_point_overview import (
        MarketingSellingPointOverviewError,
        build_marketing_selling_point_overview,
    )
    from script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
    from highlight_overview import (
        HighlightOverviewError,
        build_highlight_overview,
        copy_verified_highlight_assets,
    )
    from single_material_overview import (
        SingleMaterialOverviewError,
        build_single_material_overview,
        copy_verified_single_material_assets,
    )
except ImportError:
    from .audience_overview import AudienceOverviewError, build_audience_overview
    from .marketing_selling_point_overview import (
        MarketingSellingPointOverviewError,
        build_marketing_selling_point_overview,
    )
    from .script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
    from .highlight_overview import (
        HighlightOverviewError,
        build_highlight_overview,
        copy_verified_highlight_assets,
    )
    from .single_material_overview import (
        SingleMaterialOverviewError,
        build_single_material_overview,
        copy_verified_single_material_assets,
    )


SCHEMA_VERSION = 1
OVERVIEW_KIND = "yuntu_material_leaderboard_overview"
PAGE_SIZE = 10
FIELD_GROUPS = {
    "title": "素材基础信息",
    "publish_time": "素材基础信息",
    "video_duration": "素材基础信息",
    "rank": "素材基础信息",
    "material_uri": "素材基础信息",
    "people_tags": "素材基础信息",
    "material_tags": "素材基础信息",
    "metrics.show_cnt": "内容曝光与播放",
    "metrics.play_3s_rate": "内容曝光与播放",
    "metrics.play_5s_rate": "内容曝光与播放",
    "metrics.play_over_rate": "内容曝光与播放",
    "metrics.play_duration_avg": "内容曝光与播放",
    "metrics.ctr": "点击与转化",
    "metrics.cvr": "点击与转化",
    "metrics.pvr": "点击与转化",
    "metrics.product_wish_button_buy_cart_rate": "点击与转化",
    "metrics.interact_rate": "互动指标",
    "metrics.like_rate": "互动指标",
    "metrics.comment_rate": "互动指标",
    "metrics.share_rate": "互动指标",
}
FIELD_GROUP_ORDER = tuple(dict.fromkeys(FIELD_GROUPS.values()))
DEFAULT_FIELD_IDS = (
    "title",
    "publish_time",
    "metrics.show_cnt",
    "metrics.play_3s_rate",
    "metrics.play_over_rate",
    "metrics.play_duration_avg",
    "metrics.ctr",
    "metrics.cvr",
    "metrics.pvr",
    "metrics.interact_rate",
)
_SENSITIVE_TOKENS = {"account_id", "shop_id", "advertiser_id", "credential", "token", "password"}
_SENSITIVE_TEXT = re.compile(
    r"(?:https?|file)://|(?:^|[\\/])(?:Users|home|var|tmp)(?:[\\/]|$)", re.IGNORECASE
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class OverviewReportError(ValueError):
    """Raised when a run cannot produce a trustworthy overview."""


def _read_json(path: Path, label: str, expected_type: type, io_cache=None) -> Any:
    try:
        payload = io_cache.read_json(path) if io_cache is not None else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OverviewReportError(f"{label} is missing or unreadable") from exc
    if not isinstance(payload, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise OverviewReportError(f"{label} must be a {expected}")
    return payload


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverviewReportError(f"{label} is required")
    return value.strip()


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _SENSITIVE_TEXT.search(value):
        return None
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    return value


def _safe_value(value: Any) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            item = _safe_scalar(item)
            if item is not None:
                result.append(item)
        return result
    if isinstance(value, Mapping):
        return None
    return _safe_scalar(value)


def _resolve_relative_path(run_root: Path, raw_path: Any) -> tuple[Path, str]:
    path_text = _nonblank(raw_path, "artifacts.listRecords.path")
    path = Path(path_text)
    if path.is_absolute() or "\\" in path_text or "\x00" in path_text:
        raise OverviewReportError("list records path must be a safe relative path")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise OverviewReportError("list records path must be a safe relative path")
    candidate = (run_root / path).resolve()
    try:
        relative = candidate.relative_to(run_root)
    except ValueError as exc:
        raise OverviewReportError("list records path must remain inside the run") from exc
    return candidate, relative.as_posix()


def _validate_run_and_source(run_root: Path, io_cache=None) -> tuple[dict, Path, str, list]:
    if not run_root.is_dir():
        raise OverviewReportError("run directory is missing")
    summary = _read_json(run_root / "run-summary.json", "run summary", dict, io_cache)
    run_id = _nonblank(summary.get("runId"), "run summary runId")
    status = summary.get("collectionStatus", summary.get("status"))
    if status is not None and status not in {"success", "succeeded", "analysis_completed"}:
        raise OverviewReportError("run is not a completed successful collection")
    list_stage = summary.get("stages", {}).get("list")
    if isinstance(list_stage, dict):
        if list_stage.get("normalized") is False or list_stage.get("contextVerified") is False:
            raise OverviewReportError("list source is not normalized and context-verified")
        if list_stage.get("requestStatus") in {"failed", "error", "cancelled"}:
            raise OverviewReportError("list collection did not succeed")

    artifacts = summary.get("artifacts")
    list_artifact = artifacts.get("listRecords") if isinstance(artifacts, dict) else None
    if not isinstance(list_artifact, dict):
        raise OverviewReportError("run summary listRecords artifact is missing")
    source_path, relative_path = _resolve_relative_path(run_root, list_artifact.get("path"))
    if not source_path.is_file():
        raise OverviewReportError("list records source file is missing")
    declared_hash = _nonblank(list_artifact.get("sha256"), "list records sha256")
    if not _SHA256.fullmatch(declared_hash):
        raise OverviewReportError("list records sha256 is invalid")
    rows = _read_json(source_path, "list records source", list, io_cache)
    if io_cache is not None:
        source_hash, source_size = io_cache.digest(source_path)
    else:
        source_bytes = source_path.read_bytes()
        source_hash, source_size = hashlib.sha256(source_bytes).hexdigest(), len(source_bytes)
    if source_hash != declared_hash.lower():
        raise OverviewReportError("list records sha256 does not match source")
    if list_artifact.get("sizeBytes") is not None:
        size = list_artifact.get("sizeBytes")
        if type(size) is not int or size != source_size:
            raise OverviewReportError("list records size does not match source")
    if not rows:
        raise OverviewReportError("list records source must not be empty")
    if list_artifact.get("recordCount") is not None and list_artifact["recordCount"] != len(rows):
        raise OverviewReportError("list records record count does not match source")

    batch = _read_json(run_root / "batch-manifest.json", "batch manifest", dict, io_cache)
    batch_run_id = batch.get("runId")
    if batch_run_id is not None and batch_run_id != run_id:
        raise OverviewReportError("batch manifest runId does not match run summary")
    candidate_top_n = batch.get("candidateTopN")
    if candidate_top_n is None and isinstance(batch.get("collectionContract"), dict):
        candidate_top_n = batch["collectionContract"].get("candidateTopN")
    if candidate_top_n is not None:
        if type(candidate_top_n) is not int or candidate_top_n != len(rows):
            raise OverviewReportError("batch manifest candidate record count does not match source")

    facts_manifest_path = run_root / "facts/manifest.json"
    if facts_manifest_path.is_file():
        facts = _read_json(facts_manifest_path, "facts manifest", dict, io_cache)
        if facts.get("run_id") is not None and facts["run_id"] != run_id:
            raise OverviewReportError("facts manifest run_id does not match run summary")
        coverage = facts.get("coverage")
        ranking_records = coverage.get("ranking_records") if isinstance(coverage, dict) else None
        if ranking_records is not None and (type(ranking_records) is not int or ranking_records != len(rows)):
            raise OverviewReportError("facts manifest ranking record count does not match source")
    return summary, source_path, relative_path, rows


def _safe_context_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_safe_context_value(item) for item in value if _safe_context_value(item) is not None]
    return _safe_scalar(value)


def _is_sensitive_field(value: Any) -> bool:
    token = str(value or "").strip().casefold()
    return any(part in token for part in _SENSITIVE_TOKENS)


def _query_context(run_root: Path, io_cache=None) -> dict:
    spec = _read_json(run_root / "resolved-query-spec.json", "resolved query spec", dict, io_cache)
    context: dict[str, Any] = {}
    if _safe_scalar(spec.get("industry")) is not None:
        context["industry"] = _safe_scalar(spec["industry"])
    period = spec.get("period")
    if isinstance(period, dict):
        context["period"] = {
            key: _safe_scalar(period.get(key))
            for key in ("type", "start_date", "end_date")
            if _safe_scalar(period.get(key)) is not None
        }
    ranking = spec.get("ranking")
    if isinstance(ranking, dict):
        context["ranking"] = {
            key: _safe_scalar(ranking.get(key))
            for key in ("metric", "field", "direction")
            if _safe_scalar(ranking.get(key)) is not None
        }
    filters = spec.get("filters")
    safe_filters: dict[str, Any] = {}
    if isinstance(filters, dict):
        for key in ("ages", "genders", "crowd_groups"):
            value = _safe_context_value(filters.get(key))
            if value is not None:
                safe_filters[key] = value
        brand_scope = filters.get("brand_scope")
        if isinstance(brand_scope, dict):
            safe_filters["brand_scope"] = {
                key: _safe_context_value(brand_scope.get(key))
                for key in ("type", "brands")
                if _safe_context_value(brand_scope.get(key)) is not None
            }
    context["filters"] = safe_filters
    material_list = spec.get("material_list")
    safe_material_list: dict[str, Any] = {}
    if isinstance(material_list, dict):
        sort = material_list.get("sort")
        if isinstance(sort, dict):
            safe_material_list["sort"] = {
                key: _safe_scalar(sort.get(key))
                for key in ("field", "direction")
                if _safe_scalar(sort.get(key)) is not None
            }
        raw_filters = material_list.get("filters")
        if isinstance(raw_filters, list):
            safe_material_list["filters"] = [
                {
                    key: _safe_context_value(item.get(key))
                    for key in ("field", "operator", "value")
                    if _safe_context_value(item.get(key)) is not None
                }
                for item in raw_filters
                if isinstance(item, dict) and not _is_sensitive_field(item.get("field"))
            ]
    context["materialList"] = safe_material_list
    return context


_HIGHLIGHT_STRATEGY_LABELS = {
    "local_peaks": "局部峰值",
    "global_top_values": "数值 Top",
    "top_values": "数值 Top",
}


def _highlight_filter_context(run_root: Path, io_cache=None) -> list[dict[str, str]]:
    path = run_root / "resolved-highlight-spec.json"
    if not path.is_file():
        return []
    try:
        spec = _read_json(path, "resolved highlight spec", dict, io_cache)
    except OverviewReportError:
        return []
    entries: list[dict[str, str]] = []
    include_cover = spec.get("includeZeroSecond", spec.get("include_cover"))
    if isinstance(include_cover, bool):
        entries.append({"label": "封面帧", "value": "包含" if include_cover else "不包含"})
    metrics = spec.get("metrics")
    if isinstance(metrics, dict):
        for metric, config in metrics.items():
            if not isinstance(config, dict):
                continue
            if config.get("enabled", True) is False:
                continue
            strategy = _HIGHLIGHT_STRATEGY_LABELS.get(config.get("strategy"), "未找到中文映射：%s" % config.get("strategy"))
            limit = config.get("maxFrames", config.get("limit"))
            value = strategy
            if type(limit) is int:
                value = "%s，%d 帧" % (strategy, limit)
            entries.append({"label": str(metric), "value": value})
    return entries


def _value_type(kind: str) -> str:
    if kind == "rate":
        return "rate"
    if kind in {"number", "integer"}:
        return "number"
    if kind == "date":
        return "date"
    if kind == "string_list":
        return "list"
    return "text"


def _filter_highlight_overview(model: dict, material_ids: set[str]) -> dict:
    cards = [card for card in model.get("cards", []) if str(card.get("materialId")) in material_ids]
    filtered = dict(model)
    filtered["status"] = "available" if cards else "unavailable"
    filtered["source"] = {
        **(model.get("source") if isinstance(model.get("source"), dict) else {}),
        "materialCount": len({str(card.get("materialId")) for card in cards}),
        "verifiedFrameCount": len(cards),
    }
    filtered["cards"] = cards
    return filtered


def build_overview_data(run_dir: Path, registry_path: Path, io_cache=None, asset_index=None) -> dict:
    run_root = Path(run_dir).resolve()
    io_cache = io_cache or CommandFileCache(run_root)
    summary, source_path, relative_path, source_rows = _validate_run_and_source(run_root, io_cache)
    try:
        registry = MaterialFieldRegistry.from_path(Path(registry_path))
    except MaterialFieldRegistryError as exc:
        raise OverviewReportError(str(exc)) from exc

    definitions = registry.definitions()
    field_positions = {field.id: index for index, field in enumerate(definitions)}
    fields = []
    for definition in definitions:
        if definition.id == "material_id":
            continue
        group = FIELD_GROUPS.get(definition.id)
        if group is None:
            raise OverviewReportError(f"registry field is not allowed in overview: {definition.id}")
        fields.append({
            "id": definition.id,
            "label": definition.label,
            "group": group,
            "valueType": _value_type(definition.kind),
            "sortable": definition.sortable,
        })
    fields.sort(key=lambda field: (FIELD_GROUP_ORDER.index(field["group"]), field_positions[field["id"]]))
    field_map = {field["id"]: field for field in fields}

    rows = []
    material_ids = set()
    for index, record in enumerate(source_rows):
        if not isinstance(record, dict):
            raise OverviewReportError(f"list record {index} must be an object")
        material_id = _safe_scalar(record.get("material_id"))
        if material_id is None or isinstance(material_id, bool) or str(material_id).strip() == "":
            raise OverviewReportError(f"list record {index} has no material_id")
        material_id = str(material_id).strip()
        if material_id in material_ids:
            raise OverviewReportError(f"duplicate material_id: {material_id}")
        material_ids.add(material_id)
        values = {}
        for field in fields:
            value = _safe_value(registry.value(record, field["id"]))
            if field["valueType"] == "list" and value is None:
                value = []
            values[field["id"]] = value
        rows.append({"materialId": material_id, "values": values})

    default_ids = [field_id for field_id in DEFAULT_FIELD_IDS if field_id in field_map]
    for field in fields:
        field["defaultVisible"] = field["id"] in default_ids
    try:
        audience_overview = build_audience_overview(run_root, io_cache=io_cache)
    except AudienceOverviewError:
        audience_overview = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "audience_overview_unavailable"}]}
    try:
        script_formula_overview = build_script_formula_overview(run_root, io_cache=io_cache)
    except ScriptFormulaOverviewError:
        script_formula_overview = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "script_formula_overview_unavailable"}]}
    try:
        marketing_selling_point_overview = build_marketing_selling_point_overview(run_root, io_cache=io_cache)
    except MarketingSellingPointOverviewError:
        marketing_selling_point_overview = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "marketing_selling_point_overview_unavailable"}]}
    try:
        highlight_overview = build_highlight_overview(
            run_root, io_cache=io_cache, asset_index=asset_index
        )
    except HighlightOverviewError:
        highlight_overview = {
            "schemaVersion": 1,
            "status": "unavailable",
            "source": {"materialCount": 0, "verifiedFrameCount": 0},
            "cards": [],
            "gaps": [{"code": "highlight_overview_unavailable"}],
        }
    try:
        single_material_overview = build_single_material_overview(
            run_root, io_cache=io_cache, asset_index=asset_index
        )
    except SingleMaterialOverviewError:
        single_material_overview = {
            "schemaVersion": 1,
            "kind": "yuntu_single_material_overview",
            "status": "unavailable",
            "materialCount": 0,
            "source": {"rankingRecordCount": len(rows), "detailRecordCount": 0, "verifiedFrameCount": 0},
            "materials": [],
            "gaps": [{"code": "single_material_overview_unavailable"}],
        }
    try:
        brand_overview = build_brand_dimension(run_root, io_cache=io_cache)
    except BrandDimensionError:
        brand_overview = {
            "schemaVersion": 1,
            "status": "unavailable",
            "tabs": [],
            "materialIdsByTab": {},
            "views": {},
            "gaps": [{"code": "brand_dimension_unavailable"}],
        }
    if brand_overview.get("status") == "available":
        brand_views = {}
        for tab in brand_overview.get("tabs", []):
            tab_id = tab["id"]
            material_ids = set(brand_overview["materialIdsByTab"].get(tab_id, []))
            if tab_id == "all":
                brand_views[tab_id] = {
                    "materialCount": len(material_ids),
                    "audienceOverview": audience_overview,
                    "scriptFormulaOverview": script_formula_overview,
                    "marketingSellingPointOverview": marketing_selling_point_overview,
                    "highlightOverview": highlight_overview,
                }
                continue
            try:
                scoped_audience = build_audience_overview(run_root, material_ids, io_cache=io_cache)
            except AudienceOverviewError:
                scoped_audience = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "audience_overview_unavailable"}]}
            try:
                scoped_script = build_script_formula_overview(run_root, material_ids, io_cache=io_cache)
            except ScriptFormulaOverviewError:
                scoped_script = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "script_formula_overview_unavailable"}]}
            try:
                scoped_marketing = build_marketing_selling_point_overview(run_root, material_ids, io_cache=io_cache)
            except MarketingSellingPointOverviewError:
                scoped_marketing = {"schemaVersion": 1, "status": "unavailable", "gaps": [{"code": "marketing_selling_point_overview_unavailable"}]}
            brand_views[tab_id] = {
                "materialCount": len(material_ids),
                "audienceOverview": scoped_audience,
                "scriptFormulaOverview": scoped_script,
                "marketingSellingPointOverview": scoped_marketing,
                "highlightOverview": _filter_highlight_overview(highlight_overview, material_ids),
            }
        brand_overview["views"] = brand_views
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": OVERVIEW_KIND,
        "runId": _nonblank(summary["runId"], "run summary runId"),
        "source": {
            "path": relative_path,
            "sha256": io_cache.digest(source_path)[0],
            "recordCount": len(rows),
        },
        "queryContext": _query_context(run_root, io_cache),
        "highlightFilterContext": _highlight_filter_context(run_root, io_cache),
        "fields": fields,
        "defaultFieldIds": default_ids,
        "rows": rows,
        "audienceOverview": audience_overview,
        "scriptFormulaOverview": script_formula_overview,
        "marketingSellingPointOverview": marketing_selling_point_overview,
        "highlightOverview": highlight_overview,
        "singleMaterialOverview": single_material_overview,
        "brandOverview": brand_overview,
    }


def _json_for_script(model: dict) -> str:
    return json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_overview_html(model: dict) -> str:
    embedded = _json_for_script(model)
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>云图素材榜单数据工作台</title>
<style>
:root { color-scheme: light; --bg:#f4f7fb; --surface:#fff; --muted:#66748b; --text:#172033; --line:#dfe6f0; --line-strong:#c9d5e5; --accent:#2563eb; --accent-soft:#eff6ff; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:var(--bg); }
* { box-sizing:border-box; }
body { margin:0; padding:32px; background:var(--bg); }
main { max-width:1120px; margin:0 auto; }
h1 { margin:0 0 22px; font-size:30px; line-height:1.25; letter-spacing:-.02em; }
.toolbar,.summary,.pager { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.toolbar { padding:14px; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.06); }
input[type=search] { min-width:300px; flex:1; height:44px; padding:10px 13px; border:1px solid var(--line-strong); border-radius:9px; background:var(--surface); color:var(--text); font-size:15px; outline:none; }
input[type=search]:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(37,99,235,.14); }
button { min-height:40px; padding:8px 14px; border:1px solid var(--line-strong); border-radius:9px; background:var(--surface); color:var(--text); cursor:pointer; font-size:14px; }
button:hover:not(:disabled) { border-color:var(--accent); background:var(--accent-soft); }
button:focus-visible,input:focus-visible { outline:3px solid rgba(37,99,235,.24); outline-offset:2px; }
button:disabled { cursor:default; opacity:.45; }
.summary { margin:18px 2px 10px; color:var(--muted); font-size:14px; }
.report-nav { display:flex; gap:8px; margin:0 0 18px; padding:5px; background:var(--surface); border:1px solid var(--line); border-radius:12px; }
.report-nav button.active { border-color:var(--accent); background:var(--accent-soft); color:#1d4ed8; font-weight:650; }
.report-section[hidden] { display:none; }
.overview-card { margin:18px 0; padding:22px; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.05); }
.overview-card h2 { margin:0 0 8px; font-size:20px; }
.overview-card p { color:var(--muted); }
.metric-card { display:inline-flex; flex-direction:column; min-width:190px; padding:16px; border:1px solid var(--line); border-radius:12px; background:#fbfdff; }
.metric-card strong { margin-top:5px; font-size:28px; font-variant-numeric:tabular-nums; }
.chart-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.chart-panel { min-width:0; padding:16px; border:1px solid var(--line); border-radius:12px; background:#fbfdff; }
.chart-panel h3 { margin:0 0 8px; font-size:15px; }
.chart-svg { display:block; width:100%; height:auto; min-height:220px; }
.chart-target { cursor:pointer; outline:none; }
.chart-target:focus-visible { filter:drop-shadow(0 0 4px rgba(37,99,235,.75)); }
.chart-target:hover rect,.chart-target:hover circle,.chart-target:hover path,.chart-target.is-active rect,.chart-target.is-active circle,.chart-target.is-active path { filter:brightness(.9); }
.chart-label { fill:var(--muted); font-size:12px; }
.chart-value { fill:var(--text); font-size:12px; font-weight:650; }
.legend { display:grid; gap:7px; margin-top:8px; }
.legend button { display:flex; align-items:center; gap:8px; width:100%; min-height:32px; padding:5px 8px; border:0; text-align:left; background:transparent; }
.legend-swatch { width:10px; height:10px; flex:0 0 auto; border-radius:50%; }
.formula-primary { margin-top:16px; }
.formula-secondary { margin-top:16px; }
.formula-secondary-layout { display:grid; grid-template-columns:minmax(240px, .8fr) minmax(260px, 1fr); gap:22px; align-items:center; }
.tooltip { position:fixed; z-index:30; max-width:320px; padding:8px 10px; border-radius:8px; background:#172033; color:#fff; font-size:12px; pointer-events:none; box-shadow:0 8px 22px rgba(15,23,42,.2); }
.section-nav { display:flex; gap:8px; margin:0 0 16px; }
.section-nav button.active { border-color:var(--accent); background:var(--accent); color:#fff; }
.brand-tabs { display:flex; gap:8px; margin:0 0 16px; padding:5px; overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:12px; }
.brand-tabs button { display:inline-flex; align-items:center; gap:6px; flex:0 0 auto; min-height:36px; padding:6px 12px; border-color:transparent; white-space:nowrap; }
.brand-tabs button.active { border-color:var(--accent); background:var(--accent-soft); color:#1d4ed8; font-weight:650; }
.brand-tabs-count { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
[data-section-panel][hidden] { display:none; }
.audience-section { display:grid; gap:16px; }
.audience-card { width:100%; min-width:0; overflow:hidden; padding:18px; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.06); }
.chart-card h3 { margin:0; font-size:16px; }
.chart-box { min-width:0; max-width:100%; min-height:260px; overflow-x:auto; overflow-y:hidden; }
.chart-box svg { display:block; min-width:0; max-width:100%; height:290px; }
#marketing-selling-point-chart { overflow-x:auto; overflow-y:hidden; padding-bottom:4px; }
#marketing-selling-point-chart .marketing-selling-point-svg { max-width:none; }
.chart-bar,.script-formula-bar,.marketing-selling-point-bar { fill:#5b8def; cursor:pointer; }
.chart-bar:hover,.chart-bar:focus,.script-formula-bar:hover,.script-formula-bar:focus,.marketing-selling-point-bar:hover,.marketing-selling-point-bar:focus { fill:#2563eb; outline:none; }
.chart-axis { stroke:#dfe6f0; stroke-width:1; }
.donut-layout { display:grid; grid-template-columns:minmax(220px,1.2fr) minmax(0,.8fr); align-items:center; gap:12px; min-height:270px; }
.donut-layout svg { width:100%; min-width:220px; max-width:100%; height:250px; }
.donut-layout .chart-box { display:flex; align-items:center; justify-content:center; min-height:250px; overflow:visible; }
.chart-grid > .chart-card { display:flex; flex-direction:column; }
.chart-grid > .chart-card .donut-layout { flex:1; }
.donut-segment { cursor:pointer; stroke:#fff; stroke-width:2; outline:none; transform-box:view-box; transform-origin:116px 124px; transition:transform .24s ease,filter .24s ease,stroke-width .24s ease; }
.donut-segment:hover,.donut-segment:focus,.donut-segment.active { filter:brightness(.9); }
.donut-center-label { fill:#63708a; font-size:12px; text-anchor:middle; }
.donut-center-value { fill:#172033; font-size:18px; font-weight:700; text-anchor:middle; }
.legend-item { display:grid; grid-template-columns:10px minmax(0,1fr); gap:7px; align-items:start; min-height:32px; padding:4px 6px; border:0; border-radius:6px; text-align:left; font-size:12px; }
.legend-item:hover,.legend-item:focus,.legend-item.active { background:var(--accent-soft); outline:none; }
.legend-detail { display:block; margin-top:2px; color:var(--muted); }
.legend-item span { min-width:0; overflow-wrap:anywhere; }
#marketing-selling-point-secondary-legend { height:360px; max-height:360px; overflow-y:auto; overscroll-behavior:contain; align-content:start; gap:4px; padding-right:4px; }
#marketing-selling-point-secondary-legend .legend-item { min-height:32px; max-height:32px; overflow:hidden; }
#marketing-selling-point-secondary-legend .legend-item > span:last-child { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#marketing-selling-point-secondary-legend .legend-detail { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.script-section { display:grid; gap:16px; }
.script-card-header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }
.script-card-header h3 { margin:0; }
.script-card-actions { display:flex; align-items:center; justify-content:flex-end; gap:12px; }
.script-card-hint { color:var(--muted); font-size:12px; }
.script-card-header button { min-height:34px; padding:6px 10px; }
.script-formula-svg { width:min(760px,100%); height:290px; margin:0 auto; }
.script-formula-donut { display:grid; grid-template-columns:minmax(220px,1fr) minmax(180px,1fr); align-items:center; gap:14px; }
.script-formula-donut svg { min-width:220px; height:270px; }
.script-formula-segment { transform-box:view-box; transform-origin:116px 130px; transition:transform .24s ease,filter .24s ease,stroke-width .24s ease; }
.script-formula-segment.active,.marketing-selling-point-segment.active { filter:brightness(.9); stroke-width:3; }
.chart-tooltip { position:fixed; z-index:30; max-width:280px; padding:10px 12px; pointer-events:none; background:#172033; color:#fff; border-radius:8px; box-shadow:0 8px 24px rgba(15,23,42,.2); font-size:12px; line-height:1.65; }
.chart-tooltip[hidden] { display:none; }
.highlight-section { display:grid; gap:16px; }
.highlight-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:14px; }
.highlight-card { min-width:0; overflow:hidden; padding:12px; background:var(--surface); border:1px solid var(--line); border-radius:12px; box-shadow:0 8px 24px rgba(34,64,112,.05); }
.highlight-thumb { display:block; width:100%; aspect-ratio:9 / 16; min-height:0; padding:0; overflow:hidden; border:0; border-radius:8px; background:#e9eef6; }
.highlight-thumb img { display:block; width:100%; height:100%; object-fit:contain; }
.highlight-thumb:focus-visible { outline:3px solid rgba(37,99,235,.35); outline-offset:2px; }
.highlight-conditions,.highlight-values { display:flex; flex-direction:column; align-items:center; justify-content:center; width:100%; min-width:0; text-align:center; }
.highlight-conditions { gap:2px; min-height:18px; margin:8px 0 4px; }
.highlight-values { gap:2px; min-height:18px; margin:0 0 10px; }
.highlight-condition,.highlight-value { display:block; width:100%; min-width:0; overflow:hidden; text-align:center; text-overflow:ellipsis; white-space:nowrap; }
.highlight-condition { color:var(--text); font-size:12px; font-weight:600; line-height:1.35; }
.highlight-value { color:var(--muted); font-size:12px; line-height:1.35; }
.highlight-material { display:block; width:100%; min-width:0; padding:4px 0; color:#1d4ed8; cursor:pointer; font-size:12px; line-height:1.4; text-align:center; text-decoration:underline; text-decoration-thickness:1px; text-underline-offset:2px; white-space:normal; overflow-wrap:anywhere; word-break:break-word; }
.highlight-material:focus-visible { outline:3px solid rgba(37,99,235,.24); outline-offset:2px; border-radius:4px; }
.highlight-modal[hidden] { display:none; }
.highlight-modal { position:fixed; inset:0; z-index:40; display:grid; place-items:center; padding:24px; }
.highlight-modal-backdrop { position:absolute; inset:0; background:rgba(15,23,42,.72); }
.highlight-modal-panel { position:relative; display:flex; flex-direction:column; align-items:center; width:min(960px,100%); max-height:calc(100vh - 48px); padding:18px; border-radius:16px; background:var(--surface); box-shadow:0 24px 70px rgba(15,23,42,.35); }
.highlight-modal-close { align-self:flex-end; min-width:36px; min-height:36px; padding:0; border:0; border-radius:50%; color:var(--muted); font-size:24px; }
.highlight-modal-image { display:block; max-width:100%; max-height:calc(100vh - 150px); object-fit:contain; border-radius:10px; background:#0f172a; }
.highlight-modal-caption { margin:12px 0 0; color:var(--muted); font-size:13px; text-align:center; }
.single-material-shell { display:grid; grid-template-columns:230px minmax(0,1fr); gap:16px; align-items:start; }
.single-material-selector,.single-material-main { min-width:0; padding:16px; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.05); }
.single-material-selector { position:sticky; top:16px; max-height:calc(100vh - 32px); overflow:auto; }
.single-material-selector h2 { margin:0 0 5px; font-size:18px; }
.single-material-selector p { margin:0 0 12px; color:var(--muted); font-size:12px; line-height:1.5; }
.single-material-selector-list { display:grid; gap:6px; }
.single-material-selector-list button { width:100%; min-height:44px; padding:7px 9px; text-align:left; }
.single-material-selector-list button.active { border-color:var(--accent); background:var(--accent-soft); color:#1d4ed8; }
.single-material-selector-list strong { display:block; overflow-wrap:anywhere; word-break:break-word; font-size:12px; line-height:1.4; }
.single-material-selector-list span { display:block; margin-top:3px; overflow-wrap:anywhere; word-break:break-word; color:var(--muted); font-size:11px; line-height:1.35; }
.single-material-overview { display:grid; grid-template-columns:minmax(140px,18%) minmax(0,1fr); gap:16px; align-items:stretch; margin-bottom:14px; }
.single-material-meta { display:flex; min-width:0; align-items:center; }
.single-material-header { width:100%; }
.single-material-header h2 { margin:0; font-size:20px; }
.single-material-header p { margin:5px 0 0; color:var(--muted); font-size:12px; }
.single-material-tabs { position:sticky; top:0; z-index:10; display:flex; gap:7px; overflow:auto; margin:0 -16px 18px; padding:10px 16px 12px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.96); backdrop-filter:blur(8px); scroll-behavior:smooth; }
.single-material-tabs button { flex:0 0 auto; min-height:36px; padding:6px 10px; }
.single-material-tabs button.active { border-color:var(--accent); background:var(--accent); color:#fff; }
.single-material-panel { scroll-margin-top:74px; padding:16px 0 22px; border-bottom:1px solid var(--line); }
.single-material-panel:last-child { border-bottom:0; }
.single-panel-heading { display:flex; align-items:center; min-height:34px; margin:0 0 14px; padding-left:12px; border-left:4px solid var(--accent); }
.single-panel-heading h2 { margin:0; color:#1f2f49; font-size:18px; line-height:1.35; }
.filter-config-layout { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; align-items:start; }
.filter-config-shell { width:100%; min-width:0; overflow:hidden; padding:18px; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.06); }
.filter-config-shell h2 { margin:0 0 14px; font-size:20px; }
.filter-config-column { min-width:0; padding:16px; border:1px solid var(--line); border-radius:12px; background:var(--surface); }
.filter-config-column h3 { margin:0 0 6px; font-size:16px; }
.filter-config-note { margin:0 0 12px; color:var(--muted); font-size:12px; line-height:1.6; }
.filter-config-list { display:grid; gap:10px; margin:0; }
.filter-config-item { min-width:0; padding:12px; border:1px solid var(--line); border-radius:10px; background:#fbfdff; }
.filter-config-item dt { color:var(--muted); font-size:12px; }
.filter-config-item dd { margin:6px 0 0; overflow-wrap:anywhere; white-space:pre-wrap; line-height:1.55; }
.single-material-video { display:flex; width:min(100%,140px); min-height:0; aspect-ratio:9 / 16; align-items:center; justify-content:center; overflow:hidden; border:1px dashed var(--line-strong); border-radius:12px; background:#0f172a; color:var(--muted); text-align:center; }
.single-material-video video { display:block; width:100%; height:100%; object-fit:contain; }
.single-core-toolbar { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
.single-core-toolbar h3 { margin:0; font-size:16px; }
.single-core-toolbar p { margin:4px 0 0; color:var(--muted); font-size:12px; }
.single-core-toolbar button { min-height:34px; padding:6px 10px; }
.single-material-metrics,.single-material-tags,.single-formula-labels { display:flex; flex-wrap:wrap; gap:10px; }
.single-material-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
.single-material-metrics .metric-card { min-width:0; min-height:104px; }
.single-material-tag,.single-formula-chip { min-height:32px; padding:7px 10px; border:1px solid var(--line); border-radius:999px; background:#fbfdff; color:var(--text); }
.single-formula-chip { cursor:pointer; }
.single-formula-chip.active,.single-formula-term.active { border-color:var(--accent); background:var(--accent-soft); color:#1d4ed8; }
.single-formula-secondary { display:block; margin-top:4px; color:var(--muted); font-size:11px; }
.single-formula-sources { display:grid; gap:12px; margin-top:16px; }
.single-formula-source { padding:14px; border:1px solid var(--line); border-radius:10px; background:#fbfdff; }
.single-formula-source h3 { margin:0 0 7px; font-size:14px; }
.single-formula-source p { margin:0; color:var(--text); line-height:1.7; white-space:pre-wrap; }
.single-formula-overview { display:grid; gap:12px; margin-bottom:16px; padding:16px; border:1px solid var(--line); border-radius:12px; background:#fbfdff; }
.single-formula-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; }
.single-formula-toolbar h3 { margin:0; font-size:16px; }
.single-formula-legend { display:flex; flex-wrap:wrap; gap:12px 20px; color:var(--muted); font-size:12px; }
.single-formula-legend-item { display:inline-flex; align-items:center; gap:6px; }
.single-formula-legend-dot { width:9px; height:9px; border-radius:2px; }
.single-formula-chain { display:flex; flex-wrap:wrap; align-items:center; gap:7px; color:var(--muted); font-size:13px; }
.single-formula-chain-item { padding:4px 8px; border-radius:5px; font-weight:600; }
.single-formula-chain-plus { color:#a1aaba; }
.single-formula-highlight { padding:1px 3px; border-radius:3px; cursor:pointer; }
.single-formula-highlight.active { outline:2px solid currentColor; outline-offset:1px; }
.single-formula-source-header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:7px; }
.single-formula-source-header h3 { margin:0; }
.single-formula-copy { min-height:28px; padding:4px 8px; border:0; color:var(--muted); font-size:12px; }
.single-formula-term { margin:8px 6px 0 0; padding:4px 7px; border:1px solid var(--line); border-radius:6px; background:var(--surface); cursor:pointer; color:var(--muted); }
.single-trend-tabs { display:flex; flex-wrap:wrap; gap:7px; margin-bottom:10px; }
.single-trend-tabs button.active { border-color:var(--accent); background:var(--accent-soft); color:#1d4ed8; }
.single-trend-chart { width:100%; min-height:230px; margin-top:10px; overflow:hidden; border:1px solid var(--line); border-radius:10px; background:#fbfdff; }
.single-trend-chart svg { display:block; width:100%; min-width:100%; height:230px; }
.single-trend-controls { display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:10px; align-items:center; width:100%; margin-top:10px; color:var(--muted); font-size:12px; }
.single-trend-controls input { grid-column:1 / -1; width:100%; accent-color:var(--accent); }
.single-trend-controls output { font-variant-numeric:tabular-nums; }
.single-trend-point { fill:#2563eb; cursor:pointer; }
.single-trend-point:focus { outline:none; stroke:#172033; stroke-width:2; }
.single-portrait-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
.single-portrait-grid .overview-card { margin:0; }
.single-highlight-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
.single-highlight-card { min-width:0; padding:10px; border:1px solid var(--line); border-radius:10px; background:#fbfdff; }
.single-highlight-open { display:block; width:100%; padding:0; border:0; background:#e9eef6; cursor:pointer; }
.single-highlight-open img { display:block; width:100%; aspect-ratio:9 / 16; object-fit:contain; }
.single-highlight-card p { margin:7px 0 0; color:var(--muted); font-size:12px; line-height:1.45; }
.table-wrap { overflow:auto; background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(34,64,112,.05); }
table { width:100%; min-width:980px; border-collapse:separate; border-spacing:0; }
th,td { padding:14px 16px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; vertical-align:middle; }
th { position:sticky; top:0; z-index:2; background:#f8fafc; color:#45546b; font-size:13px; font-weight:650; }
th button { min-height:30px; padding:4px 7px; border:0; background:transparent; color:inherit; font:inherit; }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover td { background:#fbfdff; }
.material-name-column { position:sticky; left:0; z-index:1; min-width:380px; width:380px; background:var(--surface); box-shadow:8px 0 14px -14px rgba(23,32,51,.65); }
th.material-name-column { z-index:4; background:#f8fafc; }
tbody tr:hover .material-name-column { background:#fbfdff; }
.material-title { max-width:350px; overflow-wrap:anywhere; word-break:break-word; white-space:normal; font-weight:520; line-height:1.45; }
.material-id { margin-top:5px; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
mark { padding:0 2px; background:#fff0a8; border-radius:3px; }
.empty { padding:44px; text-align:center; color:var(--muted); }
.pager { justify-content:space-between; padding:14px 2px; color:var(--muted); font-size:14px; }
.pager button { min-height:34px; padding:5px 10px; }
body.modal-open { overflow:hidden; }
.field-modal[hidden] { display:none; }
.field-modal { position:fixed; inset:0; z-index:20; display:grid; place-items:center; padding:24px; }
.field-modal-backdrop { position:absolute; inset:0; background:rgba(15,23,42,.42); backdrop-filter:blur(2px); }
.field-modal-panel { position:relative; display:flex; flex-direction:column; width:min(1180px,100%); height:min(760px,calc(100vh - 48px)); overflow:hidden; background:var(--surface); border:1px solid var(--line); border-radius:16px; box-shadow:0 24px 70px rgba(15,23,42,.24); }
.field-modal-header { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; padding:22px 24px 16px; border-bottom:1px solid var(--line); }
.field-modal-header h2 { margin:0; font-size:20px; }
.field-modal-header p { margin:6px 0 0; color:var(--muted); font-size:13px; }
.field-modal-close { min-width:36px; min-height:36px; padding:0; border:0; border-radius:50%; color:var(--muted); font-size:24px; }
.field-modal-body { display:grid; grid-template-columns:190px minmax(0,1fr) 290px; flex:1; min-height:0; }
.field-modal-groups { min-width:0; padding:14px 10px; overflow:auto; background:#f8fafc; border-right:1px solid var(--line); }
.field-modal-groups button { display:flex; justify-content:space-between; width:100%; min-height:38px; margin-bottom:4px; padding:8px 10px; border:0; color:#45546b; text-align:left; }
.field-modal-groups button.active { background:var(--accent-soft); color:#1d4ed8; font-weight:650; box-shadow:inset 3px 0 0 var(--accent); }
.field-modal-main { display:flex; flex-direction:column; min-width:0; min-height:0; }
.field-modal-search { padding:14px 20px; border-bottom:1px solid var(--line); }
.field-modal-search input { width:100%; min-width:0; height:40px; padding:8px 12px; border:1px solid var(--line-strong); border-radius:8px; font-size:14px; }
.field-options { min-height:0; overflow:auto; padding:18px 22px 24px; }
.field-option-section + .field-option-section { margin-top:22px; }
.field-option-section h3 { margin:0 0 10px; font-size:14px; color:#263752; }
.field-option-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 12px; }
.field-option { display:flex; align-items:flex-start; gap:9px; min-height:40px; padding:9px 10px; border:1px solid transparent; border-radius:8px; color:#34445e; font-size:14px; cursor:pointer; }
.field-option:hover { background:#f8fafc; border-color:#e6ebf3; }
.field-option input { width:16px; height:16px; margin:1px 0 0; accent-color:var(--accent); }
.field-modal-selected { display:flex; flex-direction:column; min-width:0; min-height:0; background:#fbfcfe; border-left:1px solid var(--line); }
.field-modal-selected-header { display:flex; align-items:center; justify-content:space-between; padding:16px 16px 12px; border-bottom:1px solid var(--line); }
.field-modal-selected-header span { color:var(--muted); font-size:12px; }
.field-modal-selected-header button { min-height:28px; padding:4px 8px; border:0; color:var(--accent); font-size:12px; }
.field-modal-selected-list { min-height:0; margin:0; padding:12px; overflow:auto; list-style:none; }
.field-modal-selected-item { display:flex; align-items:center; gap:8px; min-height:38px; margin-bottom:7px; padding:7px 8px; background:#f1f3f7; border-radius:7px; color:#34445e; font-size:13px; }
.field-modal-selected-item .field-drag-handle { color:#8a96a8; }
.field-modal-selected-item button { min-width:24px; min-height:24px; margin-left:auto; padding:0; border:0; color:var(--muted); }
.field-required { margin-left:auto; color:var(--accent); font-size:12px; }
.field-modal-footer { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 24px; border-top:1px solid var(--line); }
.field-modal-footer p { margin:0; color:var(--muted); font-size:12px; }
.field-modal-footer-actions { display:flex; gap:8px; }
.field-modal-footer .primary { border-color:var(--accent); background:var(--accent); color:#fff; }
@media (max-width:900px) { .field-modal-body { grid-template-columns:160px minmax(0,1fr); } .field-modal-selected { grid-column:2; max-height:190px; border-top:1px solid var(--line); } }
@media (max-width:900px) { .highlight-grid,.single-highlight-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } .single-material-metrics { grid-template-columns:repeat(3,minmax(0,1fr)); } .single-portrait-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } }
@media (max-width:700px) { body { padding:18px 12px; } h1 { font-size:24px; } input[type=search] { min-width:0; } .field-modal { padding:10px; } .field-modal-panel { height:calc(100vh - 20px); } .field-modal-body { display:flex; flex-direction:column; } .field-modal-groups { display:flex; gap:6px; padding:10px; overflow-x:auto; border-right:0; border-bottom:1px solid var(--line); } .field-modal-groups button { flex:0 0 auto; width:auto; margin:0; white-space:nowrap; } .field-modal-selected { max-height:190px; border-top:1px solid var(--line); border-left:0; } .field-option-grid { grid-template-columns:1fr; } .field-modal-footer { align-items:flex-end; flex-direction:column; } .field-modal-footer-actions { width:100%; } .field-modal-footer-actions button { flex:1; } .field-modal-footer p { align-self:flex-start; } .material-name-column { min-width:300px; width:300px; } .chart-grid,.donut-layout,.script-formula-donut,.filter-config-layout { grid-template-columns:1fr; } .donut-layout svg,.script-formula-donut svg { margin:0 auto; } .section-nav { overflow:auto; } .section-nav button { flex:0 0 auto; } .highlight-grid,.single-highlight-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .single-material-shell { grid-template-columns:1fr; } .single-material-selector { position:static; max-height:none; } .single-material-selector-list { grid-template-columns:repeat(2,minmax(0,1fr)); } .single-material-overview { grid-template-columns:1fr; } .single-material-metrics,.single-portrait-grid { grid-template-columns:1fr; } .highlight-modal { padding:10px; } .highlight-modal-panel { max-height:calc(100vh - 20px); padding:10px; } }
 .single-formula-workspace { display:grid; gap:14px; }
 .single-formula-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; }
 .single-formula-toolbar h3 { margin:0; font-size:18px; }
 .single-formula-toolbar span { color:var(--muted); font-size:12px; }
 .single-formula-legend { display:flex; flex-wrap:wrap; gap:8px 16px; color:var(--muted); font-size:12px; }
 .single-formula-legend-item { display:inline-flex; align-items:center; gap:6px; }
 .single-formula-legend-dot { width:9px; height:9px; border-radius:2px; }
 .single-formula-flow { display:flex; align-items:stretch; gap:10px; overflow-x:auto; padding-bottom:2px; }
 .single-formula-flow-group { display:grid; grid-template-rows:auto 1fr; flex:1 1 0; gap:8px; min-width:180px; padding:10px; border:1px solid var(--line); border-radius:12px; background:#fbfdff; }
 .single-formula-node { width:100%; min-width:0; padding:8px 10px; border:1px solid color-mix(in srgb,var(--single-formula-color) 55%,var(--line)); border-radius:8px; background:color-mix(in srgb,var(--single-formula-color) 10%,white); color:var(--text); text-align:left; }
 .single-formula-node:hover,.single-formula-node:focus-visible,.single-formula-node.active { border-color:var(--single-formula-color); background:color-mix(in srgb,var(--single-formula-color) 20%,white); color:var(--single-formula-color); outline:none; }
 .single-formula-node .single-formula-level { display:block; margin-bottom:3px; color:var(--muted); font-size:10px; }
 .single-formula-node strong { display:block; overflow-wrap:anywhere; line-height:1.45; }
 .single-formula-primary { min-height:54px; text-align:center; }
 .single-formula-secondary-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:6px; align-content:start; }
 .single-formula-secondary { min-height:44px; padding:6px 8px; }
 .single-formula-empty { display:flex; align-items:center; justify-content:center; min-height:44px; color:var(--muted); font-size:12px; }
 .single-formula-flow-arrow { display:flex; flex:0 0 22px; align-items:center; justify-content:center; color:var(--muted); font-size:20px; }
 .single-script-highlight { cursor:pointer; }
 .single-script-highlight:not(.active) { opacity:.42; }
 @media (max-width:700px) { .single-formula-toolbar { align-items:flex-start; flex-direction:column; } .single-formula-flow { flex-direction:column; } .single-formula-flow-group { flex:0 0 auto; min-width:0; } .single-formula-flow-arrow { transform:rotate(90deg); min-height:20px; } }
</style>
</head>
<body>
<main>
<h1>云图素材榜单数据工作台</h1>
<nav class="section-nav" aria-label="报告部分"><button type="button" data-section="filters" class="active">素材筛选条件</button><button type="button" data-section="materials">素材榜单</button><button type="button" data-section="audience">人群数据</button><button type="button" data-section="scripts">脚本内容公式</button><button type="button" data-section="marketing-selling-points">营销卖点</button><button type="button" data-section="highlights">高光帧数据</button><button type="button" data-section="single-material-data">单素材数据</button></nav>
<section id="filters-panel" class="report-section" data-section-panel="filters"><div class="filter-config-shell"><h2>素材筛选条件</h2><div id="filter-config"></div></div></section>
<section id="materials-panel" class="report-section" data-section-panel="materials" hidden>
<div class="brand-tabs" data-brand-tabs="materials" role="tablist" aria-label="素材榜单品牌筛选"></div>
<div class="toolbar"><input id="search" type="search" placeholder="输入素材标题或 ID" aria-label="输入素材标题或 ID"><button id="field-button" type="button">自定义指标</button></div>
<section id="field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="field-management-title" hidden>
  <div class="field-modal-backdrop" data-field-action="cancel"></div>
  <div class="field-modal-panel">
    <header class="field-modal-header"><div><h2 id="field-management-title">自定义指标</h2><p>选择后按固定字段分组顺序呈现表格列</p></div><button class="field-modal-close" type="button" data-field-action="cancel" aria-label="关闭自定义指标">×</button></header>
    <div class="field-modal-body"><nav id="field-groups" class="field-modal-groups" aria-label="字段分组"></nav><section class="field-modal-main" aria-label="可选指标"><div class="field-modal-search"><input id="field-search" type="search" placeholder="搜索指标" aria-label="搜索指标"></div><div id="field-options" class="field-options"></div></section><aside class="field-modal-selected" aria-label="已选择指标"><div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="selected-count"></span></div><button type="button" data-field-action="reset">重置</button></div><ol id="selected-fields" class="field-modal-selected-list"></ol></aside></div>
    <footer class="field-modal-footer"><p>视频标题为固定列，素材 ID 显示在标题下方</p><div class="field-modal-footer-actions"><button type="button" data-field-action="cancel">取消</button><button class="primary" type="button" data-field-action="confirm">确定</button></div></footer>
  </div>
</section>
<section id="summary" class="summary"></section><div id="table-container" class="table-wrap"></div>
<div class="pager"><span id="range"></span><span><button id="first" type="button">首页</button><button id="previous" type="button">上一页</button><span id="pages"></span><button id="next" type="button">下一页</button><button id="last" type="button">尾页</button></span></div>
</section>
<section id="single-material-data-panel" class="report-section" data-section-panel="single-material-data" hidden>
  <div class="single-material-shell">
    <aside class="single-material-selector" aria-label="详情素材选择"><h2>单素材数据</h2><div id="single-material-selector" class="single-material-selector-list"></div></aside>
    <section class="single-material-main" aria-label="单素材详情">
      <div class="single-material-overview"><div id="single-material-video" class="single-material-video">本地视频不可用</div><div class="single-material-meta"><header class="single-material-header"><h2 id="single-material-title">单素材详情</h2><p id="single-material-id"></p></header></div></div>
      <nav id="single-material-tabs" class="single-material-tabs" role="tablist" aria-label="单素材数据锚点"><button id="single-material-tab-core" type="button" data-material-tab="core" aria-controls="single-material-core" role="tab">核心数据</button><button id="single-material-tab-portrait" type="button" data-material-tab="portrait" aria-controls="single-material-portrait" role="tab">人群画像</button><button id="single-material-tab-trends" type="button" data-material-tab="trends" aria-controls="single-material-trends" role="tab">秒级拆解</button><button id="single-material-tab-formula" type="button" data-material-tab="formula" aria-controls="single-material-formula" role="tab">内容公式</button><button id="single-material-tab-highlights" type="button" data-material-tab="highlights" aria-controls="single-material-highlights" role="tab">高光帧数据</button></nav>
      <section id="single-material-core" class="single-material-panel" data-material-panel="core" role="tabpanel" aria-labelledby="single-material-tab-core"></section>
      <section id="single-material-portrait" class="single-material-panel" data-material-panel="portrait" role="tabpanel" aria-labelledby="single-material-tab-portrait"></section>
      <section id="single-material-trends" class="single-material-panel" data-material-panel="trends" role="tabpanel" aria-labelledby="single-material-tab-trends"></section>
      <section id="single-material-formula" class="single-material-panel" data-material-panel="formula" role="tabpanel" aria-labelledby="single-material-tab-formula"></section>
      <section id="single-material-highlights" class="single-material-panel" data-material-panel="highlights" role="tabpanel" aria-labelledby="single-material-tab-highlights"></section>
    </section>
  </div>
</section>
<section id="single-core-field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="single-core-field-management-title" hidden>
  <div class="field-modal-backdrop" data-single-core-field-action="cancel"></div>
  <div class="field-modal-panel">
    <header class="field-modal-header"><div><h2 id="single-core-field-management-title">自定义指标</h2><p>选择后按固定字段分组顺序呈现数据卡片</p></div><button class="field-modal-close" type="button" data-single-core-field-action="cancel" aria-label="关闭核心数据自定义指标">×</button></header>
    <div class="field-modal-body single-core-field-body"><nav id="single-core-field-groups" class="field-modal-groups" aria-label="核心指标分组"></nav><section class="field-modal-main" aria-label="可选核心指标"><div class="field-modal-search"><input id="single-core-field-search" type="search" placeholder="搜索核心指标" aria-label="搜索核心指标"></div><div id="single-core-field-options" class="field-options"></div></section><aside class="field-modal-selected" aria-label="已选择核心指标"><div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="single-core-selected-count"></span></div><button type="button" data-single-core-field-action="reset">重置</button></div><ol id="single-core-selected-fields" class="field-modal-selected-list"></ol></aside></div>
    <footer class="field-modal-footer"><p>标题、素材 ID、榜单排名和首投日期固定显示在详情头部</p><div class="field-modal-footer-actions"><button type="button" data-single-core-field-action="cancel">取消</button><button class="primary" type="button" data-single-core-field-action="confirm">确定</button></div></footer>
  </div>
</section>
<section id="audience-panel" class="audience-section" data-section-panel="audience" hidden>
  <div class="brand-tabs" data-brand-tabs="audience" role="tablist" aria-label="人群数据品牌筛选"></div>
  <div id="audience-unavailable" class="audience-card empty" hidden>人群数据不可用</div>
  <div id="audience-content">
    <div class="audience-card chart-card"><h3>八大人群分布</h3><div id="audience-eight-groups" class="chart-box"></div></div>
    <div class="chart-grid">
      <div class="audience-card chart-card"><h3>性别分布</h3><div class="donut-layout"><div id="audience-gender" class="chart-box"></div><div id="legend-gender" class="legend" aria-label="性别图例"></div></div></div>
      <div class="audience-card chart-card"><h3>年龄分布</h3><div class="donut-layout"><div id="audience-age" class="chart-box"></div><div id="legend-age" class="legend" aria-label="年龄图例"></div></div></div>
    </div>
  </div>
</section>
<section id="scripts-panel" class="script-section" data-section-panel="scripts" hidden>
  <div class="brand-tabs" data-brand-tabs="scripts" role="tablist" aria-label="脚本内容公式品牌筛选"></div>
  <div class="audience-card chart-card" data-script-formula-card>
    <div class="script-card-header"><h3 id="script-formula-title">一级内容公式</h3><div class="script-card-actions"><span id="script-formula-hint" class="script-card-hint">点击一级内容公式柱状图可以显示二级内容公式分布</span></div></div>
    <div id="script-formula-chart" class="chart-box"></div>
  </div>
  <div id="script-formula-secondary-card" class="audience-card chart-card" hidden>
    <div class="script-card-header"><h3 id="script-formula-secondary-title">二级内容公式分布</h3><div class="script-card-actions"><button id="script-formula-secondary-back" type="button">收起二级内容公式</button></div></div>
    <div class="script-formula-donut"><div id="script-formula-secondary-chart" class="chart-box"></div><div id="script-formula-secondary-legend" class="legend" aria-label="二级内容公式图例"></div></div>
  </div>
</section>
<section id="marketing-selling-points-panel" class="script-section" data-section-panel="marketing-selling-points" hidden>
  <div class="brand-tabs" data-brand-tabs="marketing-selling-points" role="tablist" aria-label="营销卖点品牌筛选"></div>
  <div class="audience-card chart-card" data-marketing-selling-point-card>
    <div class="script-card-header"><h3 id="marketing-selling-point-title">一级营销卖点</h3><div class="script-card-actions"><span id="marketing-selling-point-hint" class="script-card-hint">点击一级营销卖点柱状图可以显示二级营销卖点分布</span></div></div>
    <div id="marketing-selling-point-chart" class="chart-box"></div>
  </div>
  <div id="marketing-selling-point-secondary-card" class="audience-card chart-card" hidden>
    <div class="script-card-header"><h3 id="marketing-selling-point-secondary-title">二级营销卖点分布</h3><div class="script-card-actions"><button id="marketing-selling-point-secondary-back" type="button">收起二级营销卖点</button></div></div>
    <div class="script-formula-donut"><div id="marketing-selling-point-secondary-chart" class="chart-box"></div><div id="marketing-selling-point-secondary-legend" class="legend" aria-label="二级营销卖点图例，可上下滚动查看全部"></div></div>
  </div>
</section>
<section id="highlights-panel" class="highlight-section" data-section-panel="highlights" hidden>
  <div class="brand-tabs" data-brand-tabs="highlights" role="tablist" aria-label="高光帧数据品牌筛选"></div>
  <div id="highlight-unavailable" class="audience-card empty" hidden>高光帧数据不可用</div>
  <div id="highlight-content">
    <div id="highlight-grid" class="highlight-grid"></div>
    <div class="pager"><span id="highlight-range"></span><span><button id="highlight-first" type="button">首页</button><button id="highlight-previous" type="button">上一页</button><span id="highlight-pages"></span><button id="highlight-next" type="button">下一页</button><button id="highlight-last" type="button">尾页</button></span></div>
  </div>
</section>
<section id="single-highlight-modal" class="highlight-modal" role="dialog" aria-modal="true" aria-labelledby="single-highlight-modal-caption" hidden><div class="highlight-modal-backdrop" data-single-highlight-action="close"></div><div class="highlight-modal-panel"><button id="single-highlight-modal-close" class="highlight-modal-close" type="button" data-single-highlight-action="close" aria-label="关闭单素材高光帧大图">×</button><img id="single-highlight-modal-image" class="highlight-modal-image" alt=""><p id="single-highlight-modal-caption" class="highlight-modal-caption"></p></div></section>
<section id="highlight-modal" class="highlight-modal" role="dialog" aria-modal="true" aria-labelledby="highlight-modal-caption" hidden>
  <div class="highlight-modal-backdrop" data-highlight-action="close"></div>
  <div class="highlight-modal-panel">
    <button id="highlight-modal-close" class="highlight-modal-close" type="button" data-highlight-action="close" aria-label="关闭高光帧大图">×</button>
    <img id="highlight-modal-image" class="highlight-modal-image" alt="">
    <p id="highlight-modal-caption" class="highlight-modal-caption"></p>
  </div>
</section>
<div id="chart-tooltip" class="chart-tooltip" role="status" aria-live="polite" hidden></div>
</main>
<script id="overview-model" type="application/json">''' + embedded + r'''</script>
<script>
(() => {
  const model = JSON.parse(document.getElementById("overview-model").textContent);
  const pageSize = 10;
  const highlightPageSize = 12;
  const singleHighlightPageSize = 8;
  const storageKey = `yuntu-overview:${model.runId}:v${model.schemaVersion}`;
  const fieldGroupOrder = ["素材基础信息", "内容曝光与播放", "点击与转化", "互动指标"];
  const presentationFields = model.fields.filter(field => field.id !== "material_id");
  const fieldRank = Object.fromEntries(presentationFields.map((field, index) => [field.id, index]));
  const groupRank = Object.fromEntries(fieldGroupOrder.map((group, index) => [group, index]));
  const orderedFields = [...presentationFields].sort((left, right) => (groupRank[left.group] ?? 99) - (groupRank[right.group] ?? 99) || fieldRank[left.id] - fieldRank[right.id]);
  const fieldMap = Object.fromEntries(orderedFields.map(field => [field.id, field]));
  const groupNames = fieldGroupOrder.filter(group => orderedFields.some(field => field.group === group));
  const requiredFieldIds = ["title"].filter(id => fieldMap[id]);
  const defaultFieldIds = Array.isArray(model.defaultFieldIds) ? model.defaultFieldIds : requiredFieldIds;
  const initialSort = model.queryContext.materialList && model.queryContext.materialList.sort || {};
  const state = { query: "", exactMaterialId: null, pendingMaterialId: null, visibleFieldIds: [], draftFieldIds: null, fieldSearch: "", activeFieldGroup: groupNames[0] || "", sort: { field: initialSort.field || null, direction: initialSort.direction || "none" }, page: 1 };
  const search = document.getElementById("search"), modal = document.getElementById("field-management"), fieldGroups = document.getElementById("field-groups"), fieldSearch = document.getElementById("field-search"), fieldOptions = document.getElementById("field-options"), selectedFields = document.getElementById("selected-fields"), selectedCount = document.getElementById("selected-count"), tableContainer = document.getElementById("table-container");
  const highlightModal = document.getElementById("highlight-modal"), highlightModalImage = document.getElementById("highlight-modal-image"), highlightModalCaption = document.getElementById("highlight-modal-caption"), highlightGrid = document.getElementById("highlight-grid");
  function normalize(value) { return String(value == null ? "" : value).normalize("NFKC").toLocaleLowerCase().trim(); }
  function orderedVisibleFieldIds(ids) { const selected = new Set(Array.isArray(ids) ? ids : []); requiredFieldIds.forEach(id => selected.add(id)); return orderedFields.filter(field => selected.has(field.id)).map(field => field.id); }
  function missing(value) { return value == null || value === ""; }
  function loadVisibleFields() { try { const stored = JSON.parse(localStorage.getItem(storageKey) || "null"); state.visibleFieldIds = orderedVisibleFieldIds(Array.isArray(stored) ? stored : defaultFieldIds); } catch (_) { state.visibleFieldIds = orderedVisibleFieldIds(defaultFieldIds); } }
  function saveVisibleFields() { try { localStorage.setItem(storageKey, JSON.stringify(state.visibleFieldIds)); } catch (_) {} }
  function escaped(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[char])); }
  const chartTooltip = document.getElementById("chart-tooltip");
  let audience = model.audienceOverview || {status: "unavailable"};
  let scriptFormula = model.scriptFormulaOverview || {status: "unavailable"};
  let marketingSellingPointOverview = model.marketingSellingPointOverview || {status: "unavailable"};
  let highlightOverview = model.highlightOverview || {status: "unavailable", cards: []};
  const brandOverview = model.brandOverview || {status: "unavailable", tabs: [], materialIdsByTab: {}, views: {}};
  const brandState = {selected: brandOverview.status === "available" && brandOverview.tabs.some(tab => tab.id === "all") ? "all" : null};
  const colors = ["#2563eb", "#60a5fa", "#14b8a6", "#f59e0b", "#ef6a6a", "#8b5cf6", "#64748b", "#22c55e"];
  const audienceState = {donutSelection: {gender: null, age: null}};
  const scriptFormulaState = {selectedPrimary: null, selectedSecondary: null};
  const marketingSellingPointState = {selectedPrimary: null, selectedSecondary: null};
  const highlightState = {page: 1, lastFocused: null};
  const singleMaterialOverview = model.singleMaterialOverview || {status: "unavailable", materials: []};
  const singleMaterials = Array.isArray(singleMaterialOverview.materials) ? singleMaterialOverview.materials : [];
  const singleState = {materialId: singleMaterials[0]?.materialId || null, tab: "core", trend: "点赞指数", span: 100, page: 1, formulaId: null, lastFocused: null};
  const singleCoreStorageKey = `${storageKey}:single-core`;
  const singleCoreFieldGroupOrder = ["展现指标", "互动/种草指标", "转化指标"];
  const singleCoreFieldGroupById = {"metrics.show_cnt": "展现指标", "metrics.ctr": "展现指标", "metrics.play_over_rate": "展现指标", "metrics.play_3s_rate": "展现指标", "metrics.play_5s_rate": "展现指标", "metrics.interact_rate": "互动/种草指标", "metrics.like_rate": "互动/种草指标", "metrics.comment_rate": "互动/种草指标", "metrics.share_rate": "互动/种草指标", "metrics.cvr": "转化指标", "metrics.pvr": "转化指标", "metrics.product_wish_button_buy_cart_rate": "转化指标"};
  const singleCoreFixedFields = [];
  const singleCoreState = {visibleFieldIds: null, draftFieldIds: null, fieldSearch: "", activeFieldGroup: singleCoreFieldGroupOrder[0]};
  function activeBrandTab() { return (brandOverview.tabs || []).find(tab => tab.id === brandState.selected) || (brandOverview.tabs || []).find(tab => tab.id === "all") || null; }
  function updateBrandTabButtons() { document.querySelectorAll("[data-brand-tab-id]").forEach(button => { const active = button.dataset.brandTabId === brandState.selected; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); }); }
  function renderBrandTabs() { document.querySelectorAll("[data-brand-tabs]").forEach(container => { const tabs = brandOverview.status === "available" ? (brandOverview.tabs || []) : []; container.innerHTML = tabs.map(tab => `<button type="button" role="tab" data-brand-tab-id="${escaped(tab.id)}" aria-selected="${tab.id === brandState.selected}">${escaped(tab.label)}<span class="brand-tabs-count">${escaped(tab.count)}</span></button>`).join(""); }); updateBrandTabButtons(); }
  function applyBrandView() { const view = brandOverview.views?.[brandState.selected] || {}; audience = view.audienceOverview || model.audienceOverview || {status: "unavailable"}; scriptFormula = view.scriptFormulaOverview || model.scriptFormulaOverview || {status: "unavailable"}; marketingSellingPointOverview = view.marketingSellingPointOverview || model.marketingSellingPointOverview || {status: "unavailable"}; highlightOverview = view.highlightOverview || model.highlightOverview || {status: "unavailable", cards: []}; }
  function setBrandSelection(tabId) { if (!brandOverview.tabs.some(tab => tab.id === tabId)) return; brandState.selected = tabId; applyBrandView(); state.page = 1; state.exactMaterialId = null; scriptFormulaState.selectedPrimary = null; scriptFormulaState.selectedSecondary = null; marketingSellingPointState.selectedPrimary = null; marketingSellingPointState.selectedSecondary = null; highlightState.page = 1; updateBrandTabButtons(); render(); renderAudience(); renderScriptFormula(); renderMarketingSellingPoints(); renderHighlights(); }
  const singleSelector = document.getElementById("single-material-selector"), singleCoreModal = document.getElementById("single-core-field-management"), singleCoreFieldGroups = document.getElementById("single-core-field-groups"), singleCoreFieldSearch = document.getElementById("single-core-field-search"), singleCoreFieldOptions = document.getElementById("single-core-field-options"), singleCoreSelectedFields = document.getElementById("single-core-selected-fields"), singleCoreSelectedCount = document.getElementById("single-core-selected-count"), singleHighlightModal = document.getElementById("single-highlight-modal"), singleHighlightImage = document.getElementById("single-highlight-modal-image"), singleHighlightCaption = document.getElementById("single-highlight-modal-caption");
  function currentSingleMaterial() { return singleMaterials.find(item => item.materialId === singleState.materialId) || singleMaterials[0] || null; }
  function singleText(value) { return escaped(value == null || value === "" ? "—" : value); }
  const fieldLabelById = Object.fromEntries((Array.isArray(model.fields) ? model.fields : []).filter(field => field && field.id && field.label).map(field => [field.id, field.label]));
  const filterConfigFieldAliases = { EXPOSURE_TOP1000: "曝光量 Top1000" };
  const filterConfigValueAliases = { LAST_7_DAYS: "近7天", SPECIFIED_BRANDS: "指定品牌" };
  const filterConfigLabels = { industry: "行业", period: "投放周期", startDate: "开始日期", endDate: "结束日期", start_date: "开始日期", end_date: "结束日期", type: "类型", filters: "筛选条件", ages: "年龄", genders: "性别", crowd_groups: "八大人群", brand_scope: "品牌范围", brands: "品牌", ranking: "榜单指标", metric: "指标", field: "字段", operator: "条件", value: "值", materialList: "素材列表", sort: "排序", direction: "方向" };
  const filterConfigOperators = { eq: "等于", neq: "不等于", gt: "大于", gte: "大于等于", lt: "小于", lte: "小于等于", in: "包含", not_in: "不包含", asc: "升序", desc: "降序" };
  function filterConfigHasValue(value) {
    if (value == null || value === "") return false;
    if (Array.isArray(value)) return value.some(filterConfigHasValue);
    if (typeof value === "object") return Object.values(value).some(filterConfigHasValue);
    return true;
  }
  function filterConfigFieldLabel(value) {
    const raw = String(value == null ? "" : value);
    return fieldLabelById[raw] || filterConfigFieldAliases[raw] || (raw ? `未找到中文映射：${raw}` : "未配置");
  }
  function filterConfigText(value, key) {
    if (value == null || value === "") return "未配置";
    if (Array.isArray(value)) return value.length ? value.map(item => filterConfigText(item, key)).join("、") : "不限";
    if (typeof value === "object") {
      if (Object.prototype.hasOwnProperty.call(value, "field") && Object.prototype.hasOwnProperty.call(value, "operator")) return `${filterConfigFieldLabel(value.field)} ${filterConfigOperators[value.operator] || filterConfigText(value.operator)} ${filterConfigText(value.value, "value")}`;
      return Object.entries(value).filter(([, item]) => filterConfigHasValue(item)).map(([entryKey, item]) => `${filterConfigLabels[entryKey] || entryKey}：${entryKey === "field" || entryKey === "metric" ? filterConfigFieldLabel(item) : filterConfigText(item, entryKey)}`).join("；") || "未配置";
    }
    if (key === "field" || key === "metric") return filterConfigFieldLabel(value);
    return filterConfigValueAliases[value] || filterConfigOperators[value] || String(value);
  }
  function renderFilterConfig() {
    const context = model.queryContext || {}, entries = [];
    const add = (label, value) => { if (filterConfigHasValue(value)) entries.push({ label, value: filterConfigText(value) }); };
    add(filterConfigLabels.industry, context.industry);
    add(filterConfigLabels.period, context.period);
    add(filterConfigLabels.filters, context.filters);
    add(filterConfigLabels.ranking, context.ranking);
    add(filterConfigLabels.materialList, context.materialList);
    const highlightEntries = Array.isArray(model.highlightFilterContext) ? model.highlightFilterContext : [];
    const renderColumn = (title, note, items, empty) => `<section class="filter-config-column"><h3>${escaped(title)}</h3><p class="filter-config-note">${escaped(note)}</p>${items.length ? `<dl class="filter-config-list">${items.map(item => `<div class="filter-config-item"><dt>${escaped(item.label)}</dt><dd>${escaped(item.value)}</dd></div>`).join("")}</dl>` : `<div class="empty">${escaped(empty)}</div>`}</section>`;
    const target = document.getElementById("filter-config");
    target.innerHTML = `<div class="filter-config-layout">${renderColumn("素材列表筛选条件", "仅展示本次实际影响素材入选或排序的条件。", entries, "未配置有效素材列表条件")}${renderColumn("高光帧筛选标准", "仅展示本次实际启用的高光帧选择规则。", highlightEntries, "未配置有效高光帧筛选标准")}</div>`;
  }
  function singleDisplay(value, kind) { if (value == null || value === "") return "—"; if (kind === "rate") return `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2}).format(Number(value) * 100)}%`; if (["number", "integer"].includes(kind)) return new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2}).format(Number(value)); return singleText(value); }
  function copyText(value, trigger) { const done = () => { const original = trigger.textContent; trigger.textContent = "已复制"; window.setTimeout(() => { trigger.textContent = original; }, 1200); }; if (navigator.clipboard?.writeText) { navigator.clipboard.writeText(value).then(done).catch(() => {}); return; } const helper = document.createElement("textarea"); helper.value = value; helper.setAttribute("readonly", ""); helper.style.position = "fixed"; helper.style.opacity = "0"; document.body.appendChild(helper); helper.select(); try { if (document.execCommand("copy")) done(); } finally { helper.remove(); } }
  function renderSingleSelector() { if (!singleSelector) return; if (!singleMaterials.length) { singleSelector.innerHTML = '<div class="empty">详情素材不可用</div>'; return; } singleSelector.innerHTML = singleMaterials.map(item => `<button type="button" data-material-selector="${escaped(item.materialId)}" class="${item.materialId === singleState.materialId ? "active" : ""}" aria-pressed="${item.materialId === singleState.materialId}"><strong>TOP${escaped(item.rank)} · ${singleText(item.title)}</strong><span>素材ID：${escaped(item.materialId)}</span></button>`).join(""); }
  function singleCoreFields(material) { return (material?.core?.fields || []).filter(field => !["title", "publish_time", "video_duration", "rank"].includes(field.id)); }
  function singleCoreFieldGroup(field) { return field?.group || singleCoreFieldGroupById[field?.id] || "素材基础信息"; }
  function singleCoreDialogFields(material) { return [...singleCoreFixedFields, ...singleCoreFields(material)]; }
  function orderedSingleCoreFieldIds(ids, material) { const selected = new Set(Array.isArray(ids) ? ids : []); return singleCoreFields(material).filter(field => selected.has(field.id)).map(field => field.id); }
  function loadSingleCoreFields(material) { if (singleCoreState.visibleFieldIds) return; let stored = null; try { stored = JSON.parse(localStorage.getItem(singleCoreStorageKey) || "null"); } catch (_) {} const available = singleCoreFields(material); const defaults = material?.core?.defaultFieldIds || available.map(field => field.id); singleCoreState.visibleFieldIds = orderedSingleCoreFieldIds(Array.isArray(stored) ? stored : defaults, material); }
  function saveSingleCoreFields() { try { localStorage.setItem(singleCoreStorageKey, JSON.stringify(singleCoreState.visibleFieldIds || [])); } catch (_) {} }
  function singlePanelHeading(title) { return `<header class="single-panel-heading"><h2>${escaped(title)}</h2></header>`; }
  function ensureSinglePanelHeadings() { const titles = {"single-material-filters":"素材筛选条件", "single-material-core":"核心数据", "single-material-portrait":"人群画像", "single-material-trends":"秒级拆解", "single-material-formula":"内容公式", "single-material-highlights":"高光帧数据"}; Object.entries(titles).forEach(([id, title]) => { const panel = document.getElementById(id); if (!panel || panel.querySelector(":scope > .single-panel-heading")) return; const heading = document.createElement("header"); heading.className = "single-panel-heading"; const label = document.createElement("h2"); label.textContent = title; heading.append(label); panel.prepend(heading); }); }
  function renderSingleCore(material) { const content = document.getElementById("single-material-core"), fields = singleCoreFields(material); loadSingleCoreFields(material); const visible = orderedSingleCoreFieldIds(singleCoreState.visibleFieldIds, material).map(id => fields.find(field => field.id === id)).filter(Boolean); content.innerHTML = `${singlePanelHeading("核心数据")}${material?.core?.status !== "available" ? '<div class="empty">核心数据不可用</div>' : `<div class="single-core-toolbar"><div><p>可按需要选择并调整展示的指标</p></div><button id="single-core-field-button" type="button">自定义指标</button></div><div class="single-material-metrics">${visible.map(field => `<article class="metric-card"><span>${escaped(field.label)}</span><strong>${singleDisplay(field.value, field.kind)}</strong></article>`).join("") || '<div class="empty">暂无已选择指标</div>'}</div>`}`; }
  function renderSingleCoreFieldDialog() { const material = currentSingleMaterial(), fields = singleCoreFields(material), dialogFields = singleCoreDialogFields(material); if (!material || !singleCoreModal) return; singleCoreState.draftFieldIds = orderedSingleCoreFieldIds(singleCoreState.draftFieldIds || singleCoreState.visibleFieldIds, material); const query = normalize(singleCoreState.fieldSearch); singleCoreFieldGroups.innerHTML = singleCoreFieldGroupOrder.map(group => { const count = dialogFields.filter(field => singleCoreFieldGroup(field) === group).length; return `<button type="button" class="${!query && singleCoreState.activeFieldGroup === group ? "active" : ""}" data-single-core-field-group="${escaped(group)}">${escaped(group)}<span>${count}</span></button>`; }).join(""); const available = dialogFields.filter(field => (!query && singleCoreFieldGroup(field) === singleCoreState.activeFieldGroup) || (query && (normalize(field.label).includes(query) || normalize(field.id).includes(query)))); const grouped = {}; available.forEach(field => (grouped[singleCoreFieldGroup(field)] ||= []).push(field)); singleCoreFieldOptions.innerHTML = Object.entries(grouped).map(([group, groupFields]) => `<section class="field-option-section"><h3>${escaped(group)}</h3><div class="field-option-grid">${groupFields.map(field => { const fixed = field.id.startsWith("fixed."); return `<label class="field-option"><input type="checkbox" data-single-core-field-id="${escaped(field.id)}"${fixed ? " disabled" : ""}${fixed && field.checked || !fixed && singleCoreState.draftFieldIds.includes(field.id) ? " checked" : ""}><span>${escaped(field.label)}</span>${fixed && field.checked ? '<span class="field-required">固定</span>' : ""}</label>`; }).join("")}</div></section>`).join("") || '<div class="empty">没有匹配指标</div>'; const selected = singleCoreState.draftFieldIds.map(id => fields.find(field => field.id === id)).filter(Boolean); singleCoreSelectedCount.textContent = `（${selected.length}）`; singleCoreSelectedFields.innerHTML = selected.map(field => `<li class="field-modal-selected-item"><span class="field-drag-handle">☷</span><span>${escaped(field.label)}</span><button type="button" data-single-core-remove-field-id="${escaped(field.id)}" aria-label="移除${escaped(field.label)}">×</button></li>`).join("") || '<li class="empty">暂无已选择指标</li>'; }
  function openSingleCoreFieldDialog() { const material = currentSingleMaterial(); if (!material || !singleCoreModal) return; loadSingleCoreFields(material); singleCoreState.draftFieldIds = [...(singleCoreState.visibleFieldIds || [])]; singleCoreState.fieldSearch = ""; singleCoreState.activeFieldGroup = singleCoreFieldGroupOrder.find(group => singleCoreDialogFields(material).some(field => singleCoreFieldGroup(field) === group)) || singleCoreFieldGroupOrder[0]; singleCoreFieldSearch.value = ""; singleCoreModal.hidden = false; document.body.classList.add("modal-open"); renderSingleCoreFieldDialog(); singleCoreFieldSearch.focus(); }
  function closeSingleCoreFieldDialog() { if (!singleCoreModal) return; singleCoreModal.hidden = true; singleCoreState.draftFieldIds = null; singleCoreState.fieldSearch = ""; document.body.classList.remove("modal-open"); }
  function confirmSingleCoreFieldDialog() { const material = currentSingleMaterial(); singleCoreState.visibleFieldIds = orderedSingleCoreFieldIds(singleCoreState.draftFieldIds, material); saveSingleCoreFields(); closeSingleCoreFieldDialog(); renderSingleMaterial(); }
  function renderSinglePortrait(material) { const content = document.getElementById("single-material-portrait"), portrait = material?.portrait; if (!portrait || portrait.status !== "available") { content.innerHTML = `${singlePanelHeading("人群画像")}<div class="empty">人群画像不可用：${singleText(portrait?.reason)}</div>`; return; } const groups = [["八大人群", portrait.eightCrowds], ["年龄", portrait.ageFeatures], ["性别", portrait.genderFeatures]]; content.innerHTML = `${singlePanelHeading("人群画像")}<div class="single-portrait-grid">${groups.map(([label, values]) => `<div class="overview-card"><h3>${label}</h3><div class="single-material-tags">${(values || []).length ? values.map(value => `<span class="single-material-tag">${singleText(value)}</span>`).join("") : '<span class="empty">暂无该类标签</span>'}</div></div>`).join("")}</div>`; }
  function renderSingleTrendChart(metric) { const container = document.getElementById("single-trend-chart"); const series = metric?.series || []; if (!series.length) { container.innerHTML = `<div class="empty">${singleText(metric?.reason || "暂无可验证的趋势数据")}</div>`; return; } const endIndex = Math.max(0, Math.ceil((series.length - 1) * singleState.span / 100)), visible = series.slice(0, endIndex + 1), width = 640, height = 230, left = 8, right = 8, top = 18, bottom = 34, max = Math.max(...visible.map(item => Number(item.value)), 1), x = index => left + (visible.length === 1 ? 0 : index * (width - left - right) / (visible.length - 1)), y = value => height - bottom - Number(value) / max * (height - top - bottom), path = visible.map((item, index) => `${index ? "L" : "M"} ${x(index).toFixed(2)} ${y(item.value).toFixed(2)}`).join(" "); const points = visible.map((item, index) => `<circle class="single-trend-point" data-single-trend-second="${escaped(item.second)}" data-single-trend-value="${escaped(item.value)}" cx="${x(index).toFixed(2)}" cy="${y(item.value).toFixed(2)}" r="4" tabindex="0" role="img" aria-label="${escaped(item.second)}秒 ${escaped(item.value)}"></circle>`).join(""); container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escaped(metric.label)}秒级趋势"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-right}" y2="${height-bottom}"></line><path d="${path}" fill="none" stroke="#2563eb" stroke-width="2"></path>${points}<text class="chart-label" x="${left}" y="${height-10}">${escaped(visible[0].second)}s</text><text class="chart-label" x="${width-right}" y="${height-10}" text-anchor="end">${escaped(visible[visible.length-1].second)}s</text></svg>`; container.querySelectorAll(".single-trend-point").forEach(point => { const show = () => { chartTooltip.textContent = `${metric.label} · ${point.dataset.singleTrendSecond}s · ${point.dataset.singleTrendValue}`; chartTooltip.hidden = false; const rect = point.getBoundingClientRect(); chartTooltip.style.left = `${Math.min(window.innerWidth - 220, rect.right + 8)}px`; chartTooltip.style.top = `${Math.max(12, rect.top - 34)}px`; }; point.addEventListener("pointerenter", show); point.addEventListener("focus", show); point.addEventListener("pointerleave", hideChartTooltip); point.addEventListener("blur", hideChartTooltip); }); }
  function renderSingleTrends(material) { const content = document.getElementById("single-material-trends"), metrics = material?.trends?.metrics || [], metric = metrics.find(item => item.id === singleState.trend) || metrics[0]; if (!metric) { content.innerHTML = `${singlePanelHeading("秒级拆解")}<div class="empty">秒级趋势不可用</div>`; return; } singleState.trend = metric.id; content.innerHTML = `${singlePanelHeading("秒级拆解")}<div class="single-trend-tabs">${metrics.map(item => `<button type="button" data-single-trend="${escaped(item.id)}" class="${item.id === singleState.trend ? "active" : ""}" aria-pressed="${item.id === singleState.trend}">${escaped(item.label)}</button>`).join("")}</div><div id="single-trend-chart" class="single-trend-chart"></div><div class="single-trend-controls"><div>图表展示比例</div><output id="singleTrendSpanValue" for="singleTrendSpan">${singleState.span}%</output><input id="singleTrendSpan" data-single-range="span" type="range" min="20" max="100" value="${singleState.span}" aria-label="图表展示比例"></div>`; renderSingleTrendChart(metric); }
  function setFormulaSelection(id) { singleState.formulaId = singleState.formulaId === id ? null : id; const formula = currentSingleMaterial()?.formula || {}; const group = (formula.labels || []).find(item => item.id === singleState.formulaId || (item.secondary || []).some(child => child.id === singleState.formulaId)); const activeIds = singleState.formulaId == null ? new Set() : new Set(singleState.formulaId === group?.id ? [group.id, ...(group.secondary || []).map(child => child.id)] : [singleState.formulaId]); document.querySelectorAll("[data-formula-id]").forEach(item => { const active = activeIds.has(item.dataset.formulaId); item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active)); }); }
  const formulaColors = ["#ef6a6a", "#8b5cf6", "#22b573", "#f59e0b", "#64748b"];
  function formulaColor(formula, id) { const index = (formula.labels || []).findIndex(item => item.id === id || (item.secondary || []).some(child => child.id === id)); return formulaColors[(index < 0 ? 0 : index) % formulaColors.length]; }
  function formulaSourceText(source, formula) { const raw = typeof source?.text === "string" ? source.text : ""; if (!raw) return singleText(null); const matches = []; (source.terms || []).forEach(term => (term.texts || []).forEach(text => { if (typeof text !== "string" || !text) return; let from = 0; while (from < raw.length) { const start = raw.indexOf(text, from); if (start < 0) break; matches.push({start, end: start + text.length, text, id: term.id}); from = start + text.length; } })); matches.sort((left, right) => left.start - right.start || right.end - left.end); let cursor = 0, output = ""; matches.forEach(match => { if (match.start < cursor) return; output += escaped(raw.slice(cursor, match.start)); output += `<mark class="single-script-highlight single-formula-highlight" data-formula-id="${escaped(match.id)}" style="background:${formulaColor(formula, match.id)}33;color:${formulaColor(formula, match.id)}" title="点击联动公式标签">${escaped(match.text)}</mark>`; cursor = match.end; }); return output + escaped(raw.slice(cursor)); }
  function renderSingleFormula(material) {
    const content = document.getElementById("single-material-formula"), formula = material?.formula;
    if (!formula || formula.status !== "available") { content.innerHTML = `<div class="empty">内容公式不可用：${singleText(formula?.reason)}</div>`; return; }
    const labels = formula.labels || [], sources = formula.sources || {};
    const formulaMarkup = labels.map((group, index) => `<div class="single-formula-flow-group" data-single-formula-group="${escaped(group.id)}"><button type="button" class="single-formula-node single-formula-primary" data-formula-id="${escaped(group.id)}" aria-pressed="false" style="--single-formula-color:${formulaColors[index % formulaColors.length]}"><span class="single-formula-level">一级内容公式</span><strong>${escaped(group.label)}</strong></button><div class="single-formula-secondary-list" aria-label="${escaped(group.label)}二级内容公式">${(group.secondary || []).length ? group.secondary.map(child => `<button type="button" class="single-formula-node single-formula-secondary" data-formula-id="${escaped(child.id)}" aria-pressed="false" style="--single-formula-color:${formulaColors[index % formulaColors.length]}"><span class="single-formula-level">二级</span><strong>${escaped(child.label)}</strong></button>`).join("") : '<span class="single-formula-empty">暂无二级内容公式</span>'}</div></div>${index < labels.length - 1 ? '<span class="single-formula-flow-arrow" aria-hidden="true">→</span>' : ""}`).join("");
    const legendMarkup = labels.map((item, index) => `<span class="single-formula-legend-item"><i class="single-formula-legend-dot" style="background:${formulaColors[index % formulaColors.length]}"></i>${escaped(item.label)}</span>`).join("");
    const sourceCards = [["script","本视频脚本文案"],["title","视频标题"],["visual","视频画面"]].map(([key, label]) => {
      const source = sources[key];
      if (!source?.text) return "";
      return `<article class="single-formula-source"><div class="single-formula-source-header"><h3>${label}</h3><button type="button" class="single-formula-copy" data-copy-text="${escaped(source.text)}">复制文本</button></div><p>${formulaSourceText(source, formula)}</p></article>`;
    }).join("");
    content.innerHTML = `<div class="single-formula-workspace"><div class="single-formula-toolbar"><h3>本视频内容公式</h3><button type="button" class="single-formula-copy" data-copy-formula>复制内容公式</button></div><div class="single-formula-legend">${legendMarkup || '<span class="empty">暂无公式标签</span>'}</div><div class="single-formula-flow" aria-label="内容公式层级链路">${formulaMarkup || singleEmpty("内容公式")}</div><div class="single-formula-sources">${sourceCards || singleEmpty("文本")}</div></div>`;
    if (singleState.formulaId) setFormulaSelection(singleState.formulaId);
  }
  function openSingleHighlight(frame, trigger) { singleState.lastFocused = trigger || document.activeElement; singleHighlightImage.src = frame.assetPath; singleHighlightImage.alt = `${frame.second}s ${frame.role}`; singleHighlightCaption.textContent = `${frame.second}s · ${frame.role}`; singleHighlightModal.hidden = false; document.body.classList.add("modal-open"); document.getElementById("single-highlight-modal-close").focus(); }
  function closeSingleHighlight() { singleHighlightModal.hidden = true; singleHighlightImage.removeAttribute("src"); document.body.classList.remove("modal-open"); if (singleState.lastFocused?.focus) singleState.lastFocused.focus(); singleState.lastFocused = null; }
  function renderSingleHighlights(material) { const content = document.getElementById("single-material-highlights"), frames = material?.highlights?.frames || []; if (!frames.length) { content.innerHTML = `<div class="empty">高光帧数据不可用：${singleText(material?.highlights?.reason)}</div>`; return; } const pageCount = Math.max(1, Math.ceil(frames.length / singleHighlightPageSize)); singleState.page = Math.min(singleState.page, pageCount); const start = (singleState.page - 1) * singleHighlightPageSize, pageFrames = frames.slice(start, start + singleHighlightPageSize); content.innerHTML = `<div class="single-highlight-grid">${pageFrames.map(frame => `<article class="single-highlight-card"><button type="button" class="single-highlight-open" data-single-highlight-id="${escaped(frame.id)}" aria-label="查看${escaped(frame.second)}秒高光帧"><img src="${escaped(frame.assetPath)}" alt="${escaped(frame.second)}秒 ${escaped(frame.role)}"></button><p>${escaped(frame.second)}s · ${escaped(frame.role)}<br>${(frame.reasons || []).map(reason => escaped(reason.type === "cover" ? "封面画面" : `${reason.trendName}Top${reason.rank}`)).join(" · ")}</p></article>`).join("")}</div><div class="pager"><span>显示 ${start + 1}-${Math.min(start + singleHighlightPageSize, frames.length)} / ${frames.length}</span><span><button type="button" data-single-highlight-page="first" ${singleState.page <= 1 ? "disabled" : ""}>首页</button><button type="button" data-single-highlight-page="previous" ${singleState.page <= 1 ? "disabled" : ""}>上一页</button><span>${singleState.page} / ${pageCount}</span><button type="button" data-single-highlight-page="next" ${singleState.page >= pageCount ? "disabled" : ""}>下一页</button><button type="button" data-single-highlight-page="last" ${singleState.page >= pageCount ? "disabled" : ""}>尾页</button></span></div>`; }
  function setSingleMaterialTab(tabId) { singleState.tab = tabId; document.querySelectorAll("[data-material-tab]").forEach(tab => { const active = tab.dataset.materialTab === tabId; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); }); }
  function renderSingleMaterial() { const material = currentSingleMaterial(); renderSingleSelector(); const title = document.getElementById("single-material-title"), id = document.getElementById("single-material-id"), video = document.getElementById("single-material-video"); if (!material) { title.textContent = "单素材详情不可用"; id.textContent = ""; video.textContent = "无已验证详情素材"; ["core","portrait","trends","formula","highlights"].forEach(tab => { document.getElementById(`single-material-${tab}`).innerHTML = '<div class="empty">暂无可验证数据</div>'; }); ensureSinglePanelHeadings(); return; } title.textContent = material.title || "单素材详情"; id.textContent = `素材ID：${material.materialId} · 榜单排名：${material.rank} · 首投日期：${material.publishTime || "—"}`; video.innerHTML = material.video?.available ? `<video controls preload="metadata" src="${escaped(material.video.assetPath)}">本地视频无法播放</video>` : `视频不可用：${escaped(material.video?.reason || "local_video_artifact_missing")}`; renderSingleCore(material); renderSinglePortrait(material); renderSingleTrends(material); renderSingleFormula(material); renderSingleHighlights(material); ensureSinglePanelHeadings(); setSingleMaterialTab(singleState.tab); }
  function hideChartTooltip() { chartTooltip.hidden = true; }
  function showChartTooltip(event, bucket, level, total) { const source = event.currentTarget || event.target; const rect = source && source.getBoundingClientRect ? source.getBoundingClientRect() : null; chartTooltip.innerHTML = `<div>${escaped(level)}：${escaped(bucket.label)}</div><div>覆盖素材数：${escaped(bucket.value)}</div><div>占比：${escaped(new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share))}</div><div>当前维度标签覆盖总和：${escaped(total)}</div>`; chartTooltip.hidden = false; chartTooltip.style.left = `${Math.min(Math.max(12, rect ? rect.right + 8 : 16), Math.max(12, window.innerWidth - chartTooltip.offsetWidth - 12))}px`; chartTooltip.style.top = `${Math.min(Math.max(12, rect ? rect.top : 16), Math.max(12, window.innerHeight - chartTooltip.offsetHeight - 12))}px`; }
  function attachChartInteractions(container, buckets, level, total, dimension) { container.querySelectorAll("[data-chart-index]").forEach(element => { const bucket = buckets[Number(element.dataset.chartIndex)]; element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("blur", hideChartTooltip); if (dimension) element.addEventListener("click", event => toggleDonutSelection(dimension, event.currentTarget.dataset.chartIndex)); }); }
  function setDonutSelection(dimension, index) { audienceState.donutSelection[dimension] = index == null ? null : Number(index); document.querySelectorAll(`[data-donut-dimension="${dimension}"]`).forEach(element => { const selected = Number(element.dataset.chartIndex) === audienceState.donutSelection[dimension]; element.classList.toggle("active", selected); element.setAttribute("aria-pressed", String(selected)); if (element.classList.contains("donut-segment")) { const angle = Number(element.dataset.donutAngle || 0); const distance = selected ? 8 : 0; element.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px,${(Math.sin(angle) * distance).toFixed(2)}px)`; } }); }
  function toggleDonutSelection(dimension, index) { setDonutSelection(dimension, audienceState.donutSelection[dimension] === Number(index) ? null : Number(index)); }
  function chartEmpty(label) { return `<div class="empty">暂无可验证的${escaped(label)}数据</div>`; }
  function renderBars(containerId, buckets, label, total) { const container = document.getElementById(containerId); if (!buckets.length) { container.innerHTML = chartEmpty(label); return; } const width = Math.max(760, (container.clientWidth || 900) - 8), height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20, max = Math.max(...buckets.map(item => Number(item.value)), 1), band = (width - left - 18) / buckets.length; const bars = buckets.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight), barWidth = band * .52, x = left + index * band + (band - barWidth) / 2, y = height - bottom - barHeight, center = x + barWidth / 2, shortLabel = bucket.label.length > 8 ? `${bucket.label.slice(0,8)}…` : bucket.label; return `<rect class="chart-bar" data-chart-index="${index}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="img" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share))}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14,y-6).toFixed(2)}" text-anchor="middle">${escaped(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height-42}" text-anchor="middle">${escaped(shortLabel)}</text>`; }).join(""); container.innerHTML = `<svg style="width:100%;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="${escaped(label)}柱状图"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-12}" y2="${height-bottom}"></line>${bars}</svg>`; attachChartInteractions(container, buckets, label, total, null); }
  function donutPath(start, end, outer, inner, cx, cy) { const point = (radius, angle) => `${(cx + radius * Math.cos(angle)).toFixed(2)} ${(cy + radius * Math.sin(angle)).toFixed(2)}`; const sweep = end - start, full = Math.PI * 2; if (sweep >= full - .0001) { const midpoint = start + Math.PI; return `M ${point(outer,start)} A ${outer} ${outer} 0 1 1 ${point(outer,midpoint)} A ${outer} ${outer} 0 1 1 ${point(outer,start)} L ${point(inner,start)} A ${inner} ${inner} 0 1 0 ${point(inner,midpoint)} A ${inner} ${inner} 0 1 0 ${point(inner,start)} Z`; } const large = sweep > Math.PI ? 1 : 0; return `M ${point(outer,start)} A ${outer} ${outer} 0 ${large} 1 ${point(outer,end)} L ${point(inner,end)} A ${inner} ${inner} 0 ${large} 0 ${point(inner,start)} Z`; }
  function renderDonut(containerId, legendId, buckets, label, dimension) { const container = document.getElementById(containerId), legend = document.getElementById(legendId); if (!buckets.length) { container.innerHTML = chartEmpty(label); legend.innerHTML = ""; return; } const total = buckets[0].total || buckets.reduce((sum, item) => sum + item.value, 0); let cursor = -Math.PI / 2; const segments = buckets.map((bucket, index) => { const end = cursor + bucket.value / total * Math.PI * 2, angle = (cursor + end) / 2, path = donutPath(cursor, end, 92, 56, 116, 124); cursor = end; return `<path class="donut-segment" data-donut-dimension="${dimension}" data-chart-index="${index}" data-donut-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share))}"></path>`; }).join(""); container.innerHTML = `<svg viewBox="0 0 232 250" role="group" aria-label="${escaped(label)}环形图">${segments}<text class="donut-center-label" x="116" y="118">覆盖素材数</text><text class="donut-center-value" x="116" y="143">${escaped(total)}</text></svg>`; legend.innerHTML = buckets.map((bucket, index) => `<button type="button" class="legend-item" data-donut-dimension="${dimension}" data-chart-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(bucket.value)} 个素材 · ${new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share)}</span></span></button>`).join(""); attachChartInteractions(container, buckets, label, total, dimension); attachChartInteractions(legend, buckets, label, total, dimension); setDonutSelection(dimension, audienceState.donutSelection[dimension]); }
  function renderAudience() { const content = document.getElementById("audience-content"), unavailable = document.getElementById("audience-unavailable"), available = audience.status === "available"; content.hidden = !available; unavailable.hidden = available; if (!available) return; const dimensions = audience.dimensions || {}; const crowd = dimensions.crowd_group || {buckets:[], total:0}, gender = dimensions.gender || {buckets:[], total:0}, age = dimensions.age || {buckets:[], total:0}; renderBars("audience-eight-groups", crowd.buckets, "八大人群", crowd.total); renderDonut("audience-gender", "legend-gender", gender.buckets, "性别", "gender"); renderDonut("audience-age", "legend-age", age.buckets, "年龄", "age"); }
  function formulaShare(bucket) { return new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(bucket.share); }
  function setScriptFormulaSelection(index) { scriptFormulaState.selectedSecondary = index == null ? null : Number(index); document.querySelectorAll("[data-script-formula-secondary-index]").forEach(item => { const selected = Number(item.dataset.scriptFormulaSecondaryIndex) === scriptFormulaState.selectedSecondary; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected)); if (item.classList.contains("script-formula-segment")) { const angle = Number(item.dataset.scriptFormulaAngle || 0); const distance = selected ? 8 : 0; item.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px,${(Math.sin(angle) * distance).toFixed(2)}px)`; } }); }
  function attachScriptFormulaInteractions(container, buckets, level, total) { container.querySelectorAll("[data-script-formula-index],[data-script-formula-secondary-index]").forEach(element => { const index = Number(element.dataset.scriptFormulaSecondaryIndex ?? element.dataset.scriptFormulaIndex), bucket = buckets[index]; element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("blur", hideChartTooltip); if (level === "二级内容公式") element.addEventListener("click", () => setScriptFormulaSelection(scriptFormulaState.selectedSecondary === index ? null : index)); }); }
  function renderScriptFormula() { const chart = document.getElementById("script-formula-chart"), secondaryCard = document.getElementById("script-formula-secondary-card"), secondaryChart = document.getElementById("script-formula-secondary-chart"), secondaryLegend = document.getElementById("script-formula-secondary-legend"), secondaryTitle = document.getElementById("script-formula-secondary-title"), title = document.getElementById("script-formula-title"), hint = document.getElementById("script-formula-hint"); hideChartTooltip(); if (scriptFormula.status !== "available") { title.textContent = "一级内容公式"; hint.hidden = true; chart.innerHTML = '<div class="empty">脚本内容公式不可用</div>'; secondaryCard.hidden = true; return; } const primaryBuckets = Array.isArray(scriptFormula.primaryBuckets) ? scriptFormula.primaryBuckets : []; hint.hidden = false; if (!primaryBuckets.length) { chart.innerHTML = '<div class="empty">暂无可展示的一级内容公式</div>'; secondaryCard.hidden = true; return; } const width = Math.max(560, Math.min(760, primaryBuckets.length * 170, (chart.clientWidth || 900) - 8)), height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20, max = Math.max(...primaryBuckets.map(bucket => Number(bucket.value)), 1), band = (width - left - 18) / primaryBuckets.length; const bars = primaryBuckets.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight), barWidth = band * .52, x = left + index * band + (band - barWidth) / 2, y = height - bottom - barHeight, center = x + barWidth / 2, shortLabel = bucket.label.length > 8 ? `${bucket.label.slice(0,8)}…` : bucket.label; return `<rect class="script-formula-bar" data-script-formula-index="${index}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="button" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}" data-script-primary="${escaped(bucket.label)}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14,y-6).toFixed(2)}" text-anchor="middle">${escaped(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height-42}" text-anchor="middle">${escaped(shortLabel)}</text>`; }).join(""); chart.innerHTML = `<svg class="script-formula-svg" style="width:${width}px;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="一级内容公式柱状图"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-12}" y2="${height-bottom}"></line>${bars}</svg>`; attachScriptFormulaInteractions(chart, primaryBuckets, "一级内容公式", scriptFormula.coverage.formulaMaterialCount); chart.querySelectorAll("[data-script-primary]").forEach(element => { const select = () => { scriptFormulaState.selectedPrimary = element.dataset.scriptPrimary; renderScriptFormula(); }; element.addEventListener("click", select); element.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } }); }); if (scriptFormulaState.selectedPrimary == null) { secondaryCard.hidden = true; return; } const primary = scriptFormulaState.selectedPrimary, primaryBucket = primaryBuckets.find(item => item.label === primary), secondaryBuckets = (scriptFormula.secondaryBucketsByPrimary?.[primary] || []).map(item => ({...item, total: primaryBucket ? primaryBucket.value : 0})); secondaryCard.hidden = false; secondaryTitle.textContent = `${primary} · 二级内容公式分布`; if (!secondaryBuckets.length) { secondaryChart.innerHTML = '<div class="empty">暂无该一级内容公式的二级公式数据</div>'; secondaryLegend.innerHTML = ""; return; } let cursor = -Math.PI / 2; const secondaryValueTotal = secondaryBuckets.reduce((sum, bucket) => sum + Math.max(0, Number(bucket.value) || 0), 0), segments = secondaryBuckets.map((bucket, index) => { const end = cursor + bucket.value / secondaryValueTotal * Math.PI * 2, angle = (cursor + end) / 2, path = donutPath(cursor, end, 96, 58, 116, 130); cursor = end; return `<path class="donut-segment script-formula-segment" data-script-formula-secondary-index="${index}" data-script-formula-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}"></path>`; }).join(""); secondaryChart.innerHTML = `<svg viewBox="0 0 232 270" role="group" aria-label="${escaped(primary)}二级内容公式环形图">${segments}<text class="donut-center-label" x="116" y="124">当前一级覆盖素材数</text><text class="donut-center-value" x="116" y="150">${escaped(primaryBucket ? primaryBucket.value : 0)}</text></svg>`; secondaryLegend.innerHTML = secondaryBuckets.map((bucket, index) => `<button type="button" class="legend-item" data-script-formula-secondary-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(bucket.value)} 个素材 · ${escaped(formulaShare(bucket))}</span></span></button>`).join(""); attachScriptFormulaInteractions(secondaryChart, secondaryBuckets, "二级内容公式", primaryBucket ? primaryBucket.value : 0); attachScriptFormulaInteractions(secondaryLegend, secondaryBuckets, "二级内容公式", primaryBucket ? primaryBucket.value : 0); }
  function setMarketingSellingPointSelection(index) { marketingSellingPointState.selectedSecondary = index == null ? null : Number(index); document.querySelectorAll("[data-marketing-selling-point-secondary-index]").forEach(item => { const selected = Number(item.dataset.marketingSellingPointSecondaryIndex) === marketingSellingPointState.selectedSecondary; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected)); if (item.classList.contains("marketing-selling-point-segment")) { const angle = Number(item.dataset.marketingSellingPointAngle || 0); const distance = selected ? 8 : 0; item.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px,${(Math.sin(angle) * distance).toFixed(2)}px)`; } }); }
  function attachMarketingSellingPointInteractions(container, buckets, level, total) { container.querySelectorAll("[data-marketing-selling-point-index],[data-marketing-selling-point-secondary-index]").forEach(element => { const index = Number(element.dataset.marketingSellingPointSecondaryIndex ?? element.dataset.marketingSellingPointIndex), bucket = buckets[index]; element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, level, total)); element.addEventListener("blur", hideChartTooltip); if (level === "二级营销卖点") element.addEventListener("click", () => setMarketingSellingPointSelection(marketingSellingPointState.selectedSecondary === index ? null : index)); }); }
  function renderMarketingSellingPoints() { const chart = document.getElementById("marketing-selling-point-chart"), secondaryCard = document.getElementById("marketing-selling-point-secondary-card"), secondaryChart = document.getElementById("marketing-selling-point-secondary-chart"), secondaryLegend = document.getElementById("marketing-selling-point-secondary-legend"), secondaryTitle = document.getElementById("marketing-selling-point-secondary-title"), title = document.getElementById("marketing-selling-point-title"), hint = document.getElementById("marketing-selling-point-hint"); hideChartTooltip(); if (marketingSellingPointOverview.status !== "available") { title.textContent = "一级营销卖点"; hint.hidden = true; chart.innerHTML = '<div class="empty">营销卖点不可用</div>'; secondaryCard.hidden = true; return; } const primaryBuckets = Array.isArray(marketingSellingPointOverview.primaryBuckets) ? marketingSellingPointOverview.primaryBuckets : []; hint.hidden = false; if (!primaryBuckets.length) { chart.innerHTML = '<div class="empty">暂无可展示的一级营销卖点</div>'; secondaryCard.hidden = true; return; } const total = marketingSellingPointOverview.coverage?.marketingSellingPointMaterialCount || 0, width = Math.max(560, Math.min(760, primaryBuckets.length * 170, (chart.clientWidth || 900) - 8)), height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20, max = Math.max(...primaryBuckets.map(bucket => Number(bucket.value)), 1), band = (width - left - 18) / primaryBuckets.length; const bars = primaryBuckets.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight), barWidth = band * .52, x = left + index * band + (band - barWidth) / 2, y = height - bottom - barHeight, center = x + barWidth / 2, shortLabel = bucket.label.length > 8 ? `${bucket.label.slice(0,8)}…` : bucket.label; return `<rect class="marketing-selling-point-bar" data-marketing-selling-point-index="${index}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="button" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}" data-marketing-selling-point-primary="${escaped(bucket.label)}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14,y-6).toFixed(2)}" text-anchor="middle">${escaped(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height-42}" text-anchor="middle">${escaped(shortLabel)}</text>`; }).join(""); chart.innerHTML = `<svg class="script-formula-svg" style="width:${width}px;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="一级营销卖点柱状图"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-12}" y2="${height-bottom}"></line>${bars}</svg>`; attachMarketingSellingPointInteractions(chart, primaryBuckets, "一级营销卖点", total); chart.querySelectorAll("[data-marketing-selling-point-primary]").forEach(element => { const select = () => { marketingSellingPointState.selectedPrimary = element.dataset.marketingSellingPointPrimary; renderMarketingSellingPoints(); }; element.addEventListener("click", select); element.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } }); }); if (marketingSellingPointState.selectedPrimary == null) { secondaryCard.hidden = true; return; } const primary = marketingSellingPointState.selectedPrimary, primaryBucket = primaryBuckets.find(item => item.label === primary), secondaryBuckets = (marketingSellingPointOverview.secondaryBucketsByPrimary?.[primary] || []).map(item => ({...item, total: primaryBucket ? primaryBucket.value : 0})); secondaryCard.hidden = false; secondaryTitle.textContent = `${primary} · 二级营销卖点分布`; if (!secondaryBuckets.length) { secondaryChart.innerHTML = '<div class="empty">暂无该一级营销卖点的二级卖点数据</div>'; secondaryLegend.innerHTML = ""; return; } let cursor = -Math.PI / 2; const secondaryValueTotal = secondaryBuckets.reduce((sum, bucket) => sum + Math.max(0, Number(bucket.value) || 0), 0), segments = secondaryBuckets.map((bucket, index) => { const end = cursor + bucket.value / secondaryValueTotal * Math.PI * 2, angle = (cursor + end) / 2, path = donutPath(cursor, end, 96, 58, 116, 130); cursor = end; return `<path class="donut-segment marketing-selling-point-segment" data-marketing-selling-point-secondary-index="${index}" data-marketing-selling-point-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}"></path>`; }).join(""); secondaryChart.innerHTML = `<svg viewBox="0 0 232 270" role="group" aria-label="${escaped(primary)}二级营销卖点环形图">${segments}<text class="donut-center-label" x="116" y="124">当前一级覆盖素材数</text><text class="donut-center-value" x="116" y="150">${escaped(primaryBucket ? primaryBucket.value : 0)}</text></svg>`; secondaryLegend.innerHTML = secondaryBuckets.map((bucket, index) => `<button type="button" class="legend-item" data-marketing-selling-point-secondary-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(bucket.value)} 个素材 · ${escaped(formulaShare(bucket))}</span></span></button>`).join(""); attachMarketingSellingPointInteractions(secondaryChart, secondaryBuckets, "二级营销卖点", primaryBucket ? primaryBucket.value : 0); attachMarketingSellingPointInteractions(secondaryLegend, secondaryBuckets, "二级营销卖点", primaryBucket ? primaryBucket.value : 0); }
  // Keep the marketing-selling-point renderer density-controlled without changing the source facts.
  function renderMarketingSellingPointsDense() {
    const chart = document.getElementById("marketing-selling-point-chart"), secondaryCard = document.getElementById("marketing-selling-point-secondary-card"), secondaryChart = document.getElementById("marketing-selling-point-secondary-chart"), secondaryLegend = document.getElementById("marketing-selling-point-secondary-legend"), secondaryTitle = document.getElementById("marketing-selling-point-secondary-title"), title = document.getElementById("marketing-selling-point-title"), hint = document.getElementById("marketing-selling-point-hint");
    hideChartTooltip();
    if (marketingSellingPointOverview.status !== "available") { title.textContent = "一级营销卖点"; hint.hidden = true; chart.innerHTML = '<div class="empty">营销卖点不可用</div>'; secondaryCard.hidden = true; return; }
    const primaryBuckets = Array.isArray(marketingSellingPointOverview.primaryBuckets) ? marketingSellingPointOverview.primaryBuckets : [];
    hint.hidden = false;
    if (!primaryBuckets.length) { chart.innerHTML = '<div class="empty">暂无可展示的一级营销卖点</div>'; secondaryCard.hidden = true; return; }
    const total = marketingSellingPointOverview.coverage?.marketingSellingPointMaterialCount || 0, height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20, max = Math.max(...primaryBuckets.map(bucket => Number(bucket.value)), 1), layoutWidth = Math.max(680, primaryBuckets.length * 145), baseBand = (layoutWidth - left - 18) / primaryBuckets.length, barWidth = baseBand * .42, barGap = baseBand * .45, step = barWidth + barGap, width = Math.max(680, left + barWidth + step * (primaryBuckets.length - 1) + 18);
    const bars = primaryBuckets.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight), x = left + index * step, y = height - bottom - barHeight, center = x + barWidth / 2, shortLabel = bucket.label.length > 8 ? `${bucket.label.slice(0,8)}…` : bucket.label; return `<rect class="marketing-selling-point-bar" data-marketing-selling-point-index="${index}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="button" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}" data-marketing-selling-point-primary="${escaped(bucket.label)}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14,y-6).toFixed(2)}" text-anchor="middle">${escaped(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height-42}" text-anchor="middle">${escaped(shortLabel)}</text>`; }).join("");
    chart.innerHTML = `<svg class="script-formula-svg marketing-selling-point-svg" style="width:${width}px;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="一级营销卖点柱状图"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-12}" y2="${height-bottom}"></line>${bars}</svg>`;
    attachMarketingSellingPointInteractions(chart, primaryBuckets, "一级营销卖点", total);
    chart.querySelectorAll("[data-marketing-selling-point-primary]").forEach(element => { const select = () => { marketingSellingPointState.selectedPrimary = element.dataset.marketingSellingPointPrimary; renderMarketingSellingPointsDense(); }; element.addEventListener("click", select); element.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } }); });
    if (marketingSellingPointState.selectedPrimary == null) { secondaryCard.hidden = true; return; }
    const primary = marketingSellingPointState.selectedPrimary, primaryBucket = primaryBuckets.find(item => item.label === primary), sourceSecondaryBuckets = (marketingSellingPointOverview.secondaryBucketsByPrimary?.[primary] || []).map(item => ({...item, value: Math.max(0, Number(item.value) || 0)}));
    secondaryCard.hidden = false; secondaryTitle.textContent = `${primary} · 二级营销卖点分布`;
    if (!sourceSecondaryBuckets.length) { secondaryChart.innerHTML = '<div class="empty">暂无该一级营销卖点的二级卖点数据</div>'; secondaryLegend.innerHTML = ""; return; }
    const secondaryValueTotal = sourceSecondaryBuckets.reduce((sum, bucket) => sum + bucket.value, 0), secondaryBuckets = sourceSecondaryBuckets.map(bucket => ({...bucket, share: secondaryValueTotal ? bucket.value / secondaryValueTotal : 0, total: secondaryValueTotal})), topSecondaryBuckets = secondaryBuckets.slice(0, 10), tailValue = secondaryBuckets.slice(10).reduce((sum, bucket) => sum + bucket.value, 0), pieBuckets = tailValue > 0 ? [...topSecondaryBuckets, {label: "长尾", value: tailValue, share: secondaryValueTotal ? tailValue / secondaryValueTotal : 0, total: secondaryValueTotal}] : topSecondaryBuckets;
    const setSecondarySelection = key => { marketingSellingPointState.selectedSecondary = key == null ? null : String(key); document.querySelectorAll("[data-marketing-selling-point-secondary-key]").forEach(item => { const selected = String(item.dataset.marketingSellingPointSecondaryKey) === marketingSellingPointState.selectedSecondary; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected)); if (item.classList.contains("marketing-selling-point-segment")) { const angle = Number(item.dataset.marketingSellingPointAngle || 0), distance = selected ? 8 : 0; item.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px,${(Math.sin(angle) * distance).toFixed(2)}px)`; } }); };
    const attachSecondaryInteractions = (container, buckets) => { const bucketByLabel = Object.fromEntries(buckets.map(bucket => [String(bucket.label), bucket])); container.querySelectorAll("[data-marketing-selling-point-secondary-key]").forEach(element => { const key = String(element.dataset.marketingSellingPointSecondaryKey), bucket = bucketByLabel[key]; if (!bucket) return; element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, "二级营销卖点", secondaryValueTotal)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, "二级营销卖点", secondaryValueTotal)); element.addEventListener("blur", hideChartTooltip); element.addEventListener("click", () => setSecondarySelection(marketingSellingPointState.selectedSecondary === key ? null : key)); }); };
    let cursor = -Math.PI / 2;
    const segments = pieBuckets.map((bucket, index) => { const end = cursor + (secondaryValueTotal ? bucket.value / secondaryValueTotal : 0) * Math.PI * 2, angle = (cursor + end) / 2, path = donutPath(cursor, end, 96, 58, 116, 130); cursor = end; return `<path class="donut-segment marketing-selling-point-segment" data-marketing-selling-point-secondary-key="${escaped(bucket.label)}" data-marketing-selling-point-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}"></path>`; }).join("");
    secondaryChart.innerHTML = `<svg viewBox="0 0 232 270" role="group" aria-label="${escaped(primary)}二级营销卖点环形图（Top10及长尾）">${segments}<text class="donut-center-label" x="116" y="124">当前一级覆盖素材数</text><text class="donut-center-value" x="116" y="150">${escaped(primaryBucket ? primaryBucket.value : 0)}</text></svg>`;
    secondaryLegend.innerHTML = secondaryBuckets.map((bucket, index) => `<button type="button" class="legend-item" data-marketing-selling-point-secondary-key="${escaped(bucket.label)}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖${escaped(bucket.value)}个素材，占比${escaped(formulaShare(bucket))}" title="${escaped(bucket.label)}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(bucket.value)} 个素材 · ${escaped(formulaShare(bucket))}</span></span></button>`).join("");
    attachSecondaryInteractions(secondaryChart, pieBuckets); attachSecondaryInteractions(secondaryLegend, secondaryBuckets); setSecondarySelection(marketingSellingPointState.selectedSecondary);
  }
  function renderMarketingSellingPoints() { renderMarketingSellingPointsDense(); }
  function highlightConditionLabel(reason) { if (reason.type === "cover") return "封面画面"; const kind = reason.type === "top_values" ? "数值" : "波峰"; return `${reason.trend_name}${kind}Top${reason.rank}`; }
  function highlightValueLabel(second, reason) { if (reason.type === "cover") return ""; const value = new Intl.NumberFormat("zh-CN", {maximumFractionDigits:4}).format(Number(reason.value)); return `${second}s · ${reason.trend_name} ${value}`; }
  function highlightConditionReasons(card) { const reasons = Array.isArray(card.reasons) ? card.reasons : [], metricReasons = reasons.filter(reason => reason.type !== "cover"); return (metricReasons.length ? metricReasons : reasons).map(highlightConditionLabel).filter(Boolean); }
  function highlightValueReasons(card) { return (Array.isArray(card.reasons) ? card.reasons : []).map(reason => highlightValueLabel(card.second, reason)).filter(Boolean); }
  function highlightDescription(card) { return [...highlightConditionReasons(card), ...highlightValueReasons(card)].join(" · "); }
  function renderHighlights() { const content = document.getElementById("highlight-content"), unavailable = document.getElementById("highlight-unavailable"), cards = Array.isArray(highlightOverview.cards) ? highlightOverview.cards : [], available = highlightOverview.status === "available" && cards.length > 0; content.hidden = !available; unavailable.hidden = available; if (!available) return; const pageCount = Math.max(1, Math.ceil(cards.length / highlightPageSize)); highlightState.page = Math.min(highlightState.page, pageCount); const start = (highlightState.page - 1) * highlightPageSize, pageCards = cards.slice(start, start + highlightPageSize); highlightGrid.innerHTML = pageCards.map(card => { const conditions = highlightConditionReasons(card), values = highlightValueReasons(card), description = highlightDescription(card); return `<article class="highlight-card"><button type="button" class="highlight-thumb" data-highlight-open="${escaped(card.id)}" aria-label="查看${escaped(description)}大图"><img src="${escaped(card.assetPath)}" alt="${escaped(description)}"></button><div class="highlight-conditions" aria-label="命中筛选条件">${conditions.map(condition => `<span class="highlight-condition" title="${escaped(condition)}">${escaped(condition)}</span>`).join("")}</div><div class="highlight-values" aria-label="对应指标数值">${values.map(value => `<span class="highlight-value" title="${escaped(value)}">${escaped(value)}</span>`).join("")}</div><a href="#single-material-data" class="highlight-material" data-highlight-material-id="${escaped(card.materialId)}" title="后续定位到单素材数据：${escaped(card.materialTitle)}">查看素材：${escaped(card.materialTitle)}</a></article>`; }).join(""); document.getElementById("highlight-range").textContent = `显示 ${start + 1}-${Math.min(start + highlightPageSize, cards.length)} / ${cards.length}`; document.getElementById("highlight-pages").textContent = `${highlightState.page} / ${pageCount}`; ["highlight-first", "highlight-previous"].forEach(id => document.getElementById(id).disabled = highlightState.page <= 1); ["highlight-next", "highlight-last"].forEach(id => document.getElementById(id).disabled = highlightState.page >= pageCount); }
  function openHighlightModal(cardId, trigger) { const card = (highlightOverview.cards || []).find(item => item.id === cardId); if (!card) return; const description = highlightDescription(card); highlightState.lastFocused = trigger || document.activeElement; highlightModalImage.src = card.assetPath; highlightModalImage.alt = description; highlightModalCaption.textContent = `${card.materialTitle} · ${description}`; highlightModal.hidden = false; document.body.classList.add("modal-open"); document.getElementById("highlight-modal-close").focus(); }
  function closeHighlightModal() { highlightModal.hidden = true; highlightModalImage.removeAttribute("src"); document.body.classList.remove("modal-open"); if (highlightState.lastFocused && typeof highlightState.lastFocused.focus === "function") highlightState.lastFocused.focus(); highlightState.lastFocused = null; }
  function jumpToMaterial(materialId) { state.pendingMaterialId = materialId; window.dispatchEvent(new CustomEvent("yuntu:highlight-material", {detail: {materialId}})); }
  renderAudience(); renderScriptFormula(); renderMarketingSellingPointsDense(); renderHighlights();
  function highlighted(value) { const text = escaped(value); const query = normalize(state.query); if (!query) return text; const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu"); return text.replace(pattern, match => `<mark>${match}</mark>`); }
  function display(value, field) { if (missing(value)) return "—"; if (field.id === "video_duration") return `${escaped(value)} 秒`; if (field.valueType === "rate") return `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(Number(value) * 100)}%`; if (field.valueType === "number") return new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(Number(value)); if (field.valueType === "list") return escaped(value.join("、")); if (field.valueType === "date") { const raw = String(value); return escaped(raw.length === 8 ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6)}` : raw); } return escaped(value); }
  function compareValues(left, right, field) { const leftMissing = missing(left), rightMissing = missing(right); if (leftMissing || rightMissing) return leftMissing === rightMissing ? 0 : (leftMissing ? 1 : -1); if (field.valueType === "number" || field.valueType === "rate") return Number(left) - Number(right); if (field.valueType === "date") return String(left).localeCompare(String(right)); return normalize(left).localeCompare(normalize(right)); }
  function compareRows(left, right) { const field = fieldMap[state.sort.field]; if (missing(left.values[field.id]) || missing(right.values[field.id])) return compareValues(left.values[field.id], right.values[field.id], field); const result = compareValues(left.values[field.id], right.values[field.id], field); if (result) return state.sort.direction === "desc" ? -result : result; const rank = compareValues(left.values.rank, right.values.rank, {valueType: "number"}); if (rank) return rank; return normalize(left.materialId).localeCompare(normalize(right.materialId)); }
  function matchingRows() { const query = normalize(state.query); const rows = state.exactMaterialId ? model.rows.filter(row => row.materialId === state.exactMaterialId) : model.rows.filter(row => !query || normalize(row.materialId).includes(query) || normalize(row.values.title).includes(query)); if (state.sort.field && state.sort.direction !== "none" && fieldMap[state.sort.field] && fieldMap[state.sort.field].sortable) rows.sort(compareRows); return rows; }
  function renderFieldDialog() { state.draftFieldIds = orderedVisibleFieldIds(state.draftFieldIds || state.visibleFieldIds); fieldGroups.innerHTML = groupNames.map(group => `<button type="button" class="${!state.fieldSearch && state.activeFieldGroup === group ? "active" : ""}" data-field-group="${escaped(group)}">${escaped(group)}<span>${orderedFields.filter(field => field.group === group).length}</span></button>`).join(""); const query = normalize(state.fieldSearch); const available = query ? orderedFields.filter(field => normalize(field.label).includes(query) || normalize(field.id).includes(query)) : orderedFields.filter(field => field.group === state.activeFieldGroup); const grouped = {}; available.forEach(field => (grouped[field.group] ||= []).push(field)); fieldOptions.innerHTML = Object.entries(grouped).map(([group, fields]) => `<section class="field-option-section"><h3>${escaped(group)}</h3><div class="field-option-grid">${fields.map(field => `<label class="field-option"><input type="checkbox" data-field-id="${escaped(field.id)}"${state.draftFieldIds.includes(field.id) ? " checked" : ""}${requiredFieldIds.includes(field.id) ? " disabled" : ""}><span>${escaped(field.label)}</span></label>`).join("")}</div></section>`).join("") || '<div class="empty">没有匹配字段</div>'; const selected = state.draftFieldIds.map(id => fieldMap[id]).filter(Boolean); selectedCount.textContent = `（${selected.length}）`; selectedFields.innerHTML = selected.map(field => `<li class="field-modal-selected-item"><span class="field-drag-handle">☷</span><span>${escaped(field.label)}</span>${requiredFieldIds.includes(field.id) ? '<span class="field-required">固定</span>' : `<button type="button" data-remove-field-id="${escaped(field.id)}" aria-label="移除${escaped(field.label)}">×</button>`}</li>`).join(""); }
  function openFieldDialog() { state.draftFieldIds = orderedVisibleFieldIds(state.visibleFieldIds); state.fieldSearch = ""; state.activeFieldGroup = groupNames[0] || ""; fieldSearch.value = ""; modal.hidden = false; document.body.classList.add("modal-open"); renderFieldDialog(); fieldSearch.focus(); }
  function closeFieldDialog() { modal.hidden = true; state.draftFieldIds = null; state.fieldSearch = ""; document.body.classList.remove("modal-open"); }
  function confirmFieldDialog() { state.visibleFieldIds = orderedVisibleFieldIds(state.draftFieldIds); state.page = 1; saveVisibleFields(); closeFieldDialog(); render(); }
  function sortLabel(field) { if (state.sort.field !== field.id || state.sort.direction === "none") return ""; return state.sort.direction === "asc" ? " ↑" : " ↓"; }
  function renderTable(rows) { const visibleFields = orderedVisibleFieldIds(state.visibleFieldIds).map(id => fieldMap[id]).filter(Boolean); if (!rows.length) { tableContainer.innerHTML = '<div class="empty">没有匹配素材</div>'; return; } const pageCount = Math.max(1, Math.ceil(rows.length / pageSize)); state.page = Math.min(state.page, pageCount); const pageRows = rows.slice((state.page - 1) * pageSize, state.page * pageSize); const header = visibleFields.map(field => `<th scope="col" class="${field.id === "title" ? "material-name-column" : ""}">${field.sortable ? `<button type="button" data-sort-id="${escaped(field.id)}">${escaped(field.label)}${sortLabel(field)}</button>` : escaped(field.label)}</th>`).join(""); const body = pageRows.map(row => `<tr>${visibleFields.map(field => field.id === "title" ? `<td class="material-name-column"><div class="material-title">${highlighted(row.values.title)}</div><div class="material-id">素材ID：${highlighted(row.materialId)}</div></td>` : `<td>${display(row.values[field.id], field)}</td>`).join("")}</tr>`).join(""); tableContainer.innerHTML = `<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`; tableContainer.querySelectorAll("button[data-sort-id]").forEach(button => button.addEventListener("click", event => { const field = event.currentTarget.dataset.sortId; const next = state.sort.field !== field ? "asc" : state.sort.direction === "none" ? "asc" : state.sort.direction === "asc" ? "desc" : "none"; state.sort = {field, direction: next}; state.page = 1; render(); })); }
  function renderPager(total) { const pageCount = Math.max(1, Math.ceil(total / pageSize)); state.page = Math.min(state.page, pageCount); const start = total ? (state.page - 1) * pageSize + 1 : 0; const end = Math.min(state.page * pageSize, total); document.getElementById("summary").textContent = `共 ${total} 条`; document.getElementById("range").textContent = total ? `显示 ${start}-${end} / ${total}` : "暂无记录"; document.getElementById("pages").textContent = `${state.page} / ${pageCount}`; ["first", "previous"].forEach(id => document.getElementById(id).disabled = state.page <= 1); ["next", "last"].forEach(id => document.getElementById(id).disabled = state.page >= pageCount); }
  function render() { const rows = matchingRows(); renderTable(rows); renderPager(rows.length); }
  modal.addEventListener("click", event => { const action = event.target.closest("[data-field-action]")?.dataset.fieldAction; if (action === "cancel") closeFieldDialog(); if (action === "confirm") confirmFieldDialog(); if (action === "reset") { state.draftFieldIds = orderedVisibleFieldIds(defaultFieldIds); renderFieldDialog(); } const group = event.target.closest("[data-field-group]")?.dataset.fieldGroup; if (group) { state.activeFieldGroup = group; state.fieldSearch = ""; fieldSearch.value = ""; renderFieldDialog(); } const removeId = event.target.closest("[data-remove-field-id]")?.dataset.removeFieldId; if (removeId && !requiredFieldIds.includes(removeId)) { state.draftFieldIds = orderedVisibleFieldIds(state.draftFieldIds.filter(id => id !== removeId)); renderFieldDialog(); } });
  modal.addEventListener("change", event => { const input = event.target.closest("input[data-field-id]"); if (!input) return; state.draftFieldIds = orderedVisibleFieldIds(input.checked ? [...state.draftFieldIds, input.dataset.fieldId] : state.draftFieldIds.filter(id => id !== input.dataset.fieldId)); renderFieldDialog(); });
  fieldSearch.addEventListener("input", event => { state.fieldSearch = event.target.value; renderFieldDialog(); fieldSearch.focus(); });
  highlightGrid.addEventListener("click", event => { const open = event.target.closest("[data-highlight-open]"); if (open) { openHighlightModal(open.dataset.highlightOpen, open); return; } const material = event.target.closest("[data-highlight-material-id]"); if (material) { event.preventDefault(); jumpToMaterial(material.dataset.highlightMaterialId); } });
  highlightModal.addEventListener("click", event => { if (event.target.closest("[data-highlight-action=close]")) closeHighlightModal(); });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && !singleHighlightModal.hidden) closeSingleHighlight(); else if (event.key === "Escape" && !highlightModal.hidden) closeHighlightModal(); else if (event.key === "Escape" && !singleCoreModal.hidden) closeSingleCoreFieldDialog(); else if (event.key === "Escape" && !modal.hidden) closeFieldDialog(); else if (event.key === "Escape") hideChartTooltip(); });
  document.querySelectorAll("[data-section]").forEach(button => button.addEventListener("click", event => { const section = event.currentTarget.dataset.section; document.querySelectorAll("[data-section]").forEach(item => item.classList.toggle("active", item.dataset.section === section)); document.querySelectorAll("[data-section-panel]").forEach(panel => { panel.hidden = panel.dataset.sectionPanel !== section; }); if (section === "filters") renderFilterConfig(); if (section === "single-material-data") { renderSingleMaterial(); setSingleMaterialTab("core"); } if (section === "audience") renderAudience(); if (section === "scripts") renderScriptFormula(); if (section === "marketing-selling-points") renderMarketingSellingPoints(); if (section === "highlights") renderHighlights(); hideChartTooltip(); }));
  document.querySelectorAll("[data-material-tab]").forEach(button => button.addEventListener("click", event => { const tabId = event.currentTarget.dataset.materialTab; setSingleMaterialTab(tabId); document.getElementById(`single-material-${tabId}`)?.scrollIntoView({behavior:"smooth", block:"start"}); }));
  if (singleSelector) singleSelector.addEventListener("click", event => { const button = event.target.closest("[data-material-selector]"); if (!button) return; singleState.materialId = button.dataset.materialSelector; singleState.tab = "core"; singleState.page = 1; renderSingleMaterial(); document.querySelector(".single-material-main")?.scrollIntoView({behavior:"auto", block:"start"}); });
  document.addEventListener("input", event => { const range = event.target.closest("[data-single-range]"); if (!range || range.dataset.singleRange !== "span") return; singleState.span = Math.max(20, Math.min(100, Number(range.value))); const value = document.getElementById("singleTrendSpanValue"), material = currentSingleMaterial(), metric = (material?.trends?.metrics || []).find(item => item.id === singleState.trend); if (value) value.textContent = `${singleState.span}%`; if (metric) renderSingleTrendChart(metric); });
  document.addEventListener("click", event => { const coreAction = event.target.closest("[data-single-core-field-action]")?.dataset.singleCoreFieldAction; if (event.target.closest("#single-core-field-button")) { openSingleCoreFieldDialog(); return; } if (coreAction === "cancel") { closeSingleCoreFieldDialog(); return; } if (coreAction === "confirm") { confirmSingleCoreFieldDialog(); return; } if (coreAction === "reset") { singleCoreState.draftFieldIds = [...(currentSingleMaterial()?.core?.defaultFieldIds || [])]; singleCoreState.activeFieldGroup = singleCoreFieldGroupOrder[0]; renderSingleCoreFieldDialog(); return; } const group = event.target.closest("[data-single-core-field-group]")?.dataset.singleCoreFieldGroup; if (group) { singleCoreState.activeFieldGroup = group; singleCoreState.fieldSearch = ""; singleCoreFieldSearch.value = ""; renderSingleCoreFieldDialog(); return; } const removeCoreId = event.target.closest("[data-single-core-remove-field-id]")?.dataset.singleCoreRemoveFieldId; if (removeCoreId) { singleCoreState.draftFieldIds = singleCoreState.draftFieldIds.filter(id => id !== removeCoreId); renderSingleCoreFieldDialog(); return; } const trend = event.target.closest("[data-single-trend]"); if (trend) { singleState.trend = trend.dataset.singleTrend; renderSingleMaterial(); return; } const formula = event.target.closest("[data-formula-id]"); if (formula) { setFormulaSelection(formula.dataset.formulaId); return; } const copyFormula = event.target.closest("[data-copy-formula]"); if (copyFormula) { const formula = currentSingleMaterial()?.formula; const text = (formula?.labels || []).flatMap(item => (item.secondary || []).map(child => child.label)).join(" + "); copyText(text || "暂无内容公式", copyFormula); return; } const copySource = event.target.closest("[data-copy-text]"); if (copySource) { copyText(copySource.dataset.copyText || "", copySource); return; } const open = event.target.closest("[data-single-highlight-id]"); if (open) { const material = currentSingleMaterial(), frame = material?.highlights?.frames?.find(item => item.id === open.dataset.singleHighlightId); if (frame) openSingleHighlight(frame, open); return; } const page = event.target.closest("[data-single-highlight-page]"); if (page) { const frames = currentSingleMaterial()?.highlights?.frames || [], pageCount = Math.max(1, Math.ceil(frames.length / singleHighlightPageSize)); if (page.dataset.singleHighlightPage === "first") singleState.page = 1; if (page.dataset.singleHighlightPage === "previous") singleState.page = Math.max(1, singleState.page - 1); if (page.dataset.singleHighlightPage === "next") singleState.page = Math.min(pageCount, singleState.page + 1); if (page.dataset.singleHighlightPage === "last") singleState.page = pageCount; renderSingleMaterial(); } });
  singleCoreModal.addEventListener("change", event => { const input = event.target.closest("input[data-single-core-field-id]"); if (!input) return; singleCoreState.draftFieldIds = orderedSingleCoreFieldIds(input.checked ? [...singleCoreState.draftFieldIds, input.dataset.singleCoreFieldId] : singleCoreState.draftFieldIds.filter(id => id !== input.dataset.singleCoreFieldId), currentSingleMaterial()); renderSingleCoreFieldDialog(); });
  singleCoreFieldSearch.addEventListener("input", event => { singleCoreState.fieldSearch = event.target.value; renderSingleCoreFieldDialog(); singleCoreFieldSearch.focus(); });
  singleHighlightModal.addEventListener("click", event => { if (event.target.closest("[data-single-highlight-action=close]")) closeSingleHighlight(); });
  window.addEventListener("yuntu:highlight-material", event => { if (!singleMaterials.some(item => item.materialId === event.detail?.materialId)) return; singleState.materialId = event.detail.materialId; singleState.tab = "core"; renderSingleMaterial(); document.querySelector('[data-section="single-material-data"]').click(); });
  document.getElementById("script-formula-secondary-back").addEventListener("click", () => { scriptFormulaState.selectedPrimary = null; renderScriptFormula(); });
  document.getElementById("marketing-selling-point-secondary-back").addEventListener("click", () => { marketingSellingPointState.selectedPrimary = null; renderMarketingSellingPoints(); });
  search.addEventListener("input", event => { state.query = event.target.value; state.exactMaterialId = null; state.page = 1; render(); });
  document.getElementById("field-button").addEventListener("click", openFieldDialog);
  document.getElementById("first").addEventListener("click", () => { state.page = 1; render(); }); document.getElementById("previous").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); render(); }); document.getElementById("next").addEventListener("click", () => { state.page += 1; render(); }); document.getElementById("last").addEventListener("click", () => { state.page = Math.max(1, Math.ceil(matchingRows().length / pageSize)); render(); });
  document.getElementById("highlight-first").addEventListener("click", () => { highlightState.page = 1; renderHighlights(); }); document.getElementById("highlight-previous").addEventListener("click", () => { highlightState.page = Math.max(1, highlightState.page - 1); renderHighlights(); }); document.getElementById("highlight-next").addEventListener("click", () => { highlightState.page += 1; renderHighlights(); }); document.getElementById("highlight-last").addEventListener("click", () => { highlightState.page = Math.max(1, Math.ceil((highlightOverview.cards || []).length / highlightPageSize)); renderHighlights(); });
  function matchingRows() { const query = normalize(state.query), selectedIds = brandOverview.status === "available" ? new Set(brandOverview.materialIdsByTab?.[brandState.selected] || []) : null, matchesBrand = row => !selectedIds || selectedIds.has(row.materialId); const rows = state.exactMaterialId ? model.rows.filter(row => row.materialId === state.exactMaterialId && matchesBrand(row)) : model.rows.filter(row => matchesBrand(row) && (!query || normalize(row.materialId).includes(query) || normalize(row.values.title).includes(query))); if (state.sort.field && state.sort.direction !== "none" && fieldMap[state.sort.field] && fieldMap[state.sort.field].sortable) rows.sort(compareRows); return rows; }
  document.addEventListener("click", event => { const tab = event.target.closest("[data-brand-tab-id]"); if (tab) setBrandSelection(tab.dataset.brandTabId); });
  renderBrandTabs(); applyBrandView(); renderFilterConfig(); loadVisibleFields(); renderSingleMaterial(); render();
})();
</script>
</body>
</html>
'''


def _write_atomic(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_output_dir(run_dir: Path, output_dir: Path) -> Path:
    root = run_dir.resolve()
    target = output_dir.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise OverviewReportError("output directory must remain inside the run") from exc
    if not relative.parts:
        raise OverviewReportError("output directory must not be the run directory")
    if relative.parts[0] in {"normalized", "raw-csv", "facts", "analysis", "config"}:
        raise OverviewReportError("output directory is reserved")
    if target.exists() and not target.is_dir():
        raise OverviewReportError("output directory is not a directory")
    return target


def write_overview(run_dir: Path, output_dir: Path, registry_path: Path) -> dict:
    run_root = Path(run_dir).resolve()
    io_cache = CommandFileCache(run_root)
    asset_index = {}
    model = build_overview_data(
        run_dir, registry_path, io_cache=io_cache, asset_index=asset_index
    )
    output_path = _safe_output_dir(run_root, Path(output_dir))
    output_path.mkdir(parents=True, exist_ok=True)
    if model.get("highlightOverview", {}).get("cards"):
        copy_verified_highlight_assets(
            run_root,
            output_path,
            model["highlightOverview"],
            io_cache=io_cache,
            asset_index=asset_index,
        )
    if model.get("singleMaterialOverview", {}).get("materials"):
        copy_verified_single_material_assets(
            run_root,
            output_path,
            model["singleMaterialOverview"],
            io_cache=io_cache,
            asset_index=asset_index,
        )
    data_path = output_path / "overview-data.json"
    html_path = output_path / "index.html"
    _write_atomic(data_path, json.dumps(model, ensure_ascii=False, indent=2) + "\n")
    try:
        _write_atomic(html_path, render_overview_html(model))
    except Exception:
        try:
            data_path.unlink()
        except OSError:
            pass
        raise
    relative_output = output_path.relative_to(run_root).as_posix()
    return {
        "status": "succeeded",
        "outputPaths": [f"{relative_output}/overview-data.json", f"{relative_output}/index.html"],
        "recordCount": model["source"]["recordCount"],
        "sourceSha256": model["source"]["sha256"],
    }
