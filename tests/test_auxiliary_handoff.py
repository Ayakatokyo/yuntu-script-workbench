import json
import pytest

from test_analysis_workflow import _make_ready_run
from scripts import collector
from scripts.analysis_workflow import prepare_fact_selection_handoff, apply_fact_selection_result, prepare_model_analysis_handoff
from scripts.analysis_delivery import read, read_page
from scripts.analysis_delivery_adapter import analysis_delivery_status
from scripts.workflow_state import analysis_context_dir


@pytest.mark.parametrize("packages", [["content_tags"], ["interaction_series"], ["content_tags", "interaction_series"]])
def test_selected_auxiliary_facts_reach_request_pages_template_and_publication(tmp_path, packages):
    run = tmp_path / "run"
    _make_ready_run(run)
    manifest = read(run / "facts/manifest.json")
    filenames = {"content_tags": "content-tags.json", "interaction_series": "interaction-series.json"}
    fields = {"content_tags": "platform_content_tags", "interaction_series": "trend_series"}
    records = {
        "content_tags": {"material_id": "1", "platform_content_tags": ["使用场景_早餐"]},
        "interaction_series": {"material_id": "1", "trend_series": [{"trend_name": "点赞指数", "points": [{"second": 1, "value": 0}]}]},
    }
    for package, filename in filenames.items():
        (run / "facts" / filename).write_text(json.dumps({"schema_version": 1, "records": [records[package]]}))
        manifest["packages"][package] = {"path": "facts/" + filename, "record_count": 1, "material_ids": ["1"]}
    (run / "facts/manifest.json").write_text(json.dumps(manifest))
    question = "核对内容标签和互动时序"
    first = prepare_fact_selection_handoff(run, question)
    aid = first["host_context"]["analysis_id"]
    intent = {"business_question": question, "objectives": ["analyze_creative_direction"],
              "metrics": [], "dimensions": [], "required_packages": packages,
              "material_scope": None, "creative_analysis_required": False}
    apply_fact_selection_result(run, aid, {"intent": intent, "run_id": manifest["run_id"],
        "selection_spec": {"packages": packages, "fields": {p: [fields[p]] for p in packages},
                           "filters": [], "order_by": [], "joins": [], "limit": 10}})
    request = prepare_model_analysis_handoff(run, aid)
    root = analysis_context_dir(run, aid)
    expected_paths = {"facts/" + filenames[p] for p in packages}
    assert set(request["host_context"]["facts"]) == expected_paths
    assert set(request["host_context"]["analysis_spec"]["sections"]) == set(packages)
    template = read(root / "result-template.json")
    assert {r["section"] for r in template["results"]} == set(packages)
    rows = read_page(root, 1)["records"]
    results = []
    for package in packages:
        row = next(r for r in rows if r["fact_ref"].startswith("facts/" + filenames[package]))
        assert row["record"][fields[package]] == records[package][fields[package]]
        results.append({"section": package, "findings": [{"claim": "已核对所选平台数据。", "type": "fact", "confidence": "high",
            "fact_refs": [row["fact_ref"]], "field_refs": {row["fact_ref"]: [fields[package]]}}]})
    path = tmp_path / "result.json"
    payload = {"_handoff": template["_handoff"], "results": results}
    path.write_text(json.dumps(payload))
    assert collector.main(["workflow", "--run-dir", str(run), "--analysis-id", aid, "--analysis-result", str(path)]) == 0
    assert analysis_delivery_status(root)["deliveryStatus"] == "awaiting_host_report"
    # A completed result still rejects a changed source projection.
    source = run / "facts" / filenames[packages[0]]
    source.write_text(json.dumps({"schema_version": 1, "records": []}))
    with pytest.raises(ValueError):
        analysis_delivery_status(root)
