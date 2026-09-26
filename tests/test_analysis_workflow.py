import json
import tempfile
from pathlib import Path

from scripts import collector
from scripts.analysis_workflow import (
    apply_fact_selection_result,
    prepare_fact_selection_handoff,
    prepare_model_analysis_handoff,
)
from scripts.fact_selection import FactPackage
from scripts.workflow_state import get_analysis_status, get_workflow_status


ROOT = Path(__file__).resolve().parents[1]
def _make_ready_run(root: Path) -> None:
    facts = root / "facts"
    facts.mkdir(parents=True)
    (root / "raw-csv").mkdir()
    (root / "raw-csv" / "list.csv").write_text("material_id\n1\n", encoding="utf-8")
    ranking_records = [
        {
            "material_id": str(index),
            "rank": index,
            "metrics": {"show_cnt": index, "cvr": (1001 - index) / 1000},
            "brand_classification": {"value": "品牌A", "matchedBrands": ["品牌A"]},
        }
        for index in range(1, 1001)
    ]
    section_records = {
        "material_ranking": ranking_records,
        "audience": [{"material_id": "1", "rank": 1, "audience": {"age": "25-30"}}],
        "script": [{"material_id": str(index), "rank": index, "script": ""} for index in range(1, 1001)],
        "highlight_frames": [{"material_id": "1", "rank": 1, "frames": []}],
    }
    packages = {}
    for section, filename in {
        "material_ranking": "material-ranking.json",
        "audience": "audience.json",
        "script": "script.json",
        "highlight_frames": "highlight-frames.json",
    }.items():
        records = section_records[section]
        (facts / filename).write_text(json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")
        packages[section] = {
            "path": f"facts/{filename}",
            "record_count": len(records),
            "material_ids": [record["material_id"] for record in records],
        }
    (facts / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": "e97b74711dbe4105bb7825038530aece",
            "packages": packages,
            "brand_classification": {"candidate_brands": [], "unknown_count": 0},
        }),
        encoding="utf-8",
    )
    (root / "run-summary.json").write_text(
        json.dumps({
            "runId": "e97b74711dbe4105bb7825038530aece",
            "workflow": {"stage": "ready_for_analysis"},
        }),
        encoding="utf-8",
    )


def _selection_result(run_id, question="找出曝光高但转化差的素材"):
    return {
        "intent": {
            "business_question": question,
            "objectives": ["find_high_exposure_low_conversion"],
            "metrics": ["show_cnt", "cvr"],
            "dimensions": ["brand"],
            "required_packages": ["material_ranking", "script"],
            "material_scope": None,
            "creative_analysis_required": False,
        },
        "selection_spec": {
            "packages": ["material_ranking", "script"],
            "filters": [
                {
                    "package": "material_ranking",
                    "field": "metrics.show_cnt",
                    "operator": "top_percentile",
                    "value": 75,
                },
                {
                    "package": "material_ranking",
                    "field": "metrics.cvr",
                    "operator": "bottom_percentile",
                    "value": 25,
                },
            ],
            "fields": {},
            "order_by": [],
            "limit": 10,
            "joins": [{"package": "script", "on": "material_id"}],
        },
        "run_id": run_id,
    }


def test_parallel_analysis_contexts_start_without_overwriting_each_other():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)

        first = prepare_fact_selection_handoff(run_copy, "分析问题 A")
        second = prepare_fact_selection_handoff(run_copy, "分析问题 B")
        first_id = first["host_context"]["analysis_id"]
        second_id = second["host_context"]["analysis_id"]

        assert first_id != second_id
        assert get_workflow_status(run_copy)["status"] == "ready_for_analysis"
        assert (run_copy / "analysis" / first_id / "analysis-input.json").is_file()
        assert (run_copy / "analysis" / second_id / "analysis-input.json").is_file()



def test_phase_two_handoffs_are_current_model_only_and_do_not_collect():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)
        request = prepare_fact_selection_handoff(
            run_copy,
            "找出曝光高但转化差的素材",
        )

        assert request["request_type"] == "yuntu_fact_selection"
        assert request["host_context"]["response_schema"] == "FactSelectionSpec"
        assert get_workflow_status(run_copy)["status"] == "ready_for_analysis"
        assert get_analysis_status(run_copy, request["host_context"]["analysis_id"])["status"] == "awaiting_fact_selection"
        assert not list(run_copy.glob("**/raw-csv/*"))[0].name == "never"


def test_apply_selection_then_prepare_analysis_uses_selected_local_facts():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)
        handoff = prepare_fact_selection_handoff(run_copy, "找出曝光高但转化差的素材")
        analysis_id = handoff["host_context"]["analysis_id"]
        selection = _selection_result("e97b74711dbe4105bb7825038530aece")

        selected = apply_fact_selection_result(run_copy, analysis_id, selection)
        request = prepare_model_analysis_handoff(run_copy, analysis_id)

        assert selected["selection_summary"]["source_record_counts"]["material_ranking"] == 1000
        assert request["request_type"] == "yuntu_commercial_analysis"
        assert request["host_context"]["facts"]
        assert request["host_context"]["fact_refs"]
        assert get_analysis_status(run_copy, analysis_id)["status"] == "facts_selected"
        assert (run_copy / "analysis" / analysis_id / "fact-selection-spec.json").is_file()
        assert (run_copy / "analysis" / analysis_id / "selected-fact-context.json").is_file()


def test_apply_selection_validates_model_intent_against_original_question():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)
        handoff = prepare_fact_selection_handoff(run_copy, "找出曝光高但转化差的素材")
        payload = _selection_result("e97b74711dbe4105bb7825038530aece")
        payload["intent"]["business_question"] = "比较三个品牌"

        try:
            apply_fact_selection_result(run_copy, handoff["host_context"]["analysis_id"], payload)
        except ValueError as exc:
            assert "business_question" in str(exc)
        else:
            raise AssertionError("mismatched intent should be rejected")


def test_workflow_cli_exposes_the_two_phase_local_fact_flow():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)
        selection_path = run_copy / "selection-result.json"
        selection_path.write_text(json.dumps(_selection_result("e97b74711dbe4105bb7825038530aece")), encoding="utf-8")

        first_status = collector.main([
            "workflow", "--run-dir", str(run_copy),
            "--business-question", "找出曝光高但转化差的素材",
        ])
        assert first_status == 0
        analysis_id = next((run_copy / "analysis").iterdir()).name
        output = run_copy / "analysis" / analysis_id / "workflow.md"
        assert json.loads((run_copy / "analysis" / analysis_id / "fact-selection-request.json").read_text(encoding="utf-8"))["request_type"] == "yuntu_fact_selection"

        second_status = collector.main([
            "workflow", "--run-dir", str(run_copy),
            "--business-question", "找出曝光高但转化差的素材",
            "--analysis-id", analysis_id,
            "--fact-selection-result", str(selection_path),
            "--output", str(output),
        ])
        assert second_status == 0
        assert json.loads((run_copy / "analysis" / analysis_id / "model-analysis-request.json").read_text(encoding="utf-8"))["request_type"] == "yuntu_commercial_analysis"


def test_workflow_cli_replays_analysis_result_and_renders_selected_context():
    with tempfile.TemporaryDirectory() as directory:
        run_copy = Path(directory) / "run"
        _make_ready_run(run_copy)
        selection_path = run_copy / "selection-result.json"
        selection_path.write_text(json.dumps(_selection_result("e97b74711dbe4105bb7825038530aece")), encoding="utf-8")
        analysis_path = run_copy / "analysis-result.json"
        analysis_path.write_text(json.dumps({
            "results": [
                {
                    "section": "material_ranking",
                    "findings": [{
                        "claim": "高曝光素材中存在转化效率较低的样本",
                        "type": "inference",
                        "confidence": "medium",
                        "fact_refs": ["facts/material-ranking.json#/records/0"],
                    }],
                },
                {
                    "section": "script",
                    "findings": [],
                    "gaps": ["脚本字段为 source_empty，无法分析脚本钩子"],
                },
            ]
        }, ensure_ascii=False), encoding="utf-8")

        assert collector.main([
            "workflow", "--run-dir", str(run_copy),
            "--business-question", "找出曝光高但转化差的素材",
        ]) == 0
        analysis_id = next((run_copy / "analysis").iterdir()).name
        output = run_copy / "analysis" / analysis_id / "workflow.md"
        assert collector.main([
            "workflow", "--run-dir", str(run_copy),
            "--business-question", "找出曝光高但转化差的素材",
            "--analysis-id", analysis_id,
            "--fact-selection-result", str(selection_path),
            "--output", str(output),
        ]) == 0
        selected_context = json.loads(
            (run_copy / "analysis" / analysis_id / "selected-fact-context.json").read_text(encoding="utf-8")
        )
        first_index = selected_context["sections"]["material_ranking"]["record_indices"][0]
        analysis_payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis_payload["results"][0]["findings"][0]["fact_refs"] = [
            f"facts/material-ranking.json#/records/{first_index}"
        ]
        analysis_path.write_text(json.dumps(analysis_payload, ensure_ascii=False), encoding="utf-8")
        assert collector.main([
            "workflow", "--run-dir", str(run_copy),
            "--business-question", "找出曝光高但转化差的素材",
            "--analysis-id", analysis_id,
            "--analysis-result", str(analysis_path),
            "--output", str(output),
        ]) == 0

        report = output.read_text(encoding="utf-8")
        assert "用户商业问题" in report
        assert "使用事实：素材榜单、脚本分析" in report
        assert "1000" in report
        assert "源数据为空" in report
        assert "source_empty" not in report
        assert get_workflow_status(run_copy)["status"] == "ready_for_analysis"
        assert get_analysis_status(run_copy, analysis_id)["status"] == "analysis_completed"
