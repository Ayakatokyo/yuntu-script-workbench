"""The offline report must stay faithful to its source facts and fixed layout."""

import json
import re

from scripts.script_report_html import render_script_report_html


def _row(material_id, *, title, script="", formula=None, detail=True):
    return {
        "materialId": material_id, "rank": int(material_id), "title": title,
        "metrics": {"show_cnt": int(material_id) * 100, "ctr": 0.2},
        "scriptSource": "platform" if script else "unavailable",
        "scriptText": script, "platformScriptText": script, "audioTranscript": None,
        "contentFormula": formula, "creativeBreakdown": None,
        "sourcePaths": {"normalized": "detail.json"} if detail else {},
    }


def test_fixed_five_sections_and_full_leaderboard_with_sparse_details():
    report = {
        "counts": {"ranking": 2, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "selection": {"industry": "食品饮料", "targetTopN": 1,
                      "materialList": {"filters": [], "sort": {"field": "rank", "direction": "asc"}}},
        "rows": [
            _row("1", title="第一条", script="真实口播"),
            _row("2", title="只在榜单", detail=False),
        ],
    }
    html = render_script_report_html(report)
    for section in ("素材筛选条件", "素材榜单", "脚本内容公式", "单素材脚本", "脚本片段库"):
        assert html.count(">" + section + "<") >= 1
    assert html.count("data-row=") == 2
    assert html.count("data-card=") == 1
    assert 'data-open="0"' in html
    assert 'data-open="1"' not in html
    assert "真实口播" in html
    assert "只在榜单" in html
    assert "default-src &#39;none&#39;" in html


def test_formula_and_quote_require_readable_labels_and_exact_script_match():
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
    assert html.count('data-quote>') == 1
    assert "<blockquote>并未出现在正文</blockquote>" not in html
    assert 'id="script-formula-chart"' in html
    assert 'id="script-formula-secondary-chart"' in html
    assert 'id="script-formula-secondary-legend"' in html
    assert 'id="single-core-field-management"' in html
    assert 'data-single-formula' in html


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
    payload = json.loads(re.search(r'<script id="report-ui-data" type="application/json">(.*?)</script>', html).group(1))
    overview = payload["scriptFormulaOverview"]
    assert overview["coverage"]["formulaMaterialCount"] == 2
    assert overview["primaryBuckets"] == [{"label": "商品卖点", "value": 2, "share": 1.0}]
    assert overview["secondaryBucketsByPrimary"]["商品卖点"] == [{"label": "功效功能", "value": 2, "share": 1.0}]
    core = payload["singleModels"]["0"]["core"]
    assert len(core["defaultFieldIds"]) == 7
    assert "metrics.play_duration_avg" not in [field["id"] for field in core["fields"]]
    assert "3S完播率" in html and "—" in html


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
    embedded = re.search(r'<script id="report-ui-data" type="application/json">(.*?)</script>', html).group(1)
    assert "</script><script>alert" not in embedded
    assert "\\u003c/script>" in embedded
    assert json.loads(embedded)["singleModels"]["0"]["formula"]["sources"]["script"]["text"].startswith("</script>")


def test_brand_scope_is_disclosed_without_treating_title_match_as_brand_truth():
    report = {
        "counts": {"ranking": 1, "detail": 1, "platform": 0, "agent_audio": 0, "unavailable": 1},
        "selection": {"industry": "食品饮料", "brandScope": "SPECIFIED_BRANDS",
                      "brands": ["小仙炖", "燕之屋", "五个女博士"], "titleBrandUnmatchedCount": 1},
        "rows": [_row("1", title="燕窝礼盒")],
    }
    html = render_script_report_html(report)
    assert "平台品牌范围" in html and "指定品牌" in html
    assert "小仙炖、燕之屋、五个女博士" in html
    assert "1 条榜单标题未出现指定品牌名" in html
    assert "不等于品牌归属判定" in html
