"""Yuntu adapters for verified analysis paging and host report handoff."""
from pathlib import Path

try:
    from .analysis_delivery import read, sha, write, prepare_pages, verify_pages, register_host_report, verify_delivery, has_report_receipt, timed_phase
    from .analysis_result import validate_analysis_result, AnalysisResultValidationError
    from .analysis_facts import build_fact_index, execute_fact_selection
    from .fact_selection import FactSelectionSpec
except ImportError:
    from analysis_delivery import read, sha, write, prepare_pages, verify_pages, register_host_report, verify_delivery, has_report_receipt, timed_phase
    from analysis_result import validate_analysis_result, AnalysisResultValidationError
    from analysis_facts import build_fact_index, execute_fact_selection
    from fact_selection import FactSelectionSpec


def prepare_delivery(root, request, fact_index, gaps):
    host = request["host_context"]
    # Blank claim deliberately fails the existing validator until the model fills it.
    sections = read(Path(root) / "selected-fact-context.json")["sections"]
    sources = ["analysis-input.json", "fact-selection-spec.json", "selected-fact-context.json", "analysis-intent.json"]
    template = {"_handoff": {"identity": {"runId": host["run_id"], "analysisId": host["analysis_id"]},
                             "inputHashes": {name: sha(Path(root) / name) for name in sources}},
                "results": [{"section": section,
                             "findings": [{"claim": "", "type": "fact", "confidence": "low", "fact_refs": [], "field_refs": {}, "quotes": []}],
                             "gaps": [], "next_questions": []} for section in sections]}
    return prepare_pages(root, {"runId": host["run_id"], "analysisId": host["analysis_id"]},
                         [{"fact_ref": ref, "record": record} for ref, record in fact_index.items()],
                         sources, template, gaps)


def verified_context(root):
    root = Path(root).resolve()
    index = verify_pages(root)
    selected = read(root / "selected-fact-context.json")
    # Re-execute selection locally to validate source hashes and the saved projection.
    selection = FactSelectionSpec.from_dict(read(root / "fact-selection-spec.json"))
    current = execute_fact_selection(root.parent.parent, selection)
    if current.sections != selected["sections"]:
        raise ValueError("selected analysis facts differ from verified source packages")
    identity = read(root / "analysis-input.json")
    if index["identity"] != {"runId": identity["run_id"], "analysisId": identity["analysis_id"]} or identity["analysis_id"] != root.name:
        raise ValueError("analysis handoff identity mismatch")
    return index, build_fact_index(selected)


def _completed_result(root):
    root = Path(root).resolve()
    index, facts = verified_context(root)
    # Result path is persisted by the workflow completion gate, including custom output names.
    try:
        from .workflow_state import get_analysis_status
    except ImportError:
        from workflow_state import get_analysis_status
    status = get_analysis_status(root.parent.parent, root.name)
    if status["stage"] != "analysis_completed":
        raise ValueError("analysis result is not completed")
    result_path = Path(status["state"]["metadata"]["result_path"]).resolve()
    if not result_path.is_relative_to(root):
        raise ValueError("analysis result outside context")
    result = read(result_path)
    if not status["state"]["metadata"].get("result_sha256") or sha(result_path) != status["state"]["metadata"]["result_sha256"]:
        raise ValueError("completed analysis result hash mismatch or legacy result requires a new analysis")
    if result.get("analysis_id") != root.name or result.get("run_id") != index["identity"]["runId"]:
        raise ValueError("analysis result identity mismatch")
    for section in result["results"]:
        errors = validate_analysis_result(section, facts)
        if errors:
            raise AnalysisResultValidationError(errors)
    return index, result_path, result


def prepare_host_report_handoff(root):
    root = Path(root).resolve()
    index, result_path, _result = _completed_result(root)
    selected = root / "selected-fact-context.json"
    input_path = root / "analysis-input.json"
    handoff_path = root / "host-report-handoff.json"
    write(handoff_path, {"schemaVersion": 1, "identity": index["identity"],
                         "businessQuestion": read(input_path)["business_question"],
                         "analysisResult": {"path": str(result_path.relative_to(root)), "sha256": sha(result_path)},
                         "selectedFacts": {"path": selected.name, "sha256": sha(selected)},
                         "factPages": {"path": "handoff-index.json", "sha256": sha(root / "handoff-index.json")},
                         "guidance": "宿主用已核验事实和业务问题独立组织回答；results 是审计辅助，不限制报告章节与结论数。新增事实通过现有事实选择和读取路径核对，不重新采集。报告可选 HTML、Markdown 或纯文本，保留引用与缺口。"})
    return {"status": "analysis_completed", "deliveryStatus": "awaiting_host_report", "reportVerified": False,
            "nextAction": "create_and_register_host_report", "handoffPath": str(handoff_path)}


@timed_phase("register_host_report")
def register_analysis_report(root, revision=None, report_format=None):
    root = Path(root).resolve()
    index, result_path, _result = _completed_result(root)
    handoff = read(root / "host-report-handoff.json")
    if handoff.get("identity") != index["identity"]:
        raise ValueError("host report handoff identity mismatch")
    for key in ("analysisResult", "selectedFacts", "factPages"):
        source = handoff[key]
        if sha(root / source["path"]) != source["sha256"]:
            raise ValueError("host report handoff source hash mismatch: " + key)
    return register_host_report(root, index["identity"],
                                ["host-report-handoff.json", str(result_path.relative_to(root)),
                                 "handoff-index.json", "selected-fact-context.json", "analysis-input.json"],
                                revision=revision, report_format=report_format)


def analysis_delivery_status(root):
    root = Path(root).resolve()
    try:
        from .workflow_state import get_analysis_status
    except ImportError:
        from workflow_state import get_analysis_status
    status = get_analysis_status(root.parent.parent, root.name)
    if status["stage"] == "analysis_completed" and (root / "handoff-index.json").exists():
        index = _completed_result(root)[0]
    else:
        index = verified_context(root)[0] if (root / "handoff-index.json").exists() else None
    report = verify_delivery(root) if has_report_receipt(root) else None
    return {"status": status["stage"], "analysisId": root.name, "handoff": "verified" if index else "not_generated",
            "pageCount": len(index["pages"]) if index else None,
            "deliveryStatus": ("legacy_completed" if report.get("schemaVersion") == 1 else "completed") if report else ("awaiting_host_report" if status["stage"] == "analysis_completed" else "not_generated"),
            "reportVerified": bool(report), "reportPath": report.get("reportPath") if report else None,
            "reportRevisions": report.get("revisionHistory", []) if report else [],
            "nextAction": "deliver_report" if report else ("create_and_register_host_report" if status["stage"] == "analysis_completed" else "continue_workflow")}
