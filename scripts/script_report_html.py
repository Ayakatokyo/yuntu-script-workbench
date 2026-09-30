"""Offline single-page script workbench report.

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
    from script_workbench_page import render_page, _scope_filters, _platform_scope_params
except ImportError:
    from .content_formula_labels import formula_detail_labels, resolve_formula_ref
    from .material_field_registry import default_material_field_registry
    from .single_material_overview import _project_core, _project_formula
    from .script_workbench_page import render_page, _scope_filters, _platform_scope_params


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


def render_script_report_html(report: dict) -> str:
    """Render detail scripts, metrics, and formula structure in one page."""
    details = [(index, row) for index, row in enumerate(report['rows']) if row.get('sourcePaths')]
    selection = report.get('selection') or {}
    brand = report.get('brandOverview') or {}
    detail_ids = {str(row.get('materialId')) for _, row in details}
    brand_tabs = []
    for tab in brand.get('tabs') or []:
        ids = [str(value) for value in (brand.get('materialIdsByTab') or {}).get(tab['id'], [])
               if str(value) in detail_ids]
        brand_tabs.append({'id': tab['id'], 'label': tab['label'], 'ids': ids})
    brands_by_id: dict[str, list[str]] = defaultdict(list)
    for tab in brand_tabs:
        if tab['id'] != 'all':
            for material_id in tab['ids']:
                brands_by_id[material_id].append(tab['id'])
    page_materials = []
    for _, row in details:
        material_id = str(row.get('materialId'))
        text = row.get('scriptText') or ''
        core = _single_core(row)
        metrics = [{'id': field['id'], 'label': field['label'],
                    'value': _display_metric(field.get('value'), field.get('kind') or 'text'),
                    'default': field['id'] in core.get('defaultFieldIds', [])}
                   for field in core.get('fields') or []]
        projected = _single_formula(row)
        source_terms: dict[str, list[str]] = defaultdict(list)
        for link in projected.get('links') or []:
            if link.get('source') == 'script':
                source_terms[str(link.get('id'))].extend(
                    term for term in link.get('texts') or []
                    if isinstance(term, str) and term and term in text)
        formula = []
        for group in projected.get('labels') or []:
            children = []
            for child in group.get('secondary') or []:
                child_id = str(child.get('id'))
                children.append({'label': child['label'],
                                 'terms': list(dict.fromkeys(source_terms.get(child_id, [])))})
            formula.append({'label': group['label'], 'children': children})
        source_key = row.get('scriptSource') or 'unavailable'
        source_label = {'platform': '平台原文', 'agent_audio': 'Agent 音频转写',
                        'unavailable': '脚本缺口'}.get(source_key, '来源未提供')
        transcript = row.get('audioTranscript')
        transcript = transcript if isinstance(transcript, dict) else {}
        note = '音频转写无可靠时间码。' if source_key == 'agent_audio' else ''
        if source_key == 'unavailable' and transcript.get('reason') == 'recognition_empty_after_review':
            note = '音频理解后未取得可用转写，已按复核缺口记录；不据此断定音频无人声。'
            if transcript.get('reviewNote'):
                note += f"复核说明：{transcript['reviewNote']}"
        page_materials.append({'id': material_id, 'title': row.get('title') or '素材名称未提供',
                               'rank': row.get('rank'), 'text': text, 'source': source_label,
                               'sourceNote': note, 'metrics': metrics,
                               'formula': formula, 'brands': brands_by_id[material_id],
                               'layeredText': {'平台原文': row.get('platformScriptText') or '',
                                               'Agent 音频转写': transcript.get('text') or ''}})
    period = selection.get('period')
    period = period if isinstance(period, dict) else {}
    material_list = selection.get('materialList')
    material_list = material_list if isinstance(material_list, dict) else {}
    sort = material_list.get('sort') or {}
    scope = [('行业', selection.get('industry')),
             ('取数周期', _PERIOD_LABELS.get(period.get('date_type') or period.get('type'),
                                         period.get('date_type') or period.get('type'))),
             ('开始日期', period.get('custom_start_date') or period.get('start_date')),
             ('结束日期', period.get('custom_end_date') or period.get('end_date')),
             ('选材排序口径', _RANKING_LABELS.get(selection.get('rankingMetric'), selection.get('rankingMetric'))),
             ('脚本素材数 TopN', selection.get('targetTopN')),
             ('平台品牌范围', '指定品牌' if selection.get('brandScope') == 'SPECIFIED_BRANDS' else '全行业')]
    if selection.get('brandScope') == 'SPECIFIED_BRANDS':
        scope.append(('指定品牌', '、'.join(selection.get('brands') or [])))
    scope.extend(_platform_scope_params(selection.get('listParams') or {}))
    scope.extend(_scope_filters(material_list.get('filters')))
    scope.append(('本地排序', f"{sort.get('field') or '—'} · { {'asc': '升序', 'desc': '降序'}.get(sort.get('direction'), sort.get('direction') or '—')}" if sort else '未提供'))
    start = period.get('custom_start_date') or period.get('start_date') or '—'
    end = period.get('custom_end_date') or period.get('end_date') or '—'
    model = {'platform': '云图', 'title': '云图素材脚本工作台',
             'summary': f'{start} 至 {end} · 详情素材 {len(page_materials)} 条 · {selection.get("industry") or "本次行业"}',
             'scope': scope, 'brandTabs': brand_tabs, 'materials': page_materials}
    return render_page(model)
