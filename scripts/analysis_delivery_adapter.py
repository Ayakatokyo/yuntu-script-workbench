"""Yuntu adapters for verified analysis paging and HTML delivery."""
from pathlib import Path

try:
    from .analysis_delivery import read, sha, prepare_pages, verify_pages, render_delivery, verify_delivery, timed_phase
    from .analysis_result import validate_analysis_result, AnalysisResultValidationError, _field_value
    from .analysis_facts import build_fact_index, execute_fact_selection
    from .fact_selection import FactSelectionSpec
except ImportError:
    from analysis_delivery import read, sha, prepare_pages, verify_pages, render_delivery, verify_delivery, timed_phase
    from analysis_result import validate_analysis_result, AnalysisResultValidationError, _field_value
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


@timed_phase("render_analysis_report")
def render_analysis_delivery(root):
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
    cards, gaps = [], []
    labels = {"fact": "事实", "inference": "推断", "recommendation": "待验证建议"}
    for section in result["results"]:
        errors = validate_analysis_result(section, facts)
        if errors:
            raise AnalysisResultValidationError(errors)
        gaps.extend(section.get("gaps", []))
        for finding in section["findings"]:
            evidence = []
            for ref in finding["fact_refs"]:
                record = facts[ref]
                for field in finding.get("field_refs", {}).get(ref, list(record)):
                    evidence.append({"material": record.get("material_name") or record.get("title") or record.get("material_id") or "所选事实",
                                     "field": {"script_text": "脚本正文", "metrics.show_cnt": "曝光次数", "metrics.cvr": "转化率"}.get(field, field), "value": _field_value(record, field), "isMetric": field.startswith("metrics."), "fact_ref": ref})
            cards.append({"claim": finding["claim"], "type": labels[finding["type"]], "evidence": evidence})
    return render_delivery(root, "云图脚本分析", index["identity"], cards, gaps + ["指标关联不代表脚本写法导致投放效果。"],
                           [str(result_path.relative_to(root)), "handoff-index.json", "selected-fact-context.json"])


def analysis_delivery_status(root):
    root = Path(root).resolve()
    try:
        from .workflow_state import get_analysis_status
    except ImportError:
        from workflow_state import get_analysis_status
    status = get_analysis_status(root.parent.parent, root.name)
    index = verified_context(root)[0] if (root / "handoff-index.json").exists() else None
    report = verify_delivery(root) if (root / "analysis-report/receipt.json").exists() else None
    return {"status": status["stage"], "analysisId": root.name, "handoff": "verified" if index else "not_generated",
            "pageCount": len(index["pages"]) if index else None, "deliveryStatus": "completed" if report else "not_generated",
            "reportVerified": bool(report), "nextAction": "deliver_report" if report else ("render-analysis-report" if status["stage"] == "analysis_completed" else "continue_workflow")}
