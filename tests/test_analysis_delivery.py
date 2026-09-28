import json
from pathlib import Path
from unittest.mock import patch

import pytest

from test_analysis_workflow import _make_ready_run, _selection_result
from scripts import collector
from scripts.analysis_workflow import prepare_fact_selection_handoff, apply_fact_selection_result, prepare_model_analysis_handoff
from scripts.analysis_delivery import read, read_page, verify_delivery
from scripts.analysis_delivery_adapter import analysis_delivery_status, render_analysis_delivery
from scripts.workflow_state import analysis_context_dir, get_analysis_status


def prepared(tmp_path):
    run = tmp_path / "run"
    _make_ready_run(run)
    request = prepare_fact_selection_handoff(run, "找出曝光高但转化差的素材")
    aid = request["host_context"]["analysis_id"]
    apply_fact_selection_result(run, aid, _selection_result(request["host_context"]["run_id"]))
    prepare_model_analysis_handoff(run, aid)
    return run, analysis_context_dir(run, aid)


def test_pages_preserve_original_indices_and_reject_source_changes(tmp_path):
    run, root = prepared(tmp_path)
    index = read(root / "handoff-index.json")
    context = read(root / "selected-fact-context.json")
    rows = [row for n in range(1, len(index["pages"])+1) for row in read_page(root, n)["records"]]
    expected = {section["path"] + "#/records/" + str(i): r for section in context["sections"].values()
                for i, r in zip(section["record_indices"], section["records"])}
    assert {r["fact_ref"]: r["record"] for r in rows} == expected
    assert not analysis_delivery_status(root)["reportVerified"]
    path = run / "facts/material-ranking.json"
    facts = read(path)
    for r in facts["records"]:
        r["metrics"]["show_cnt"] += 10
    path.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match="differ|SHA-256"):
        analysis_delivery_status(root)


def test_workflow_report_is_verified_recoverable_and_bound(tmp_path):
    run, root = prepared(tmp_path)
    first = read_page(root, 1)["records"][0]
    result = {"section": "material_ranking", "findings": [{"claim": "原样值<script>zero</script>", "type": "fact", "confidence": "high",
              "fact_refs": [first["fact_ref"]], "field_refs": {first["fact_ref"]: ["metrics.show_cnt"]}}]}
    result_path = tmp_path / "result.json"
    payload = {"_handoff": read(root / "result-template.json")["_handoff"],
               "results": [result, {"section": "script", "findings": [], "gaps": ["无可读正文"]}]}
    args = ["workflow", "--run-dir", str(run), "--analysis-id", root.name,
            "--business-question", "找出曝光高但转化差的素材", "--analysis-result", str(result_path)]
    wrong = json.loads(json.dumps(payload))
    wrong["_handoff"]["identity"]["analysisId"] = "other-analysis"
    result_path.write_text(json.dumps(wrong))
    with pytest.raises(SystemExit):
        collector.main(args)
    assert not (root / "analysis-report/receipt.json").exists()
    result_path.write_text(json.dumps(payload))
    assert collector.main(args) == 0
    assert analysis_delivery_status(root)["reportVerified"]
    report = root / "analysis-report/index.html"
    original = report.read_text()
    assert "<script>" not in original and "&lt;script&gt;" in original
    report.write_text("damaged")
    with pytest.raises(ValueError):
        verify_delivery(root)
    render_analysis_delivery(root)
    assert report.read_text() == original
    status = get_analysis_status(run, root.name)
    path = Path(status["state"]["metadata"]["result_path"])
    payload = read(path)
    payload["results"][0]["findings"][0]["claim"] = "changed but otherwise valid"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="hash mismatch"):
        render_analysis_delivery(root)


def test_remote_failure_keeps_cause_and_never_resubmits():
    failure = {"status": "failed", "errors": [{"code": "rpa_shop_not_authorized", "message": "account unavailable token=secret-value https://private.invalid?token=secret-value"}]}
    with patch.object(collector, "query_fetch_task", return_value=failure) as query, patch.object(collector, "submit_fetch_task") as submit, patch.object(collector.time, "sleep"):
        with pytest.raises(collector.RpaConnectorError) as caught:
            collector.wait_fetch_task("task-1")
    details = caught.value.failure_details
    assert details["taskGroupId"] == "task-1"
    assert details["errors"][0]["code"] == "rpa_shop_not_authorized"
    assert "secret-value" not in str(caught.value) and "private.invalid" not in str(caught.value)
    assert not details["retrySubmitted"]
    assert query.call_count == 1 and submit.call_count == 0


def test_list_failure_persists_structured_cause_in_both_receipts(tmp_path):
    from test_yuntu_batch import load_batch
    batch = load_batch()
    class FailedInvoker:
        def invoke_list(self, *_args, **_kwargs):
            raise collector.RpaConnectorError("RPA task failed", task_group_id="task-list", errors=[{"code": "rpa_shop_not_authorized", "message": "账号未授权"}])
    root = tmp_path / "failed-run"
    assert batch.fetch_batch(shop_id="shop", list_params={}, detail_date_params={"date_range_type": "LAST_7_DAYS"},
                             target_top_n=1, candidate_top_n=1000, output_dir=root, invoker=FailedInvoker()) == 1
    manifest, summary = read(root / "batch-manifest.json"), read(root / "run-summary.json")
    assert manifest["status"] == "list_failed"
    assert manifest["failureDetails"] == summary["stages"]["list"]["failureDetails"]
    assert manifest["failureDetails"]["errors"][0]["code"] == "rpa_shop_not_authorized"
