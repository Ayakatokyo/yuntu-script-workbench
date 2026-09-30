"""The offline report must stay faithful to its source facts and fixed layout."""

import json
import re

from scripts.script_report_html import _formula_overview, _metric_columns, render_script_report_html


def _model(html):
    return json.loads(re.search(r'<script id="workbench-model" type="application/json">(.*?)</script>', html).group(1))


def _row(material_id, *, title, script="", formula=None, detail=True):
    return {
        "materialId": material_id, "rank": int(material_id), "title": title,
        "metrics": {"show_cnt": int(material_id) * 100, "ctr": 0.2},
        "scriptSource": "platform" if script else "unavailable",
        "scriptText": script, "platformScriptText": script, "audioTranscript": None,
        "contentFormula": formula, "creativeBreakdown": None,
        "sourcePaths": {"normalized": "detail.json"} if detail else {},
    }


def test_metric_columns_keep_registry_order_after_json_key_sorting():
    rows = [{"metrics": {"unknown_z": 1, "ctr": 2, "show_cnt": 3, "unknown_a": 4}},
            {"metrics": {"pvr": 5, "ctr": 6}}]
    persisted = json.loads(json.dumps(rows, sort_keys=True))
    expected = ["show_cnt", "ctr", "pvr", "unknown_a", "unknown_z"]
    assert [item["key"] for item in _metric_columns(rows)] == expected
    assert _metric_columns(rows) == _metric_columns(persisted)


def test_html_is_identical_after_report_json_key_sorting():
    row = _row("1", title="第一条", script="甲乙", formula={
        "labels": [], "sources": {"script": {"detail": {"b": ["乙"], "a": ["甲"]}}},
    })
    row["metrics"] = {"ctr": 0.2, "show_cnt": 100}
    report = {
        "runId": "fixture",
        "counts": {"ranking": 1, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "selection": {"materialList": {"sort": {"field": "rank", "direction": "asc"}}},
        "rows": [row],
    }
    persisted = json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True))
    assert render_script_report_html(report) == render_script_report_html(persisted)


def test_detail_sections_exclude_full_ranking_rows():
    report = {
        "counts": {"ranking": 2, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "selection": {"industry": "食品饮料", "targetTopN": 1,
                      "materialList": {"filters": [], "sort": {"field": "rank", "direction": "asc"}}},
        "rows": [
            _row("1", title="第一条", script="真实口播"),
            _row("2", title="只在榜单", detail=False),
        ],
        "brandOverview": {"status": "available", "tabs": [{"id": "all", "label": "全部素材", "count": 2}],
                          "materialIdsByTab": {"all": ["1", "2"]},
                          "materialBrands": {"1": "甲", "2": "乙"}, "views": {}},
    }
    html = render_script_report_html(report)
    assert '本次取数与选材条件' in html
    assert '本批详情的内容公式分布' in html
    assert '当前素材的内容公式' in html
    assert '脚本片段库' not in html
    assert '来源原句' not in html
    embedded = _model(html)
    assert [item['id'] for item in embedded['materials']] == ['1']
    assert embedded['brandTabs'][0]['ids'] == ['1']
    assert embedded['brandTabs'][0]['label'] == '全部素材'
    assert 'data-material-scope="' not in html
    assert "真实口播" in html
    assert "只在榜单" not in html
    assert "default-src 'none'" in html


def test_formula_terms_require_readable_labels_and_exact_script_match():
    formula = {
        "labels": [{"level_1_label": 9, "level_2_labels": [34]}],
        "sources": {"script": {"text": "今天有活动，欢迎来看。",
                               "detail": {"34": ["今天有活动", "并未出现在正文"]}}},
    }
    report = {
        "counts": {"ranking": 1, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "rows": [_row("1", title="<script>alert(1)</script>", script="今天有活动，欢迎来看。", formula=formula)],
    }
    html = render_script_report_html(report)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "商品卖点" in html and "功效功能" in html
    embedded = _model(html)
    groups = embedded['materials'][0]['formula']
    assert groups[0]['label'] == '商品卖点'
    assert groups[0]['children'][0]['label'] == '功效功能'
    assert groups[0]['children'][0]['terms'] == ['今天有活动']
    assert '并未出现在正文' not in html
    assert 'id="formula-chart"' in html
    assert 'id="overview-secondary"' in html
    assert 'id="metric-dialog"' in html


def test_formula_counts_materials_once_and_core_keeps_default_missing_fields():
    formula = {
        "labels": [{"level_1_label": 9, "level_2_labels": [34, 34]},
                   {"level_1_label": 9, "level_2_labels": [34]}],
        "sources": {"script": {"text": "功效功能", "detail": {"34": ["功效功能"]}}},
    }
    rows = [_row("1", title="第一条", script="功效功能", formula=formula),
            _row("2", title="第二条", script="功效功能", formula=formula)]
    html = render_script_report_html({
        "runId": "fixture", "counts": {"ranking": 2, "detail": 2, "platform": 2,
                                         "agent_audio": 0, "unavailable": 0}, "rows": rows,
    })
    overview = _formula_overview(rows)
    assert overview["coverage"]["formulaMaterialCount"] == 2
    assert overview["primaryBuckets"] == [{"label": "商品卖点", "value": 2, "share": 1.0}]
    assert overview["secondaryBucketsByPrimary"]["商品卖点"] == [{"label": "功效功能", "value": 2, "share": 1.0}]
    metrics = _model(html)['materials'][0]['metrics']
    assert sum(field['default'] for field in metrics) == 7
    assert "metrics.play_duration_avg" not in [field['id'] for field in metrics]
    assert any(field['label'] == '3S完播率' and field['value'] == '—' for field in metrics)


def test_formula_source_text_is_safe_in_embedded_model():
    formula = {
        "labels": [{"level_1_label": 9, "level_2_labels": [34]}],
        "sources": {"script": {"text": '</script><script>alert("x")</script>',
                               "detail": {"34": ["alert"]}}},
    }
    html = render_script_report_html({
        "counts": {"ranking": 1, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "rows": [_row("1", title="报价\"<&>", script="真实口播", formula=formula)],
    })
    embedded = re.search(r'<script id="workbench-model" type="application/json">(.*?)</script>', html).group(1)
    assert "</script><script>alert" not in embedded
    assert "\\u003c/script>" not in embedded
    assert json.loads(embedded)['materials'][0]['formula'][0]['children'][0]['terms'] == []


def test_brand_scope_keeps_filter_values_without_extra_explanation():
    report = {
        "counts": {"ranking": 1, "detail": 1, "platform": 0, "agent_audio": 0, "unavailable": 1},
        "selection": {"industry": "食品饮料", "brandScope": "SPECIFIED_BRANDS",
                      "brands": ["小仙炖", "燕之屋", "五个女博士"], "titleBrandUnmatchedCount": 1},
        "rows": [_row("1", title="燕窝礼盒")],
    }
    html = render_script_report_html(report)
    assert "平台品牌范围" in html and "指定品牌" in html
    assert "小仙炖、燕之屋、五个女博士" in html
    assert "1 条榜单标题未出现指定品牌名" not in html
    assert "不等于品牌归属判定" not in html


def test_scope_expands_recorded_platform_and_local_filters_without_wrapping_bars():
    report = {
        "selection": {
            "industry": "食品饮料", "targetTopN": 2,
            "listParams": {"ages": ["18-24", "25-34"], "genders": ["FEMALE"],
                           "future_filter": {"type": "verified"}, "accessToken": "private"},
            "materialList": {"filters": [
                {"field": "曝光量", "operator": "gt", "value": 9999},
                {"field": "点击率", "operator": "gte", "value": 0.02}],
                "sort": {"field": "rank", "direction": "asc"}},
        },
        "rows": [_row("1", title="第一条", script="真实口播")],
    }
    html = render_script_report_html(report)
    scope = _model(html)['scope']
    assert ["年龄筛选", "18-24、25-34"] in scope
    assert ["平台取数参数：future_filter", '{"type": "verified"}'] in scope
    assert ["本地筛选条件 1", "曝光量 > 9999"] in scope
    assert ["本地筛选条件 2", "点击率 ≥ 0.02"] in scope
    assert "private" not in html
    assert "width:max-content;min-width:100%;margin:0 auto" in html
    assert "点二级步骤可在中间完整脚本中定位已核对的原句。" not in html


def test_ranking_fields_do_not_enter_report_ui_without_details():
    row = _row("1", title="第一条", detail=False)
    row.update({"publishTime": "20260928", "videoDuration": 18, "materialTags": ["演示标签"]})
    row["metrics"].update({"play_3s_rate": 0.4, "play_over_rate": 0.2,
                           "play_duration_avg": 7, "cvr": 0.1, "pvr": 0.05,
                           "interact_rate": 0.3})
    report = {"runId": "fixture", "counts": {"ranking": 1, "detail": 0, "platform": 0,
                                             "agent_audio": 0, "unavailable": 0}, "rows": [row]}
    html = render_script_report_html(report)
    embedded = _model(html)
    assert embedded['materials'] == []
    assert 'leaderboardFields' not in embedded
    assert 'defaultFieldIds' not in embedded
    assert 'id="metric-dialog"' in html
    assert 'data-metric-choice' not in html
