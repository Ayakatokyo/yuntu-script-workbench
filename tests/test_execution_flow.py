import json
from pathlib import Path
from unittest.mock import patch

import pytest

from test_analysis_delivery import prepared
from scripts import collector, run
from scripts.analysis_delivery import read, read_page, verify_pages
from scripts.fact_catalog import describe_analysis


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_result_preflight_is_read_only_and_uses_publish_gates(tmp_path):
    source, root = prepared(tmp_path)
    first = read_page(root, 1)["records"][0]
    finding = {"claim": "真实曝光指标", "type": "fact", "confidence": "high",
               "fact_refs": [first["fact_ref"]], "field_refs": {first["fact_ref"]: ["metrics.show_cnt"]}}
    payload = {"_handoff": read(root / "result-template.json")["_handoff"],
               "results": [{"section": "material_ranking", "findings": [finding]},
                           {"section": "script", "findings": [], "gaps": ["缺正文"]}]}
    result = tmp_path / "result.json"
    result.write_text(json.dumps(payload))
    before = snapshot(source)
    assert run.main(["check-analysis-result", "--analysis-root", str(root), "--result", str(result)]) == 0
    assert snapshot(source) == before
    finding["field_refs"][first["fact_ref"]] = ["metrics.missing"]
    result.write_text(json.dumps(payload))
    assert run.main(["check-analysis-result", "--analysis-root", str(root), "--result", str(result)]) == 2
    assert snapshot(source) == before
    finding["field_refs"][first["fact_ref"]] = ["metrics.show_cnt"]
    result.write_text(json.dumps(payload))
    # Resuming does not need the repeated question; the saved identity stays bound.
    assert collector.main(["workflow", "--run-dir", str(source), "--analysis-id", root.name,
                           "--analysis-result", str(result)]) == 0


def test_resume_question_mismatch_is_rejected_before_mutation(tmp_path):
    source, root = prepared(tmp_path)
    before = snapshot(source)
    with pytest.raises(SystemExit):
        collector.main(["workflow", "--run-dir", str(source), "--analysis-id", root.name,
                        "--business-question", "另一个问题"])
    assert snapshot(source) == before


def test_field_discovery_and_page_range_do_not_change_inputs(tmp_path):
    source, root = prepared(tmp_path)
    before = snapshot(source)
    fields = describe_analysis(source, ["material_ranking"], ["metrics.show_cnt"])
    assert fields["packages"]["material_ranking"][0]["valueTypes"] == ["number"]
    assert snapshot(source) == before
    with patch("scripts.analysis_delivery.verify_pages", wraps=verify_pages) as verify:
        pages = read_page(root, 1, 2)
    assert verify.call_count == 1
    assert len(pages["pages"]) == 2
    assert pages["pages"][0] == {k: v for k, v in read_page(root, 1).items() if k not in {"pageCount", "nextPage"}}
    (root / "fact-pages/0002.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_page(root, 1, 1)


def test_status_summary_omits_task_payload_and_retains_pause(tmp_path, capsys):
    payload = {"status": "partial", "collection_status": "partial", "rpa_paused": True,
               "firstBatchCsvGate": {"status": "unconfirmed"}, "taskRuns": {"payload": "large"},
               "stages": {"details": {"persistedRecordCount": 39}}, "evidencePaths": {"batchManifest": "batch-manifest.json"}}
    with patch.object(collector, "collection_status", return_value=payload), patch.object(collector, "build_ready_receipt") as ready:
        assert collector.main(["status", "--output-dir", str(tmp_path), "--summary"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "taskRuns" not in result and result["rpa_paused"]
    assert result["firstBatchCsvGate"]["status"] == "unconfirmed"
    assert result["evidencePaths"]["batchManifest"] == "batch-manifest.json"
    assert not ready.called
