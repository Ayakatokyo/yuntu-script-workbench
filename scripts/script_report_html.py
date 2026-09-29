"""Offline five-section script report, adapted from the Qianchuan workbench layout.

Only verified fact-package fields are used for material content. JavaScript filters
already rendered content; the document stays readable with JavaScript disabled.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
import json
from pathlib import Path
from typing import Any

try:
    from content_formula_labels import formula_detail_labels, resolve_formula_ref
    from material_field_registry import default_material_field_registry
    from single_material_overview import _project_core, _project_formula
except ImportError:
    from .content_formula_labels import formula_detail_labels, resolve_formula_ref
    from .material_field_registry import default_material_field_registry
    from .single_material_overview import _project_core, _project_formula


def _e(value: Any) -> str:
    if value is None or value == "":
        value = "未提供"
    return escape(str(value), quote=True)


def _metric_columns(rows: list[dict]) -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "config/yuntu-material-field-registry-v1.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    fields = [item for item in registry["fields"]
              if len(item.get("path", [])) == 2 and item["path"][0] == "metrics"]
    labels = {item["path"][1]: item["label"] for item in fields}
    available = {key for row in rows for key in (row.get("metrics") or {})}
    keys = [item["path"][1] for item in fields if item["path"][1] in available]
    keys.extend(sorted(available - set(keys)))
    return [{"key": key, "label": labels.get(key, key)} for key in keys]


_FIELD_GROUPS = {
    "title": "素材基础信息", "publish_time": "素材基础信息",
    "video_duration": "素材基础信息", "rank": "素材基础信息",
    "material_uri": "素材基础信息", "material_tags": "素材基础信息",
    "metrics.show_cnt": "内容曝光与播放", "metrics.play_3s_rate": "内容曝光与播放",
    "metrics.play_5s_rate": "内容曝光与播放", "metrics.play_over_rate": "内容曝光与播放",
    "metrics.play_duration_avg": "内容曝光与播放",
    "metrics.ctr": "点击与转化", "metrics.cvr": "点击与转化",
    "metrics.pvr": "点击与转化", "metrics.product_wish_button_buy_cart_rate": "点击与转化",
    "metrics.interact_rate": "互动指标", "metrics.like_rate": "互动指标",
    "metrics.comment_rate": "互动指标", "metrics.share_rate": "互动指标",
}
_FIELD_GROUP_ORDER = ("素材基础信息", "内容曝光与播放", "点击与转化", "互动指标", "其他指标")
_DEFAULT_FIELD_IDS = ("title", "publish_time", "metrics.show_cnt", "metrics.play_3s_rate",
                      "metrics.play_over_rate", "metrics.play_duration_avg", "metrics.ctr",
                      "metrics.cvr", "metrics.pvr", "metrics.interact_rate")
_ROW_FIELD_KEYS = {"publish_time": "publishTime", "video_duration": "videoDuration",
                   "rank": "rank", "material_uri": "materialUri", "material_tags": "materialTags"}


def _leaderboard_fields(rows: list[dict]) -> list[dict]:
    """Use the meta report's field groups and defaults with facts available in this run."""
    path = Path(__file__).resolve().parents[1] / "config/yuntu-material-field-registry-v1.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    definitions = {item["id"]: item for item in registry["fields"]}
    available = {"title"}
    for field_id, key in _ROW_FIELD_KEYS.items():
        if any(row.get(key) not in (None, "", []) for row in rows):
            available.add(field_id)
    available.update("metrics." + item["key"] for item in _metric_columns(rows))
    ids = [item["id"] for item in registry["fields"] if item["id"] in available]
    ids.extend(sorted(available - set(ids)))
    fields = [{"id": field_id, "label": definitions.get(field_id, {}).get("label", field_id),
               "group": _FIELD_GROUPS.get(field_id, "其他指标"),
               "kind": definitions.get(field_id, {}).get("kind", "text")}
              for field_id in ids]
    return sorted(fields, key=lambda field: (_FIELD_GROUP_ORDER.index(field["group"]), ids.index(field["id"])))


def _row_field_value(row: dict, field_id: str) -> Any:
    if field_id.startswith("metrics."):
        return (row.get("metrics") or {}).get(field_id.removeprefix("metrics."))
    return row.get(_ROW_FIELD_KEYS.get(field_id, field_id))


_PERIOD_LABELS = {"LAST_7_DAYS": "近 7 天", "LAST_30_DAYS": "近 30 天", "CUSTOM": "自定义日期"}
_RANKING_LABELS = {"EXPOSURE_TOP1000": "曝光量 Top 1000", "CTR_TOP1000": "点击率 Top 1000", "INTERACTION_RATE_TOP1000": "互动率 Top 1000", "COMPLETION_RATE_TOP1000": "完播率 Top 1000"}

def _formula_pairs(row: dict) -> list[tuple[str, str | None]]:
    formula = row.get("contentFormula")
    if row.get("contentFormulaAvailable") is False or not isinstance(formula, dict) or not isinstance(formula.get("labels"), list):
        return []
    detail_labels = formula_detail_labels(formula)
    pairs = []
    for entry in formula["labels"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("level_2_labels"), list):
            continue
        primary_id, primary = resolve_formula_ref(entry.get("level_1_label"), "primary", detail_labels)
        if primary_id is None or primary is None:
            continue
        pairs.append((primary, None))
        for child in entry["level_2_labels"]:
            _, secondary = resolve_formula_ref(child, "secondary", detail_labels)
            if secondary:
                pairs.append((primary, secondary))
    return pairs


def _formula_counts(rows: list[dict]) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
    primary: dict[str, set[str]] = defaultdict(set)
    secondary: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        material_id = row["materialId"]
        for first, second in _formula_pairs(row):
            primary[first].add(material_id)
            if second:
                secondary[first][second].add(material_id)
    return primary, secondary


def _single_formula(row: dict) -> dict:
    if isinstance(row.get("formula"), dict):
        return row["formula"]
    raw = row.get("contentFormula")
    return _project_formula({
        "content_formula": raw,
        "data_availability": {"content_formula": {"available": isinstance(raw, dict)}},
    })


def _single_core(row: dict) -> dict:
    if isinstance(row.get("core"), dict):
        return row["core"]
    return _project_core({"metrics": row.get("metrics") or {}}, default_material_field_registry())


def _display_metric(value: Any, kind: str) -> str:
    if value is None or value == "":
        return "—"
    if kind == "rate" and isinstance(value, (int, float)):
        return f"{value * 100:,.2f}".rstrip("0").rstrip(".") + "%"
    if kind in {"number", "integer"} and isinstance(value, (int, float)):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _formula_overview(rows: list[dict]) -> dict:
    primary, secondary = _formula_counts(rows)
    total = len(set().union(*primary.values())) if primary else 0
    buckets = lambda values, denominator: [
        {"label": label, "value": len(ids), "share": len(ids) / denominator if denominator else 0}
        for label, ids in sorted(values.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    ]
    return {
        "status": "available", "coverage": {"scriptMaterialCount": len(rows), "formulaMaterialCount": total},
        "primaryBuckets": buckets(primary, total),
        "secondaryBucketsByPrimary": {
            label: buckets(children, len(primary[label])) for label, children in secondary.items()
        },
    }


def _quotes(rows: list[tuple[int, dict]]) -> list[dict]:
    """Use only exact excerpts from the verified complete script text."""
    found = []
    seen = set()
    for index, row in rows:
        text = row.get("scriptText")
        formula = row.get("contentFormula")
        if not isinstance(text, str) or not text.strip() or not isinstance(formula, dict):
            continue
        sources = formula.get("sources")
        script = sources.get("script") if isinstance(sources, dict) else None
        detail = script.get("detail") if isinstance(script, dict) else None
        if not isinstance(detail, dict):
            continue
        for key in sorted(detail):
            value = detail[key]
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                quote = candidate.strip()
                key = (row["materialId"], quote)
                if quote and quote in text and key not in seen:
                    found.append({"row": row, "index": index, "text": quote})
                    seen.add(key)
    return found


_STYLE = """
:root{color-scheme:light;--bg:#f4f7fb;--surface:#fff;--soft:#f8fafc;--line:#dfe6f0;--line-strong:#c9d5e5;--text:#172033;--muted:#63708a;--accent:#2563eb;--accent-soft:#eff6ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--text);background:var(--bg)}
*{box-sizing:border-box}body{margin:0;padding:32px}main{max-width:1120px;margin:auto}h1{margin:0 0 10px;font-size:30px;line-height:1.25}h2{font-size:20px;margin:0 0 16px}h3{font-size:16px;margin:0 0 12px}p,li{line-height:1.65}p{margin:8px 0 16px}a{color:var(--accent)}button,input{font:inherit}button{min-height:40px;padding:8px 14px;border:1px solid var(--line-strong);border-radius:9px;background:var(--surface);color:var(--text);cursor:pointer}button:hover,.section-nav a:hover{border-color:var(--accent);background:var(--accent-soft)}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #93b4ef;outline-offset:2px}.muted,.subtitle{color:var(--muted)}.subtitle{font-size:13px;margin-bottom:20px}.section-nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}.section-nav a{min-height:40px;padding:8px 14px;border:1px solid var(--line-strong);border-radius:9px;background:#fff;color:var(--text);text-decoration:none;font-size:14px}.section-nav a.active{background:var(--accent);border-color:var(--accent);color:#fff}.panel,.material-card,.toolbar{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 24px rgba(34,64,112,.05)}.panel{padding:22px;margin:16px 0}.summary-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:0 0 20px}.summary-cards div{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}.summary-cards span{display:block;font-size:12px;color:var(--muted)}.summary-cards strong{display:block;font-size:24px;margin-top:4px}.context{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:0}.context>div{padding:12px;background:var(--soft);border:1px solid var(--line);border-radius:10px}.context dt{font-size:12px;color:var(--muted)}.context dd{margin:6px 0 0;font-size:14px;overflow-wrap:anywhere}.toolbar{padding:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.toolbar input{flex:1;min-width:210px}.toolbar details{position:relative}.toolbar details[open] .metric-picker{position:absolute;z-index:5;right:0;top:38px;width:min(310px,80vw);max-height:320px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px;box-shadow:0 12px 25px rgba(34,64,112,.13)}.metric-picker label{display:block;padding:7px;font-size:13px}.metric-picker input{min-width:0;margin-right:7px}input[type=search]{min-height:42px;padding:9px 12px;border:1px solid var(--line-strong);border-radius:9px;background:#fff;color:var(--text)}.count-note{font-size:13px;color:var(--muted);margin:16px 2px 10px}.table-wrap{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:14px;margin:16px 0}table{width:100%;min-width:750px;border-collapse:separate;border-spacing:0;font-size:13px}th,td{text-align:left;padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:top}th{position:sticky;top:0;background:var(--soft);white-space:nowrap}th button{border:0;background:none;padding:4px 0;min-height:28px;font-weight:650}.material-name-column{min-width:340px;width:340px;position:sticky;left:0;background:#fff}th.material-name-column{background:var(--soft);z-index:2}.material-title{display:block;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text);text-decoration:none;font-weight:550}.material-title:hover{color:var(--accent)}.material-id{display:block;font-size:12px;color:var(--muted);margin-top:6px;overflow-wrap:anywhere}.pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:13px}.pager div{display:flex;align-items:center;gap:8px}.formula-grid{display:grid;grid-template-columns:1fr;gap:16px}.formula-chart{padding:20px;background:#fff;border:1px solid var(--line);border-radius:14px}.bar-row{display:grid;grid-template-columns:130px minmax(0,1fr) 48px;align-items:center;gap:10px;margin:15px 0;font-size:13px}.bar-track{height:14px;background:#e9eef7;border-radius:8px;overflow:hidden}.bar-fill{display:block;height:100%;background:#5b8def;border-radius:8px}.formula-children{padding:0 0 0 18px}.formula-children li{padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}.single-shell{display:grid;grid-template-columns:230px minmax(0,1fr);gap:16px;align-items:start}.selector{position:sticky;top:18px;padding:16px;background:#fff;border:1px solid var(--line);border-radius:14px;max-height:70vh;overflow:auto}.selector h2{font-size:17px}.selector input{width:100%;min-width:0}.material-option{display:block;width:100%;text-align:left;margin-top:7px;padding:10px 11px;border:1px solid var(--line);border-radius:9px;color:var(--text);background:#fff;font-size:12px;overflow-wrap:anywhere;line-height:1.55;text-decoration:none}.material-option.active,.material-option:hover{background:var(--accent-soft);border-color:var(--accent)}.material-card{padding:22px;margin:0 0 16px;min-width:0}.material-card h2{overflow-wrap:anywhere}.material-card h3{margin-top:22px}.eyebrow{font-size:12px;color:var(--muted)}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric-card{padding:14px;border:1px solid var(--line);border-radius:10px;background:#fbfdff;min-width:0}.metric-card span{display:block;font-size:12px;color:var(--muted)}.metric-card strong{display:block;font-size:21px;overflow-wrap:anywhere}.script-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.9;padding:16px;border:1px solid var(--line);border-radius:10px;background:#fbfdff;font-size:14px}.source-note{font-size:12px;color:var(--muted)}.source-copy{border-top:1px solid var(--line);padding:14px 0}.quote-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.quote-card{border:1px solid var(--line);border-top:3px solid var(--accent);border-radius:13px;background:#fff;padding:20px}.quote-card blockquote{margin:0 0 14px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85}.empty{padding:30px;text-align:center;color:var(--muted)}.footer{font-size:12px;color:var(--muted);margin:22px 0}[hidden]{display:none!important}
@media(max-width:760px){body{padding:18px 12px}h1{font-size:24px}.summary-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.context,.formula-grid,.single-shell,.quote-grid{grid-template-columns:1fr}.selector{position:static;max-height:280px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.panel,.material-card{padding:16px}.material-name-column{min-width:250px;width:250px}.material-title{max-width:220px}.bar-row{grid-template-columns:90px minmax(0,1fr) 38px}}
@media print{body{background:#fff;padding:0}.section-nav,.toolbar,.pager,.selector{display:none!important}.single-shell{display:block}.panel,.material-card{box-shadow:none}}
"""

_DETAIL_STYLE = """
.brand-tabs{display:flex;gap:8px;margin:0 0 16px;padding:5px;overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px}.brand-tabs button{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;min-height:36px;padding:6px 12px;border-color:transparent;white-space:nowrap}.brand-tabs button.active{border-color:var(--accent);background:var(--accent-soft);color:#1d4ed8;font-weight:650}.brand-tabs-count{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}body.modal-open{overflow:hidden}
.script-section{display:grid;gap:16px}.script-card-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}.script-card-header h3{margin:0}.script-card-hint{color:var(--muted);font-size:12px}.script-formula-svg{width:min(760px,100%);height:290px;margin:0 auto}.chart-box{min-width:0;max-width:100%;min-height:260px;overflow-x:auto;overflow-y:hidden}.chart-box svg{display:block;min-width:0;max-width:100%;height:290px}.script-formula-bar{fill:#5b8def;cursor:pointer}.script-formula-bar:hover,.script-formula-bar:focus{fill:#2563eb;outline:none}.chart-axis{stroke:#dfe6f0;stroke-width:1}.chart-value{fill:var(--text);font-size:12px;font-weight:650}.chart-label{fill:var(--muted);font-size:11px}.script-formula-donut{display:grid;grid-template-columns:minmax(220px,1fr) minmax(180px,1fr);align-items:center;gap:14px}.script-formula-donut svg{min-width:220px;height:270px}.donut-segment{cursor:pointer;stroke:#fff;stroke-width:2;outline:none;transform-box:view-box;transform-origin:116px 130px;transition:transform .24s ease,filter .24s ease,stroke-width .24s ease}.donut-segment:hover,.donut-segment:focus,.donut-segment.active{filter:brightness(.9)}.donut-center-label{fill:#63708a;font-size:12px;text-anchor:middle}.donut-center-value{fill:#172033;font-size:18px;font-weight:700;text-anchor:middle}.legend-item{display:grid;grid-template-columns:10px minmax(0,1fr);gap:7px;align-items:start;min-height:32px;padding:4px 6px;border:0;border-radius:6px;text-align:left;font-size:12px}.legend-item:hover,.legend-item:focus,.legend-item.active{background:var(--accent-soft);outline:none}.legend-detail{display:block;margin-top:2px;color:var(--muted)}.legend-swatch{display:block;width:10px;height:10px;margin-top:4px;border-radius:2px}.chart-tooltip{position:fixed;z-index:30;max-width:280px;padding:10px 12px;pointer-events:none;background:#172033;color:#fff;border-radius:8px;box-shadow:0 8px 24px rgba(15,23,42,.2);font-size:12px;line-height:1.65}
.single-material-panel{scroll-margin-top:74px;padding:16px 0 22px;border-bottom:1px solid var(--line)}.single-panel-heading{display:flex;align-items:center;min-height:34px;margin:0 0 14px;padding-left:12px;border-left:4px solid var(--accent)}.single-panel-heading h3{margin:0;color:#1f2f49;font-size:18px}.single-core-toolbar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.single-core-toolbar p{margin:4px 0 0;color:var(--muted);font-size:12px}.single-core-toolbar button{min-height:34px;padding:6px 10px}.single-material-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.single-material-metrics .metric-card{min-height:104px}.single-formula-workspace{display:grid;gap:14px}.single-formula-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}.single-formula-toolbar h3{margin:0;font-size:18px}.single-formula-copy{min-height:28px;padding:4px 8px;border:0;color:var(--muted);font-size:12px}.single-formula-legend{display:flex;flex-wrap:wrap;gap:8px 16px;color:var(--muted);font-size:12px}.single-formula-legend-item{display:inline-flex;align-items:center;gap:6px}.single-formula-legend-dot{width:9px;height:9px;border-radius:2px}.single-formula-flow{display:flex;align-items:stretch;gap:10px;overflow-x:auto;padding-bottom:2px}.single-formula-flow-group{display:grid;grid-template-rows:auto 1fr;flex:1 1 0;gap:8px;min-width:180px;padding:10px;border:1px solid var(--line);border-radius:12px;background:#fbfdff}.single-formula-node{width:100%;min-width:0;padding:8px 10px;border:1px solid color-mix(in srgb,var(--single-formula-color) 55%,var(--line));border-radius:8px;background:color-mix(in srgb,var(--single-formula-color) 10%,white);color:var(--text);text-align:left}.single-formula-node:hover,.single-formula-node:focus-visible,.single-formula-node.active{border-color:var(--single-formula-color);background:color-mix(in srgb,var(--single-formula-color) 20%,white);color:var(--single-formula-color);outline:none}.single-formula-level{display:block;margin-bottom:3px;color:var(--muted);font-size:10px}.single-formula-node strong{display:block;overflow-wrap:anywhere;line-height:1.45}.single-formula-primary{min-height:54px;text-align:center}.single-formula-secondary-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:6px;align-content:start}.single-formula-secondary{min-height:44px;padding:6px 8px}.single-formula-empty{display:flex;align-items:center;justify-content:center;min-height:44px;color:var(--muted);font-size:12px}.single-formula-flow-arrow{display:flex;flex:0 0 22px;align-items:center;justify-content:center;color:var(--muted);font-size:20px}.single-formula-sources{display:grid;gap:12px;margin-top:4px}.single-formula-source{padding:14px;border:1px solid var(--line);border-radius:10px;background:#fbfdff}.single-formula-source-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px}.single-formula-source-header h3{margin:0;font-size:14px}.single-formula-source p{margin:0;line-height:1.7;white-space:pre-wrap}.single-formula-highlight{padding:1px 3px;border-radius:3px;cursor:pointer}.single-formula-highlight.active{outline:2px solid currentColor;outline-offset:1px}.single-formula-highlight:not(.active){opacity:.42}
.field-modal{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:24px}.field-modal-backdrop{position:absolute;inset:0;background:rgba(15,23,42,.42);backdrop-filter:blur(2px)}.field-modal-panel{position:relative;display:flex;flex-direction:column;width:min(1180px,100%);height:min(760px,calc(100vh - 48px));overflow:hidden;background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:0 24px 70px rgba(15,23,42,.24)}.field-modal-header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:22px 24px 16px;border-bottom:1px solid var(--line)}.field-modal-header h2{margin:0}.field-modal-header p{margin:6px 0 0;color:var(--muted);font-size:13px}.field-modal-close{min-width:36px;min-height:36px;padding:0;border:0;border-radius:50%;color:var(--muted);font-size:24px}.field-modal-body{display:grid;grid-template-columns:190px minmax(0,1fr) 290px;flex:1;min-height:0}.field-modal-groups{min-width:0;padding:14px 10px;overflow:auto;background:#f8fafc;border-right:1px solid var(--line)}.field-modal-groups button{display:flex;justify-content:space-between;width:100%;min-height:38px;margin-bottom:4px;padding:8px 10px;border:0;color:#45546b;text-align:left}.field-modal-groups button.active{background:var(--accent-soft);color:#1d4ed8;font-weight:650;box-shadow:inset 3px 0 0 var(--accent)}.field-modal-main{display:flex;flex-direction:column;min-width:0;min-height:0}.field-modal-search{padding:14px 20px;border-bottom:1px solid var(--line)}.field-modal-search input{width:100%;min-width:0;height:40px;padding:8px 12px;border:1px solid var(--line-strong);border-radius:8px}.field-options{min-height:0;overflow:auto;padding:18px 22px 24px}.field-option-section+.field-option-section{margin-top:22px}.field-option-section h3{margin:0 0 10px;font-size:14px;color:#263752}.field-option-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 12px}.field-option{display:flex;align-items:flex-start;gap:9px;min-height:40px;padding:9px 10px;border:1px solid transparent;border-radius:8px;color:#34445e;font-size:14px;cursor:pointer}.field-option:hover{background:#f8fafc;border-color:#e6ebf3}.field-option input{width:16px;height:16px;margin:1px 0 0;accent-color:var(--accent)}.field-modal-selected{display:flex;flex-direction:column;min-width:0;min-height:0;background:#fbfcfe;border-left:1px solid var(--line)}.field-modal-selected-header{display:flex;align-items:center;justify-content:space-between;padding:16px 16px 12px;border-bottom:1px solid var(--line)}.field-modal-selected-header span{color:var(--muted);font-size:12px}.field-modal-selected-header button{min-height:28px;padding:4px 8px;border:0;color:var(--accent);font-size:12px}.field-modal-selected-list{min-height:0;margin:0;padding:12px;overflow:auto;list-style:none}.field-modal-selected-item{display:flex;align-items:center;gap:8px;min-height:38px;margin-bottom:7px;padding:7px 8px;background:#f1f3f7;border-radius:7px;color:#34445e;font-size:13px}.field-modal-selected-item button{min-width:24px;min-height:24px;margin-left:auto;padding:0;border:0;color:var(--muted)}.field-modal-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 24px;border-top:1px solid var(--line)}.field-modal-footer p{margin:0;color:var(--muted);font-size:12px}.field-modal-footer-actions{display:flex;gap:8px}.field-modal-footer .primary{border-color:var(--accent);background:var(--accent);color:#fff}
@media(max-width:900px){.field-modal-body{grid-template-columns:160px minmax(0,1fr)}.field-modal-selected{grid-column:2;max-height:190px;border-top:1px solid var(--line)}.single-material-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:700px){.script-formula-donut{grid-template-columns:1fr}.script-formula-donut svg{margin:auto}.single-material-metrics{grid-template-columns:1fr}.single-formula-toolbar{align-items:flex-start;flex-direction:column}.single-formula-flow{flex-direction:column}.single-formula-flow-group{flex:0 0 auto;min-width:0}.single-formula-flow-arrow{transform:rotate(90deg);min-height:20px}.field-modal{padding:10px}.field-modal-panel{height:calc(100vh - 20px)}.field-modal-body{display:flex;flex-direction:column}.field-modal-groups{display:flex;gap:6px;padding:10px;overflow-x:auto;border-right:0;border-bottom:1px solid var(--line)}.field-modal-groups button{flex:0 0 auto;width:auto;margin:0;white-space:nowrap}.field-modal-selected{max-height:190px;border-top:1px solid var(--line);border-left:0}.field-option-grid{grid-template-columns:1fr}.field-modal-footer{align-items:flex-end;flex-direction:column}.field-modal-footer-actions{width:100%}.field-modal-footer-actions button{flex:1}}
"""

_SCOPE_STYLE = """
.report-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.material-scope-switch{display:flex;gap:10px;flex:0 0 auto}.material-scope-switch button{width:150px;height:54px;padding:0 12px;border-color:var(--accent);border-radius:9px;background:#fff;color:var(--accent);font-size:16px;font-weight:650;line-height:1.3}.material-scope-switch button:hover:not(:disabled){border-color:#1d4ed8;background:var(--accent-soft);color:var(--accent)}.material-scope-switch button.active{background:var(--accent);color:#fff}.material-scope-switch button.active:hover:not(:disabled){border-color:#1d4ed8;background:#1d4ed8;color:#fff}
@media(max-width:760px){.report-heading{flex-direction:column}.material-scope-switch{width:100%}.material-scope-switch button{flex:1;width:auto;min-width:0}}
@media print{.material-scope-switch{display:none}}
"""

_SCRIPT = """
(() => {
  const all = selector => Array.from(document.querySelectorAll(selector));
  const model = JSON.parse(document.getElementById("report-ui-data").textContent);
  const fields = model.leaderboardFields || [];
  const fieldMap = Object.fromEntries(fields.map(field => [field.id, field]));
  const groupOrder = ["素材基础信息", "内容曝光与播放", "点击与转化", "互动指标", "其他指标"];
  const fieldGroups = groupOrder.filter(group => fields.some(field => field.group === group));
  const defaultFieldIds = model.defaultFieldIds || ["title"];
  const storageKey = `yuntu-script-report:${model.runId || "unknown"}:v${model.schemaVersion || 1}:leaderboard`;
  const orderedIds = ids => { const selected = new Set(Array.isArray(ids) ? ids : []); selected.add("title"); return fields.filter(field => selected.has(field.id)).map(field => field.id); };
  const brandOverview = model.brandOverview || {status:"unavailable", tabs:[], materialIdsByTab:{}};
  let selectedBrand = brandOverview.status === "available" ? "all" : null;
  const detailIds = new Set((model.detailMaterialIds || []).map(String));
  const scopeButtons = all("[data-material-scope]");
  let materialScope = "all";
  const panels = all("[data-panel]");
  const tabs = all("[data-section]");
  const cards = all("[data-card]");
  const options = all(".material-option");
  const openLinks = all("[data-open]");
  const rows = all("[data-row]");
  let page = 1, sortKey = "", direction = 0;
  const pageSize = 10;
  const normalize = value => String(value || "").normalize("NFKC").toLocaleLowerCase().trim();
  function section(name) {
    panels.forEach(item => { item.hidden = item.dataset.panel !== name; });
    tabs.forEach(item => {
      const active = item.dataset.section === name;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    });
  }
  function openMaterial(index) {
    if (materialScope !== "detail") applyScope("detail");
    cards.forEach(item => { item.hidden = item.dataset.card !== index; });
    options.forEach(item => item.classList.toggle("active", item.dataset.open === index));
    section("single-material");
  }
  tabs.forEach(item => item.addEventListener("click", event => {
    event.preventDefault(); section(item.dataset.section);
  }));
  openLinks.forEach(item => item.addEventListener("click", event => {
    event.preventDefault(); openMaterial(item.dataset.open);
  }));
  const search = document.getElementById("material-search");
  function renderBrandTabs() {
    all("[data-brand-tabs]").forEach(container => {
      container.innerHTML = brandOverview.status === "available" ? brandOverview.tabs.map(tab => { const count = materialScope === "detail" ? (brandOverview.materialIdsByTab?.[tab.id] || []).filter(id => detailIds.has(String(id))).length : tab.count; return `<button type="button" role="tab" data-brand-tab-id="${escapeText(tab.id)}" aria-selected="${tab.id === selectedBrand}" class="${tab.id === selectedBrand ? "active" : ""}">${escapeText(tab.label)}<span class="brand-tabs-count">${escapeText(count)}</span></button>`; }).join("") : '<span class="source-note">本轮品牌分类事实不可用</span>';
    });
  }
  function applyScope(scope) {
    const currentSection = tabs.find(tab => tab.classList.contains("active"))?.dataset.section || "filters";
    materialScope = scope;
    scopeButtons.forEach(button => { const active = button.dataset.materialScope === scope; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
    document.getElementById("materials-tab").textContent = scope === "detail" ? "详情素材榜单" : "全量素材榜单";
    tabs.forEach(tab => { tab.hidden = scope === "all" && !["filters", "materials"].includes(tab.dataset.section); });
    page = 1;
    renderBrandTabs();
    section(scope === "all" && !["filters", "materials"].includes(currentSection) ? "filters" : currentSection);
    updateRows();
  }
  scopeButtons.forEach(button => button.addEventListener("click", () => applyScope(button.dataset.materialScope)));
  function escapeText(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;"}[char] || "&quot;")); }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-brand-tab-id]");
    if (!button || !brandOverview.tabs.some(tab => tab.id === button.dataset.brandTabId)) return;
    const focusedBar = button.closest("[data-brand-tabs]");
    selectedBrand = button.dataset.brandTabId;
    page = 1;
    renderBrandTabs(); updateRows();
    Array.from(focusedBar?.querySelectorAll("[data-brand-tab-id]") || []).find(tab => tab.dataset.brandTabId === selectedBrand)?.focus();
    window.dispatchEvent(new CustomEvent("yuntu:brand-changed", {detail:{brandId:selectedBrand}}));
  });
  function updateRows() {
    const query = normalize(search.value);
    const brandIds = selectedBrand == null ? null : new Set(brandOverview.materialIdsByTab?.[selectedBrand] || []);
    const matching = rows.filter(row => (materialScope === "all" || detailIds.has(row.dataset.materialId)) && (!brandIds || brandIds.has(row.dataset.materialId)) && normalize(row.dataset.search).includes(query));
    if (sortKey && direction) matching.sort((a,b) => {
      const value = row => row.querySelector(`[data-field-cell="${sortKey}"]`)?.dataset.sortValue || "";
      const av = value(a), bv = value(b);
      if (!av || !bv) return av === bv ? Number(a.dataset.row) - Number(b.dataset.row) : av ? -1 : 1;
      const kind = fieldMap[sortKey]?.kind;
      const compared = ["number", "integer", "rate"].includes(kind) ? Number(av) - Number(bv) : normalize(av).localeCompare(normalize(bv));
      return compared * direction || Number(a.dataset.row) - Number(b.dataset.row);
    });
    page = Math.max(1, Math.min(page, Math.max(1, Math.ceil(matching.length / pageSize))));
    rows.forEach(row => { row.hidden = true; });
    matching.slice((page-1)*pageSize, page*pageSize).forEach(row => {
      row.hidden = false; document.getElementById("material-rows").appendChild(row);
    });
    document.getElementById("visible-count").textContent = "共 " + matching.length + " 条";
    document.getElementById("table-range").textContent = matching.length ? "显示 " + ((page-1)*pageSize+1) + "-" + Math.min(page*pageSize,matching.length) + " / " + matching.length : "暂无记录";
    document.getElementById("page-number").textContent = page + " / " + Math.max(1,Math.ceil(matching.length/pageSize));
    document.getElementById("previous-page").disabled = page <= 1;
    document.getElementById("next-page").disabled = page >= Math.max(1,Math.ceil(matching.length/pageSize));
    document.getElementById("no-results").hidden = matching.length > 0;
  }
  search.addEventListener("input", () => { page = 1; updateRows(); });
  all("[data-sort]").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.sort;
    direction = sortKey === key ? direction === 1 ? -1 : direction === -1 ? 0 : 1 : 1;
    sortKey = direction ? key : "";
    all("[data-sort] span").forEach(mark => { mark.textContent = ""; });
    if (direction) button.querySelector("span").textContent = direction === 1 ? " ↑" : " ↓";
    updateRows();
  }));
  document.getElementById("previous-page").onclick = () => { page--; updateRows(); };
  document.getElementById("next-page").onclick = () => { page++; updateRows(); };
  const modal = document.getElementById("field-management"), fieldGroupButtons = document.getElementById("field-groups"), fieldSearch = document.getElementById("field-search"), fieldOptions = document.getElementById("field-options"), selectedFields = document.getElementById("selected-fields");
  const picker = {visibleIds:[], draftIds:null, query:"", group:fieldGroups[0] || "", lastFocused:null};
  try { picker.visibleIds = orderedIds(JSON.parse(localStorage.getItem(storageKey) || "null") || defaultFieldIds); }
  catch (_) { picker.visibleIds = orderedIds(defaultFieldIds); }
  function showColumns() {
    const selected = new Set(picker.visibleIds);
    all("[data-col]").forEach(item => { item.hidden = !selected.has(item.dataset.col); });
    all("[data-field-cell]").forEach(item => { item.hidden = !selected.has(item.dataset.fieldCell); });
  }
  function renderDialog() {
    picker.draftIds = orderedIds(picker.draftIds);
    fieldGroupButtons.innerHTML = fieldGroups.map(group => `<button type="button" data-field-group="${escapeText(group)}" class="${!picker.query && picker.group === group ? "active" : ""}">${escapeText(group)}<span>${fields.filter(field => field.group === group).length}</span></button>`).join("");
    const query = normalize(picker.query);
    const available = fields.filter(field => query ? normalize(field.label).includes(query) || normalize(field.id).includes(query) : field.group === picker.group);
    const grouped = {}; available.forEach(field => (grouped[field.group] ||= []).push(field));
    fieldOptions.innerHTML = Object.entries(grouped).map(([group, items]) => `<section class="field-option-section"><h3>${escapeText(group)}</h3><div class="field-option-grid">${items.map(field => `<label class="field-option"><input type="checkbox" data-field-id="${escapeText(field.id)}"${picker.draftIds.includes(field.id) ? " checked" : ""}${field.id === "title" ? " disabled" : ""}><span>${escapeText(field.label)}</span></label>`).join("")}</div></section>`).join("") || '<div class="empty">没有匹配字段</div>';
    document.getElementById("selected-count").textContent = `（${picker.draftIds.length}）`;
    selectedFields.innerHTML = picker.draftIds.map(id => { const field = fieldMap[id]; return `<li class="field-modal-selected-item"><span class="field-drag-handle">☷</span><span>${escapeText(field.label)}</span>${id === "title" ? '<span class="field-required">固定</span>' : `<button type="button" data-remove-field-id="${escapeText(id)}" aria-label="移除${escapeText(field.label)}">×</button>`}</li>`; }).join("");
  }
  function openDialog(event) { picker.lastFocused = event.currentTarget; picker.draftIds = [...picker.visibleIds]; picker.query = ""; picker.group = fieldGroups[0] || ""; fieldSearch.value = ""; modal.hidden = false; document.body.classList.add("modal-open"); renderDialog(); fieldSearch.focus(); }
  function closeDialog() { modal.hidden = true; picker.draftIds = null; picker.query = ""; document.body.classList.remove("modal-open"); picker.lastFocused?.focus(); }
  modal.addEventListener("click", event => {
    const action = event.target.closest("[data-field-action]")?.dataset.fieldAction;
    if (action === "cancel") { closeDialog(); return; }
    if (action === "confirm") { picker.visibleIds = orderedIds(picker.draftIds); try { localStorage.setItem(storageKey, JSON.stringify(picker.visibleIds)); } catch (_) {} page = 1; closeDialog(); showColumns(); updateRows(); return; }
    if (action === "reset") { picker.draftIds = orderedIds(defaultFieldIds); renderDialog(); return; }
    const group = event.target.closest("[data-field-group]")?.dataset.fieldGroup;
    if (group) { picker.group = group; picker.query = ""; fieldSearch.value = ""; renderDialog(); return; }
    const remove = event.target.closest("[data-remove-field-id]")?.dataset.removeFieldId;
    if (remove) { picker.draftIds = picker.draftIds.filter(id => id !== remove); renderDialog(); }
  });
  modal.addEventListener("change", event => { const input = event.target.closest("[data-field-id]"); if (!input || input.dataset.fieldId === "title") return; picker.draftIds = orderedIds(input.checked ? [...picker.draftIds, input.dataset.fieldId] : picker.draftIds.filter(id => id !== input.dataset.fieldId)); renderDialog(); });
  fieldSearch.addEventListener("input", event => { picker.query = event.target.value; renderDialog(); fieldSearch.focus(); });
  document.getElementById("field-button").addEventListener("click", openDialog);
  document.addEventListener("keydown", event => { if (event.key === "Escape" && !modal.hidden) closeDialog(); });
  showColumns();
  if (cards.length) {
    cards.forEach((item,index) => { item.hidden = index !== 0; });
    options[0]?.classList.add("active");
  }
  document.getElementById("report-controls").hidden = false;
  document.getElementById("table-pager").hidden = false;
  document.getElementById("quote-search").hidden = false;
  const quoteCards = all("[data-quote]");
  const quoteSearch = document.getElementById("quote-search");
  quoteSearch.addEventListener("input", () => {
    const query = normalize(quoteSearch.value);
    quoteCards.forEach(card => { card.hidden = !normalize(card.textContent).includes(query); });
  });
  applyScope("all");
})();
"""

_DETAIL_SCRIPT = """
(() => {
  const model = JSON.parse(document.getElementById("report-ui-data").textContent);
  let formulaOverview = model.scriptFormulaOverview;
  const singleModels = model.singleModels;
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;","'":"&#39;"}[char]));
  const normalize = value => String(value == null ? "" : value).normalize("NFKC").toLocaleLowerCase().trim();
  const percent = value => new Intl.NumberFormat("zh-CN", {style:"percent", maximumFractionDigits:2}).format(value);
  const colors = ["#2563eb", "#60a5fa", "#14b8a6", "#f59e0b", "#ef6a6a", "#8b5cf6", "#64748b", "#22c55e"];
  const formulaColors = ["#ef6a6a", "#8b5cf6", "#22b573", "#f59e0b", "#64748b"];
  const chartTooltip = document.getElementById("chart-tooltip");
  const chartState = {primary:null, secondary:null};
  function hideTooltip() { chartTooltip.hidden = true; }
  function showTooltip(event, bucket, level, total) {
    if (!bucket) return;
    const rect = event.currentTarget.getBoundingClientRect();
    chartTooltip.innerHTML = `<div>${esc(level)}：${esc(bucket.label)}</div><div>覆盖素材数：${esc(bucket.value)}</div><div>占比：${esc(percent(bucket.share))}</div><div>当前维度标签覆盖总和：${esc(total)}</div>`;
    chartTooltip.hidden = false;
    chartTooltip.style.left = `${Math.min(Math.max(12, rect.right + 8), Math.max(12, window.innerWidth - chartTooltip.offsetWidth - 12))}px`;
    chartTooltip.style.top = `${Math.min(Math.max(12, rect.top), Math.max(12, window.innerHeight - chartTooltip.offsetHeight - 12))}px`;
  }
  function donutPath(start, end, outer, inner, cx, cy) {
    const point = (radius, angle) => `${(cx + radius * Math.cos(angle)).toFixed(2)} ${(cy + radius * Math.sin(angle)).toFixed(2)}`;
    const sweep = end - start, full = Math.PI * 2;
    if (sweep >= full - .0001) { const midpoint = start + Math.PI; return `M ${point(outer,start)} A ${outer} ${outer} 0 1 1 ${point(outer,midpoint)} A ${outer} ${outer} 0 1 1 ${point(outer,start)} L ${point(inner,start)} A ${inner} ${inner} 0 1 0 ${point(inner,midpoint)} A ${inner} ${inner} 0 1 0 ${point(inner,start)} Z`; }
    const large = sweep > Math.PI ? 1 : 0;
    return `M ${point(outer,start)} A ${outer} ${outer} 0 ${large} 1 ${point(outer,end)} L ${point(inner,end)} A ${inner} ${inner} 0 ${large} 0 ${point(inner,start)} Z`;
  }
  function selectSecondary(index) {
    chartState.secondary = chartState.secondary === index ? null : index;
    document.querySelectorAll("[data-script-formula-secondary-index]").forEach(item => {
      const selected = Number(item.dataset.scriptFormulaSecondaryIndex) === chartState.secondary;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-pressed", String(selected));
      if (item.classList.contains("donut-segment")) {
        const angle = Number(item.dataset.scriptFormulaAngle || 0), distance = selected ? 8 : 0;
        item.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px,${(Math.sin(angle) * distance).toFixed(2)}px)`;
      }
    });
  }
  function attachChart(container, buckets, level, total) {
    container.querySelectorAll("[data-script-formula-index],[data-script-formula-secondary-index]").forEach(item => {
      const index = Number(item.dataset.scriptFormulaSecondaryIndex ?? item.dataset.scriptFormulaIndex), bucket = buckets[index];
      item.addEventListener("pointerenter", event => showTooltip(event, bucket, level, total));
      item.addEventListener("pointerleave", hideTooltip);
      item.addEventListener("focus", event => showTooltip(event, bucket, level, total));
      item.addEventListener("blur", hideTooltip);
      if (level === "二级内容公式") { item.addEventListener("click", () => selectSecondary(index)); if (item.tagName.toLowerCase() === "path") item.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectSecondary(index); } }); }
    });
  }
  function renderScriptFormula() {
    hideTooltip();
    const chart = document.getElementById("script-formula-chart"), secondaryCard = document.getElementById("script-formula-secondary-card"), secondaryChart = document.getElementById("script-formula-secondary-chart"), secondaryLegend = document.getElementById("script-formula-secondary-legend");
    const buckets = Array.isArray(formulaOverview.primaryBuckets) ? formulaOverview.primaryBuckets : [];
    if (!buckets.length) { chart.innerHTML = '<div class="empty">暂无可展示的一级内容公式</div>'; secondaryCard.hidden = true; return; }
    const width = Math.max(560, Math.min(760, buckets.length * 170, (chart.clientWidth || 900) - 8)), height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20, max = Math.max(...buckets.map(item => Number(item.value)), 1), band = (width - left - 18) / buckets.length;
    const bars = buckets.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight), barWidth = band * .52, x = left + index * band + (band - barWidth) / 2, y = height - bottom - barHeight, center = x + barWidth / 2, label = bucket.label.length > 8 ? `${bucket.label.slice(0,8)}…` : bucket.label; return `<rect class="script-formula-bar" data-script-formula-index="${index}" data-script-primary="${esc(bucket.label)}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="button" aria-label="${esc(bucket.label)}，覆盖${esc(bucket.value)}个素材，占比${esc(percent(bucket.share))}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14,y-6).toFixed(2)}" text-anchor="middle">${esc(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height-42}" text-anchor="middle">${esc(label)}</text>`; }).join("");
    chart.innerHTML = `<svg class="script-formula-svg" style="width:${width}px;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="一级内容公式柱状图"><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-12}" y2="${height-bottom}"></line>${bars}</svg>`;
    attachChart(chart, buckets, "一级内容公式", formulaOverview.coverage?.formulaMaterialCount || 0);
    chart.querySelectorAll("[data-script-primary]").forEach(item => { const select = () => { chartState.primary = item.dataset.scriptPrimary; chartState.secondary = null; renderScriptFormula(); }; item.addEventListener("click", select); item.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } }); });
    if (chartState.primary == null) { secondaryCard.hidden = true; return; }
    const primary = chartState.primary, parent = buckets.find(item => item.label === primary), children = formulaOverview.secondaryBucketsByPrimary?.[primary] || [];
    secondaryCard.hidden = false;
    document.getElementById("script-formula-secondary-title").textContent = `${primary} · 二级内容公式分布`;
    if (!children.length) { secondaryChart.innerHTML = '<div class="empty">暂无该一级内容公式的二级公式数据</div>'; secondaryLegend.innerHTML = ""; return; }
    const total = children.reduce((sum, item) => sum + Math.max(0, Number(item.value) || 0), 0);
    let cursor = -Math.PI / 2;
    const segments = children.map((bucket, index) => { const end = cursor + bucket.value / total * Math.PI * 2, angle = (cursor + end) / 2, path = donutPath(cursor, end, 96, 58, 116, 130); cursor = end; return `<path class="donut-segment" data-script-formula-secondary-index="${index}" data-script-formula-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="button" aria-pressed="false" aria-label="${esc(bucket.label)}，覆盖${esc(bucket.value)}个素材，占比${esc(percent(bucket.share))}"></path>`; }).join("");
    secondaryChart.innerHTML = `<svg viewBox="0 0 232 270" role="group" aria-label="${esc(primary)}二级内容公式环形图">${segments}<text class="donut-center-label" x="116" y="124">当前一级覆盖素材数</text><text class="donut-center-value" x="116" y="150">${esc(parent?.value || 0)}</text></svg>`;
    secondaryLegend.innerHTML = children.map((bucket, index) => `<button type="button" class="legend-item" data-script-formula-secondary-index="${index}" aria-pressed="false" aria-label="${esc(bucket.label)}，覆盖${esc(bucket.value)}个素材，占比${esc(percent(bucket.share))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${esc(bucket.label)}<span class="legend-detail">${esc(bucket.value)} 个素材 · ${esc(percent(bucket.share))}</span></span></button>`).join("");
    attachChart(secondaryChart, children, "二级内容公式", parent?.value || 0);
    attachChart(secondaryLegend, children, "二级内容公式", parent?.value || 0);
  }
  document.getElementById("script-formula-secondary-back").addEventListener("click", () => { chartState.primary = null; chartState.secondary = null; renderScriptFormula(); });
  window.addEventListener("yuntu:brand-changed", event => {
    formulaOverview = model.brandOverview?.views?.[event.detail?.brandId]?.scriptFormulaOverview || {status:"unavailable",coverage:{formulaMaterialCount:0},primaryBuckets:[],secondaryBucketsByPrimary:{}};
    chartState.primary = null; chartState.secondary = null;
    renderScriptFormula();
  });
  document.querySelector('[data-section="scripts"]').addEventListener("click", renderScriptFormula);
  const groupOrder = ["展现指标", "互动/种草指标", "转化指标"];
  const groupById = {"metrics.show_cnt":"展现指标","metrics.ctr":"展现指标","metrics.play_over_rate":"展现指标","metrics.play_3s_rate":"展现指标","metrics.play_5s_rate":"展现指标","metrics.interact_rate":"互动/种草指标","metrics.like_rate":"互动/种草指标","metrics.comment_rate":"互动/种草指标","metrics.share_rate":"互动/种草指标","metrics.cvr":"转化指标","metrics.pvr":"转化指标","metrics.product_wish_button_buy_cart_rate":"转化指标"};
  const storageKey = `yuntu-script-report:${model.runId || "unknown"}:v${model.schemaVersion}:single-core`;
  const coreState = {visibleIds:null, draftIds:null, query:"", group:groupOrder[0], lastFocused:null};
  const modal = document.getElementById("single-core-field-management"), fieldGroups = document.getElementById("single-core-field-groups"), fieldSearch = document.getElementById("single-core-field-search"), fieldOptions = document.getElementById("single-core-field-options"), selectedFields = document.getElementById("single-core-selected-fields");
  const currentIndex = () => document.querySelector("[data-card]:not([hidden])")?.dataset.card;
  const currentModel = () => singleModels[currentIndex()] || null;
  const fields = material => material?.core?.fields || [];
  const orderedIds = (ids, material) => { const selected = new Set(Array.isArray(ids) ? ids : []); return fields(material).filter(field => selected.has(field.id)).map(field => field.id); };
  const group = field => groupById[field.id] || "展现指标";
  function loadFields() {
    if (coreState.visibleIds) return;
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(storageKey) || "null"); } catch (_) {}
    const material = currentModel();
    coreState.visibleIds = orderedIds(Array.isArray(stored) ? stored : material?.core?.defaultFieldIds || [], material);
  }
  function singleDisplay(value, kind) { if (value == null || value === "") return "—"; if (kind === "rate") return `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2}).format(Number(value) * 100)}%`; if (["number","integer"].includes(kind)) return new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2}).format(Number(value)); return String(value); }
  function renderCore(card, material) {
    const content = card.querySelector("[data-single-core]");
    if (!content) return;
    loadFields();
    const visible = orderedIds(coreState.visibleIds, material).map(id => fields(material).find(field => field.id === id)).filter(Boolean);
    content.innerHTML = `<div class="single-panel-heading"><h3>核心数据</h3></div><div class="single-core-toolbar"><p>可按需要选择并调整展示的指标</p><button type="button" data-open-core-fields>自定义指标</button></div><div class="single-material-metrics">${visible.map(field => `<article class="metric-card"><span>${esc(field.label)}</span><strong>${esc(singleDisplay(field.value, field.kind))}</strong></article>`).join("") || '<div class="empty">暂无已选择指标</div>'}</div>`;
  }
  function renderDialog() {
    const material = currentModel(); if (!material) return;
    coreState.draftIds = orderedIds(coreState.draftIds, material);
    const query = normalize(coreState.query), allFields = fields(material);
    fieldGroups.innerHTML = groupOrder.map(name => `<button type="button" data-core-group="${esc(name)}" class="${!query && coreState.group === name ? "active" : ""}">${esc(name)}<span>${allFields.filter(field => group(field) === name).length}</span></button>`).join("");
    const options = allFields.filter(field => query ? normalize(field.label).includes(query) || normalize(field.id).includes(query) : group(field) === coreState.group);
    const byGroup = {}; options.forEach(field => (byGroup[group(field)] ||= []).push(field));
    fieldOptions.innerHTML = Object.entries(byGroup).map(([name, items]) => `<section class="field-option-section"><h3>${esc(name)}</h3><div class="field-option-grid">${items.map(field => `<label class="field-option"><input type="checkbox" data-core-field-id="${esc(field.id)}" ${coreState.draftIds.includes(field.id) ? "checked" : ""}><span>${esc(field.label)}</span></label>`).join("")}</div></section>`).join("") || '<div class="empty">没有匹配指标</div>';
    const selected = coreState.draftIds.map(id => allFields.find(field => field.id === id)).filter(Boolean);
    document.getElementById("single-core-selected-count").textContent = `（${selected.length}）`;
    selectedFields.innerHTML = selected.map(field => `<li class="field-modal-selected-item"><span class="field-drag-handle">☷</span><span>${esc(field.label)}</span><button type="button" data-core-remove="${esc(field.id)}" aria-label="移除${esc(field.label)}">×</button></li>`).join("") || '<li class="empty">暂无已选择指标</li>';
  }
  function openDialog(trigger) { const material = currentModel(); if (!material) return; coreState.lastFocused = trigger; loadFields(); coreState.draftIds = [...coreState.visibleIds]; coreState.query = ""; coreState.group = groupOrder.find(name => fields(material).some(field => group(field) === name)) || groupOrder[0]; fieldSearch.value = ""; modal.hidden = false; document.body.classList.add("modal-open"); renderDialog(); fieldSearch.focus(); }
  function closeDialog() { modal.hidden = true; coreState.draftIds = null; coreState.query = ""; document.body.classList.remove("modal-open"); coreState.lastFocused?.focus(); }
  function confirmDialog() { coreState.visibleIds = orderedIds(coreState.draftIds, currentModel()); try { localStorage.setItem(storageKey, JSON.stringify(coreState.visibleIds)); } catch (_) {} closeDialog(); renderAllCards(); }
  function copyText(value, button) { const done = () => { const original = button.textContent; button.textContent = "已复制"; window.setTimeout(() => { button.textContent = original; }, 1200); }; if (navigator.clipboard?.writeText) { navigator.clipboard.writeText(value).then(done).catch(() => {}); return; } const helper = document.createElement("textarea"); helper.value = value; helper.setAttribute("readonly", ""); helper.style.position = "fixed"; helper.style.opacity = "0"; document.body.appendChild(helper); helper.select(); try { if (document.execCommand("copy")) done(); } finally { helper.remove(); } }
  function formulaColor(formula, id) { const index = (formula.labels || []).findIndex(item => item.id === id || (item.secondary || []).some(child => child.id === id)); return formulaColors[(index < 0 ? 0 : index) % formulaColors.length]; }
  function formulaSourceText(source, formula) {
    const raw = typeof source?.text === "string" ? source.text : ""; if (!raw) return "—";
    const matches = [];
    (source.terms || []).forEach(term => (term.texts || []).forEach(text => { if (typeof text !== "string" || !text) return; let from = 0; while (from < raw.length) { const start = raw.indexOf(text, from); if (start < 0) break; matches.push({start, end:start + text.length, text, id:term.id}); from = start + text.length; } }));
    matches.sort((left, right) => left.start - right.start || right.end - left.end);
    let cursor = 0, output = "";
    matches.forEach(match => { if (match.start < cursor) return; output += esc(raw.slice(cursor, match.start)); output += `<mark class="single-formula-highlight" data-formula-id="${esc(match.id)}" style="background:${formulaColor(formula, match.id)}33;color:${formulaColor(formula, match.id)}" title="点击联动公式标签">${esc(match.text)}</mark>`; cursor = match.end; });
    return output + esc(raw.slice(cursor));
  }
  const formulaSelection = {};
  function selectFormula(card, material, id) {
    const key = card.dataset.card, current = formulaSelection[key]; formulaSelection[key] = current === id ? null : id;
    const selected = formulaSelection[key], formula = material.formula || {};
    const parent = (formula.labels || []).find(item => item.id === selected || (item.secondary || []).some(child => child.id === selected));
    const active = selected == null ? new Set() : new Set(selected === parent?.id ? [parent.id, ...(parent.secondary || []).map(child => child.id)] : [selected]);
    card.querySelectorAll("[data-formula-id]").forEach(item => { const isActive = active.has(item.dataset.formulaId); item.classList.toggle("active", isActive); item.setAttribute("aria-pressed", String(isActive)); });
  }
  function renderFormula(card, material) {
    const content = card.querySelector("[data-single-formula]"); if (!content) return;
    const formula = material.formula;
    if (!formula || formula.status !== "available") { content.innerHTML = `<div class="single-panel-heading"><h3>内容公式</h3></div><div class="empty">内容公式不可用：${esc(formula?.reason || "source_empty")}</div>`; return; }
    const labels = formula.labels || [], sources = formula.sources || {};
    const flow = labels.map((item, index) => `<div class="single-formula-flow-group"><button type="button" class="single-formula-node single-formula-primary" data-formula-id="${esc(item.id)}" aria-pressed="false" style="--single-formula-color:${formulaColors[index % formulaColors.length]}"><span class="single-formula-level">一级内容公式</span><strong>${esc(item.label)}</strong></button><div class="single-formula-secondary-list" aria-label="${esc(item.label)}二级内容公式">${(item.secondary || []).length ? item.secondary.map(child => `<button type="button" class="single-formula-node single-formula-secondary" data-formula-id="${esc(child.id)}" aria-pressed="false" style="--single-formula-color:${formulaColors[index % formulaColors.length]}"><span class="single-formula-level">二级</span><strong>${esc(child.label)}</strong></button>`).join("") : '<span class="single-formula-empty">暂无二级内容公式</span>'}</div></div>${index < labels.length - 1 ? '<span class="single-formula-flow-arrow" aria-hidden="true">→</span>' : ""}`).join("");
    const legend = labels.map((item, index) => `<span class="single-formula-legend-item"><i class="single-formula-legend-dot" style="background:${formulaColors[index % formulaColors.length]}"></i>${esc(item.label)}</span>`).join("");
    const sourceCards = [["script","本视频脚本文案"],["title","视频标题"],["visual","视频画面"]].map(([key,label]) => { const source = sources[key]; if (!source?.text) return ""; return `<article class="single-formula-source"><div class="single-formula-source-header"><h3>${label}</h3><button type="button" class="single-formula-copy" data-copy-source="${esc(key)}">复制文本</button></div><p>${formulaSourceText(source, formula)}</p></article>`; }).join("");
    content.innerHTML = `<div class="single-panel-heading"><h3>内容公式</h3></div><div class="single-formula-workspace"><div class="single-formula-toolbar"><h3>本视频内容公式</h3><button type="button" class="single-formula-copy" data-copy-formula>复制内容公式</button></div><div class="single-formula-legend">${legend || '<span class="empty">暂无公式标签</span>'}</div><div class="single-formula-flow" aria-label="内容公式层级链路">${flow || '<div class="empty">暂无内容公式</div>'}</div><div class="single-formula-sources">${sourceCards || '<div class="empty">暂无公式来源文本</div>'}</div></div>`;
    if (formulaSelection[card.dataset.card]) { const id = formulaSelection[card.dataset.card]; formulaSelection[card.dataset.card] = null; selectFormula(card, material, id); }
  }
  function renderAllCards() { document.querySelectorAll("[data-card]").forEach(card => { const material = singleModels[card.dataset.card]; if (!material) return; renderCore(card, material); renderFormula(card, material); }); }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-open-core-fields]"); if (button) { openDialog(button); return; }
    const formulaButton = event.target.closest("[data-formula-id]"); if (formulaButton) { const card = formulaButton.closest("[data-card]"), material = singleModels[card?.dataset.card]; if (material) selectFormula(card, material, formulaButton.dataset.formulaId); return; }
    const copyFormula = event.target.closest("[data-copy-formula]"); if (copyFormula) { const material = singleModels[copyFormula.closest("[data-card]")?.dataset.card], value = (material?.formula?.labels || []).flatMap(item => (item.secondary || []).map(child => child.label)).join(" + "); copyText(value || "暂无内容公式", copyFormula); return; }
    const copySource = event.target.closest("[data-copy-source]"); if (copySource) { const material = singleModels[copySource.closest("[data-card]")?.dataset.card], value = material?.formula?.sources?.[copySource.dataset.copySource]?.text || ""; copyText(value, copySource); }
  });
  modal.addEventListener("click", event => { const action = event.target.closest("[data-core-action]")?.dataset.coreAction; if (action === "cancel") { closeDialog(); return; } if (action === "confirm") { confirmDialog(); return; } if (action === "reset") { coreState.draftIds = [...(currentModel()?.core?.defaultFieldIds || [])]; coreState.group = groupOrder[0]; renderDialog(); return; } const groupName = event.target.closest("[data-core-group]")?.dataset.coreGroup; if (groupName) { coreState.group = groupName; coreState.query = ""; fieldSearch.value = ""; renderDialog(); return; } const remove = event.target.closest("[data-core-remove]")?.dataset.coreRemove; if (remove) { coreState.draftIds = coreState.draftIds.filter(id => id !== remove); renderDialog(); } });
  modal.addEventListener("change", event => { const input = event.target.closest("input[data-core-field-id]"); if (!input) return; coreState.draftIds = orderedIds(input.checked ? [...coreState.draftIds, input.dataset.coreFieldId] : coreState.draftIds.filter(id => id !== input.dataset.coreFieldId), currentModel()); renderDialog(); });
  fieldSearch.addEventListener("input", event => { coreState.query = event.target.value; renderDialog(); fieldSearch.focus(); });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && !modal.hidden) closeDialog(); else if (event.key === "Escape") hideTooltip(); });
  renderAllCards();
  renderScriptFormula();
})();
"""


def render_script_report_html(report: dict) -> str:
    rows = report["rows"]
    detail_rows = [(index, row) for index, row in enumerate(rows) if row["sourcePaths"]]
    detail_indexes = {row["materialId"]: index for index, row in detail_rows}
    leaderboard_fields = _leaderboard_fields(rows)
    columns = [field for field in leaderboard_fields if field["id"] != "title"]
    selection = report.get("selection") or {}
    formula_overview = report.get("scriptFormulaOverview")
    if not isinstance(formula_overview, dict):
        formula_overview = _formula_overview([row for _, row in detail_rows])
    brand_overview = report.get("brandOverview") or {"status": "unavailable", "tabs": [], "materialIdsByTab": {}, "views": {}}
    single_models = {str(index): {"core": _single_core(row), "formula": _single_formula(row)}
                     for index, row in detail_rows}
    quotes = _quotes(detail_rows)
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; script-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">',
        '<title>云图素材脚本数据工作台</title><style>', _STYLE, _DETAIL_STYLE, _SCOPE_STYLE, '</style></head><body><main>',
        '<div class="report-heading"><div><h1>云图素材脚本数据工作台</h1></div>',
        '<div class="material-scope-switch" role="group" aria-label="素材数据口径"><button type="button" data-material-scope="all" aria-pressed="true">全量素材</button><button type="button" data-material-scope="detail" aria-pressed="false">详情素材</button></div></div>',
        '<nav class="section-nav" aria-label="报告部分">'
    ]
    for key, label in (("filters", "素材筛选条件"), ("materials", "素材榜单"), ("scripts", "脚本内容公式"),
                       ("single-material", "单素材脚本"), ("quote-library", "脚本片段库")):
        detail_only = ' data-detail-only="true"' if key not in {"filters", "materials"} else ""
        tab_id = ' id="materials-tab"' if key == "materials" else ""
        parts.append('<a href="#' + key + '-panel" data-section="' + key + '"' + detail_only + tab_id + '>' + ("全量素材榜单" if key == "materials" else label) + '</a>')
    parts.append('</nav><noscript><p class="panel">浏览器未启用 JavaScript；全部分区和素材保持展开，可以直接阅读。</p></noscript>')
    period = selection.get("period")
    period = period if isinstance(period, dict) else {}
    material_list = selection.get("materialList")
    material_list = material_list if isinstance(material_list, dict) else {}
    parts.append('<section id="filters-panel" class="panel" data-panel="filters"><h2>素材筛选条件</h2><dl class="context">')
    context = [
        ("行业", selection.get("industry")),
        ("取数周期", _PERIOD_LABELS.get(period.get("date_type") or period.get("type"), period.get("date_type") or period.get("type"))),
        ("开始日期", period.get("custom_start_date") or period.get("start_date")),
        ("结束日期", period.get("custom_end_date") or period.get("end_date")),
        ("榜单口径", _RANKING_LABELS.get(selection.get("rankingMetric"), selection.get("rankingMetric"))),
        ("脚本素材数 TopN", selection.get("targetTopN")),
        ("平台品牌范围", "指定品牌" if selection.get("brandScope") == "SPECIFIED_BRANDS" else "全行业"),
        ("指定品牌", "、".join(selection.get("brands") or []) if selection.get("brandScope") == "SPECIFIED_BRANDS" else "不适用"),
        ("本地筛选条件", json.dumps(material_list.get("filters"), ensure_ascii=False, sort_keys=True) if material_list.get("filters") else "无"),
        ("本地排序", json.dumps(material_list.get("sort"), ensure_ascii=False, sort_keys=True) if material_list.get("sort") else "未提供"),
    ]
    for label, value in context:
        parts.append('<div><dt>' + label + '</dt><dd>' + _e(value) + '</dd></div>')
    parts.append('</dl></section><section id="materials-panel" data-panel="materials"><div class="brand-tabs" data-brand-tabs="materials" role="tablist" aria-label="素材榜单品牌筛选"></div><div class="toolbar" id="report-controls" hidden><input id="material-search" type="search" placeholder="输入素材标题或 ID" aria-label="搜索素材标题或 ID"><button id="field-button" type="button">自定义指标</button></div>')
    parts.append('<section id="field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="field-management-title" hidden><div class="field-modal-backdrop" data-field-action="cancel"></div><div class="field-modal-panel"><header class="field-modal-header"><div><h2 id="field-management-title">自定义指标</h2><p>选择后按固定字段分组顺序呈现表格列</p></div><button class="field-modal-close" type="button" data-field-action="cancel" aria-label="关闭自定义指标">×</button></header><div class="field-modal-body"><nav id="field-groups" class="field-modal-groups" aria-label="字段分组"></nav><section class="field-modal-main" aria-label="可选指标"><div class="field-modal-search"><input id="field-search" type="search" placeholder="搜索指标" aria-label="搜索指标"></div><div id="field-options" class="field-options"></div></section><aside class="field-modal-selected" aria-label="已选择指标"><div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="selected-count"></span></div><button type="button" data-field-action="reset">重置</button></div><ol id="selected-fields" class="field-modal-selected-list"></ol></aside></div><footer class="field-modal-footer"><p>视频标题为固定列，素材 ID 显示在标题下方</p><div class="field-modal-footer-actions"><button type="button" data-field-action="cancel">取消</button><button class="primary" type="button" data-field-action="confirm">确定</button></div></footer></div></section>')
    parts.append('<p class="count-note" id="visible-count">共 ' + str(len(rows)) + ' 条</p><div class="table-wrap"><table aria-label="素材榜单"><thead><tr><th class="material-name-column" scope="col">素材名称</th>')
    for column in columns:
        parts.append('<th scope="col" data-col="' + _e(column["id"]) + '"><button type="button" data-sort="' + _e(column["id"]) + '">' + _e(column["label"]) + '<span></span></button></th>')
    parts.append('</tr></thead><tbody id="material-rows">')
    for index, row in enumerate(rows):
        title = row["title"] or "素材名称未提供"
        material_id = row["materialId"]
        parts.append('<tr data-row="' + str(index) + '" data-material-id="' + _e(material_id) + '" data-search="' + _e(title + " " + material_id) + '"><td class="material-name-column">')
        if material_id in detail_indexes:
            parts.append('<a class="material-title" href="#material-' + str(index) + '" data-open="' + str(index) + '" title="' + _e(title) + '">' + _e(title) + '</a>')
        else:
            parts.append('<span class="material-title" title="' + _e(title) + '">' + _e(title) + '</span>')
        parts.append('<div class="material-id">素材 ID：' + _e(material_id) + '</div></td>')
        for column in columns:
            value = _row_field_value(row, column["id"])
            sortable = "" if value is None or value == "" or isinstance(value, (dict, list, bool)) else str(value)
            display = "、".join(map(str, value)) if isinstance(value, list) else _display_metric(value, column["kind"])
            if column["kind"] == "date" and isinstance(value, str) and len(value) == 8 and value.isdigit():
                display = f"{value[:4]}-{value[4:6]}-{value[6:]}"
            parts.append('<td data-field-cell="' + _e(column["id"]) + '" data-sort-value="' + escape(sortable, quote=True) + '">' + _e(display) + '</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div><div class="empty" id="no-results" hidden>没有匹配素材。</div><div class="pager" id="table-pager" hidden><span id="table-range"></span><div><button type="button" id="previous-page">上一页</button><span id="page-number"></span><button type="button" id="next-page">下一页</button></div></div></section>')
    parts.append('<section id="scripts-panel" data-panel="scripts" class="script-section"><div class="brand-tabs" data-brand-tabs="scripts" role="tablist" aria-label="脚本内容公式品牌筛选"></div><div class="panel"><div class="script-card-header"><h3>一级内容公式</h3><span class="script-card-hint">点击一级内容公式柱状图可以显示二级内容公式分布</span></div><div id="script-formula-chart" class="chart-box"></div></div><div id="script-formula-secondary-card" class="panel" hidden><div class="script-card-header"><h3 id="script-formula-secondary-title">二级内容公式分布</h3><button id="script-formula-secondary-back" type="button">收起二级内容公式</button></div><div class="script-formula-donut"><div id="script-formula-secondary-chart" class="chart-box"></div><div id="script-formula-secondary-legend" class="legend" aria-label="二级内容公式图例"></div></div></div>')
    if formula_overview.get("primaryBuckets"):
        parts.append('<noscript><div class="panel"><h3>公式标签覆盖素材数</h3><ul>')
        for bucket in formula_overview["primaryBuckets"]:
            parts.append('<li>' + _e(bucket["label"]) + '：' + str(bucket["value"]) + ' 个素材</li>')
        parts.append('</ul></div></noscript>')
    parts.append('</section><section id="single-material-panel" data-panel="single-material"><div class="single-shell"><aside class="selector"><h2>单素材脚本</h2>')
    for index, row in detail_rows:
        parts.append('<a class="material-option" data-open="' + str(index) + '" href="#material-' + str(index) + '">' + _e(row["rank"]) + '. ' + _e(row["title"]) + '<span class="material-id">' + _e(row["materialId"]) + '</span></a>')
    parts.append('</aside><div>')
    for index, row in detail_rows:
        transcript = row["audioTranscript"] if isinstance(row["audioTranscript"], dict) else {}
        source_label = {"platform": "平台原文", "agent_audio": "Agent 音频转写", "unavailable": "无可用脚本"}.get(row["scriptSource"], "未提供")
        parts.append('<article class="material-card" id="material-' + str(index) + '" data-card="' + str(index) + '"><p class="eyebrow">素材排名 ' + _e(row["rank"]) + '</p><h2>' + _e(row["title"]) + '</h2><p class="material-id">素材 ID：' + _e(row["materialId"]) + ' · 首投日期：' + _e(row.get("publishTime") or "—") + ' · 脚本来源：' + source_label + '</p>')
        core = single_models[str(index)]["core"]
        parts.append('<section class="single-material-panel" data-single-core><div class="single-panel-heading"><h3>核心数据</h3></div><div class="single-material-metrics">')
        for field in core["fields"]:
            if field["id"] in core["defaultFieldIds"]:
                parts.append('<article class="metric-card"><span>' + _e(field["label"]) + '</span><strong>' + _e(_display_metric(field["value"], field["kind"])) + '</strong></article>')
        parts.append('</div></section><section class="single-material-panel" data-single-formula><div class="single-panel-heading"><h3>内容公式</h3></div>')
        formula = single_models[str(index)]["formula"]
        if formula["status"] == "available":
            parts.append('<div class="single-formula-flow">')
            for group in formula["labels"]:
                parts.append('<div class="single-formula-flow-group"><strong>' + _e(group["label"]) + '</strong><div class="single-formula-secondary-list">')
                for child in group["secondary"]:
                    parts.append('<span>' + _e(child["label"]) + '</span>')
                parts.append('</div></div>')
            parts.append('</div>')
        else:
            parts.append('<p class="empty">内容公式不可用：' + _e(formula.get("reason")) + '</p>')
        parts.append('</section><h3>完整脚本</h3><div class="script-text">' + (_e(row["scriptText"]) if row["scriptText"] else '无可用脚本文本') + '</div>')
        if transcript.get("reason") == "recognition_empty_after_review":
            parts.append('<p class="source-note">Agent 音频理解后未取得可用转写，已按缺口记录；不据此断定音频无人声。复核说明：' + _e(transcript.get("reviewNote")) + '</p>')
        if row["scriptSource"] == "agent_audio":
            parts.append('<p class="source-note">音频转写无可靠时间码，不映射高光帧或画面文字。</p>')
        parts.append('<details class="source-copy"><summary>查看分层来源文本</summary><h3>平台原文</h3><div class="script-text">' + (_e(row["platformScriptText"]) if row["platformScriptText"] else '未提供') + '</div><h3>Agent 音频转写</h3><div class="script-text">' + (_e(transcript.get("text")) if transcript.get("text") else '未提供') + '</div></details></article>')
    if not detail_rows:
        parts.append('<div class="panel empty">本次没有可展示的素材详情。</div>')
    parts.append('</div></div></section><section id="single-core-field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="single-core-field-management-title" hidden><div class="field-modal-backdrop" data-core-action="cancel"></div><div class="field-modal-panel"><header class="field-modal-header"><div><h2 id="single-core-field-management-title">自定义指标</h2><p>选择后按固定字段分组顺序呈现数据卡片</p></div><button class="field-modal-close" type="button" data-core-action="cancel" aria-label="关闭核心数据自定义指标">×</button></header><div class="field-modal-body"><nav id="single-core-field-groups" class="field-modal-groups" aria-label="核心指标分组"></nav><section class="field-modal-main" aria-label="可选核心指标"><div class="field-modal-search"><input id="single-core-field-search" type="search" placeholder="搜索核心指标" aria-label="搜索核心指标"></div><div id="single-core-field-options" class="field-options"></div></section><aside class="field-modal-selected" aria-label="已选择核心指标"><div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="single-core-selected-count"></span></div><button type="button" data-core-action="reset">重置</button></div><ol id="single-core-selected-fields" class="field-modal-selected-list"></ol></aside></div><footer class="field-modal-footer"><p>标题、素材 ID、榜单排名和首投日期固定显示在详情头部</p><div class="field-modal-footer-actions"><button type="button" data-core-action="cancel">取消</button><button class="primary" type="button" data-core-action="confirm">确定</button></div></footer></div></section><section id="quote-library-panel" data-panel="quote-library"><div class="panel"><h2>脚本片段库</h2><p class="muted">仅展示内容公式来源中能在完整脚本中精确命中的原句；不自动切分或补写转写文本。</p><input type="search" id="quote-search" placeholder="搜索原句或素材名称" aria-label="搜索脚本片段" hidden></div><div class="quote-grid">')
    for item in quotes:
        row = item["row"]
        parts.append('<article class="quote-card" data-quote><blockquote>' + _e(item["text"]) + '</blockquote><p class="eyebrow">素材排名 ' + _e(row["rank"]) + '</p><a data-open="' + str(item["index"]) + '" href="#material-' + str(item["index"]) + '">' + _e(row["title"]) + '</a></article>')
    if not quotes:
        parts.append('<div class="panel empty">没有可核对的原句片段；完整脚本可在“单素材脚本”中查看。</div>')
    parts.append('</div></section></main><div id="chart-tooltip" class="chart-tooltip" hidden></div><script id="report-ui-data" type="application/json">')
    parts.append(json.dumps({"runId": report.get("runId"), "schemaVersion": report.get("schemaVersion", 1),
                             "detailMaterialIds": report.get("detailMaterialIds") or [row["materialId"] for _, row in detail_rows],
                             "scriptFormulaOverview": formula_overview, "brandOverview": brand_overview,
                             "leaderboardFields": leaderboard_fields,
                             "defaultFieldIds": [field_id for field_id in _DEFAULT_FIELD_IDS
                                                 if field_id in {field["id"] for field in leaderboard_fields}],
                             "singleModels": single_models},
                            ensure_ascii=False, sort_keys=True).replace("<", "\\u003c"))
    parts.append('</script><script>')
    parts.append(_SCRIPT)
    parts.append(_DETAIL_SCRIPT)
    parts.append('</script></body></html>')
    return "".join(parts)
