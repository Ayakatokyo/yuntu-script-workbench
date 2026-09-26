"""Phase-two local fact selection and current-model handoff orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .analysis_facts import build_fact_index, execute_fact_selection
    from .analysis_intent import parse_analysis_intent
    from .analysis_input import parse_analysis_input, validate_analysis_input
    from .analysis_spec import AnalysisSpec
    from .fact_catalog import build_fact_catalog
    from .fact_selection import FactSelectionSpec
    from .model_analysis_request import build_fact_selection_request, build_model_analysis_request
    from .workflow_state import (
        analysis_context_dir,
        begin_fact_selection,
        complete_fact_selection,
        get_workflow_status,
    )
except ImportError:  # pragma: no cover - direct script imports
    from analysis_facts import build_fact_index, execute_fact_selection
    from analysis_intent import parse_analysis_intent
    from analysis_input import parse_analysis_input, validate_analysis_input
    from analysis_spec import AnalysisSpec
    from fact_catalog import build_fact_catalog
    from fact_selection import FactSelectionSpec
    from model_analysis_request import build_fact_selection_request, build_model_analysis_request
    from workflow_state import (
        analysis_context_dir,
        begin_fact_selection,
        complete_fact_selection,
        get_workflow_status,
    )


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")


def _manifest(run_dir: Path) -> dict[str, Any]:
    payload = _read(Path(run_dir) / "facts" / "manifest.json")
    if not isinstance(payload, dict):
        raise ValueError("facts manifest must be an object")
    return payload


def _spec_for_question(run_dir: Path, business_question: str) -> AnalysisSpec:
    root = Path(run_dir)
    manifest = _manifest(root)
    status = get_workflow_status(root)
    spec = parse_analysis_input(business_question, status, manifest)
    errors = validate_analysis_input(spec, status, manifest)
    if errors:
        raise ValueError("invalid analysis input: " + "; ".join(errors))
    return spec


def prepare_fact_selection_handoff(run_dir: Path, business_question: str) -> dict[str, Any]:
    """Persist the first phase-two handoff for the current conversation model."""
    root = Path(run_dir)
    spec = _spec_for_question(root, business_question)
    catalog = build_fact_catalog(root)
    request = build_fact_selection_request(spec, catalog)
    begin_fact_selection(root, spec.analysis_id)
    context = analysis_context_dir(root, spec.analysis_id)
    _write(context / "analysis-input.json", spec.to_dict())
    _write(context / "fact-selection-request.json", request)
    return request


def _selection_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, list[Any]]:
    if not isinstance(payload, dict):
        raise ValueError("fact selection result must be an object")
    intent = payload.get("intent")
    selection = payload.get("selection_spec")
    run_id = payload.get("run_id")
    if not isinstance(intent, dict):
        raise ValueError("fact selection result intent must be an object")
    if not isinstance(selection, dict):
        raise ValueError("fact selection result selection_spec must be an object")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("fact selection result run_id is required")
    data_gaps = payload.get("data_gaps", [])
    if not isinstance(data_gaps, list):
        raise ValueError("fact selection result data_gaps must be an array")
    return intent, selection, run_id.strip(), data_gaps


def apply_fact_selection_result(
    run_dir: Path, analysis_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate and execute an AI selection plan against local persisted facts."""
    root = Path(run_dir)
    intent, raw_selection, result_run_id, model_gaps = _selection_payload(payload)
    manifest = _manifest(root)
    expected_run_id = manifest.get("run_id") or manifest.get("runId")
    if result_run_id != expected_run_id:
        raise ValueError(f"selection result run_id does not match facts manifest: {result_run_id} != {expected_run_id}")
    selection = FactSelectionSpec.from_dict(raw_selection)
    context = analysis_context_dir(root, analysis_id)
    input_payload = _read(context / "analysis-input.json")
    if input_payload.get("analysis_id") != analysis_id:
        raise ValueError("analysis context does not match analysis_id")
    original_question = input_payload.get("business_question") if isinstance(input_payload, dict) else None
    parsed_intent = parse_analysis_intent(intent)
    if parsed_intent.business_question != original_question:
        raise ValueError("fact selection intent business_question must match the original question")
    available_packages = set(manifest.get("packages", {}))
    unsupported_required = sorted(
        package.value for package in parsed_intent.required_packages
        if package.value not in available_packages
    )
    if unsupported_required:
        raise ValueError("fact selection intent requires unavailable packages: " + ", ".join(unsupported_required))
    result = execute_fact_selection(root, selection)
    combined_gaps = list(result.data_gaps)
    for gap in model_gaps:
        if isinstance(gap, str) and gap.strip():
            combined_gaps.append(gap.strip())
        elif isinstance(gap, dict):
            combined_gaps.append(dict(gap))
    serializable = {
        "sections": result.sections,
        "selection_summary": result.selection_summary,
        "fact_refs": list(result.fact_refs),
        "aggregates": result.aggregates or {},
        "data_gaps": combined_gaps,
        "validation_errors": list(result.validation_errors),
    }
    _write(context / "analysis-intent.json", intent)
    _write(context / "fact-selection-spec.json", selection.to_dict())
    context_path = context / "selected-fact-context.json"
    _write(context_path, serializable)
    complete_fact_selection(root, analysis_id, context_path)
    return serializable


def prepare_model_analysis_handoff(run_dir: Path, analysis_id: str) -> dict[str, Any]:
    """Build the final current-model request from the locally selected facts."""
    root = Path(run_dir)
    manifest = _manifest(root)
    context = analysis_context_dir(root, analysis_id)
    input_payload = _read(context / "analysis-input.json")
    if input_payload.get("analysis_id") != analysis_id:
        raise ValueError("analysis context does not match analysis_id")
    status = get_workflow_status(root)
    spec = parse_analysis_input(input_payload, status, manifest)
    selected_context = _read(context / "selected-fact-context.json")
    if not isinstance(selected_context, dict) or not isinstance(selected_context.get("sections"), dict):
        raise ValueError("selected fact context is missing or invalid")
    selected = {
        "manifest": manifest,
        "sections": selected_context["sections"],
        "aggregates": selected_context.get("aggregates", {}),
        "data_gaps": selected_context.get("data_gaps", []),
    }
    fact_index = build_fact_index(selected)
    intent_path = context / "analysis-intent.json"
    standardized_intent = _read(intent_path) if intent_path.is_file() else None
    catalog = build_fact_catalog(root)
    request = build_model_analysis_request(
        spec,
        selected,
        fact_index,
        standardized_intent=standardized_intent,
        capability_snapshot=catalog.to_dict(),
    )
    _write(context / "model-analysis-request.json", request)
    _write(context / "prompt-packet.json", request["host_context"].get("facts", {}))
    return request


__all__ = [
    "apply_fact_selection_result",
    "prepare_fact_selection_handoff",
    "prepare_model_analysis_handoff",
]
