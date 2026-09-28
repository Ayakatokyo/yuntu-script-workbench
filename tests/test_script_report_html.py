"""The offline report must stay faithful to its source facts and fixed layout."""

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
        "labels": [{"level_1_label": {"id": 1, "label": "开场"},
                    "level_2_labels": [{"id": 2, "label": "提问"}]}],
        "sources": {"script": {"detail": {"2": ["今天有活动", "并未出现在正文"]}}},
    }
    report = {
        "counts": {"ranking": 1, "detail": 1, "platform": 1, "agent_audio": 0, "unavailable": 0},
        "rows": [_row("1", title="<script>alert(1)</script>", script="今天有活动，欢迎来看。", formula=formula)],
    }
    html = render_script_report_html(report)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "开场" in html and "提问" in html
    assert html.count('data-quote>') == 1
    assert "并未出现在正文" not in html
