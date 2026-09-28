"""Two-phase workflow state and ready-receipt helpers for Yuntu runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

try:
    from .rpa_io import write_json_atomically
    from .yuntu_run_state import load_run_state, save_run_state, sha256_file
except ImportError:  # pragma: no cover - direct script imports
    from rpa_io import write_json_atomically
    from yuntu_run_state import load_run_state, save_run_state, sha256_file


WORKFLOW_PROTOCOL_VERSION = "yuntu-two-phase-v2"
RUN_SUMMARY_NAME = "run-summary.json"
COLLECTION_STAGES = (
    "collecting",
    "normalizing",
    "fact_packaging",
    "brand_classifying",
    "ready_for_analysis",
)
ANALYSIS_STAGES = (
    "awaiting_fact_selection",
    "facts_selected",
    "analysis_running",
    "analysis_failed",
    "analysis_completed",
)
FAILURE_STAGES = ("partial", "failed")
WORKFLOW_STAGES = set(COLLECTION_STAGES) | set(ANALYSIS_STAGES) | set(FAILURE_STAGES)
_STAGE_INDEX = {
    stage: index for index, stage in enumerate(COLLECTION_STAGES + ANALYSIS_STAGES)
}


class WorkflowStateError(RuntimeError):
    """Raised when a workflow state transition or receipt is invalid."""


def _read_json(path: Path):
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _attempts_path(context_root: Path) -> Path:
    return context_root / "analysis-attempts.json"


def analysis_context_dir(run_root: Path, analysis_id: str) -> Path:
    """Return the isolated artifact directory for one phase-two analysis."""
    if not isinstance(analysis_id, str) or not analysis_id.strip():
        raise WorkflowStateError("analysis_id is required")
    normalized = analysis_id.strip()
    if Path(normalized).name != normalized or normalized in {".", ".."}:
        raise WorkflowStateError("analysis_id must be a single path segment")
    return Path(run_root) / "analysis" / normalized


def _analysis_state_path(run_root: Path, analysis_id: str) -> Path:
    return analysis_context_dir(run_root, analysis_id) / "workflow-state.json"


def _load_analysis_state(run_root: Path, analysis_id: str) -> dict:
    path = _analysis_state_path(run_root, analysis_id)
    if not path.is_file():
        return {"analysis_id": analysis_id.strip(), "stage": "not_started", "history": []}
    try:
        payload = _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowStateError(f"unable to load analysis state: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowStateError(f"analysis state must be an object: {path}")
    return payload


def _set_analysis_stage(run_root: Path, analysis_id: str, stage: str, **metadata) -> dict:
    if stage not in ANALYSIS_STAGES:
        raise WorkflowStateError(f"unsupported analysis stage: {stage}")
    state = _load_analysis_state(run_root, analysis_id)
    current = state.get("stage", "not_started")
    if current not in {"not_started", *ANALYSIS_STAGES}:
        raise WorkflowStateError(f"unsupported persisted analysis stage: {current}")
    if current in ANALYSIS_STAGES and _STAGE_INDEX[stage] < _STAGE_INDEX[current]:
        retry = current == "analysis_failed" and stage == "analysis_running"
        if not retry:
            raise WorkflowStateError(f"analysis stage cannot move backward: {current} -> {stage}")
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    event = {
        "stage": stage,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    event.update(metadata)
    history.append(event)
    state.update({
        "protocol_version": WORKFLOW_PROTOCOL_VERSION,
        "analysis_id": analysis_id.strip(),
        "stage": stage,
        "history": history,
    })
    if metadata:
        state["metadata"] = dict(metadata)
    path = _analysis_state_path(run_root, analysis_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(state, path)
    return state


def _load_attempts(root: Path) -> list[dict]:
    path = _attempts_path(root)
    if not path.is_file():
        return []
    try:
        payload = _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _save_attempts(root: Path, attempts: list[dict]) -> None:
    path = _attempts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(attempts, path)


def _manifest_is_complete(root: Path) -> bool:
    path = root / "facts" / "manifest.json"
    if not path.is_file():
        return False
    try:
        manifest = _read_json(path)
        packages = manifest.get("packages") if isinstance(manifest, dict) else None
        if not isinstance(packages, dict):
            return False
        for name, package in packages.items():
            if not isinstance(package, dict):
                return False
            relative = package.get("path")
            count = package.get("record_count")
            if (
                not isinstance(relative, str)
                or not relative.strip()
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                return False
            package_path = (root / relative).resolve()
            try:
                package_path.relative_to(root.resolve())
            except ValueError:
                return False
            if not package_path.is_file():
                return False
            payload = _read_json(package_path)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                return False
            if len(payload["records"]) != count:
                return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _legacy_fact_manifest_exists(root: Path) -> bool:
    """Recognize pre-protocol runs that only persisted the historical manifest."""
    path = root / "facts" / "manifest.json"
    if not path.is_file():
        return False
    try:
        payload = _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    packages = payload.get("packages") if isinstance(payload, dict) else None
    return isinstance(packages, dict) and bool(packages)


def _legacy_stage(root: Path, summary: dict | None = None) -> str:
    summary = summary or {}
    workflow = summary.get("workflow")
    if isinstance(workflow, dict) and workflow.get("stage") in WORKFLOW_STAGES:
        return workflow["stage"]
    if summary.get("workflowStage") in WORKFLOW_STAGES:
        return summary["workflowStage"]
    status = summary.get("status")
    if status in WORKFLOW_STAGES:
        return status
    if status in {"list_failed", "list_context_mismatch"}:
        return "failed"
    if status == "success" and _manifest_is_complete(root):
        return "ready_for_analysis"
    if _manifest_is_complete(root) or (
        not summary and _legacy_fact_manifest_exists(root)
    ):
        return "ready_for_analysis"
    if status == "partial":
        return "partial"
    if status == "failed":
        return "failed"
    return "not_started"


def _load_or_create_summary(root: Path) -> dict:
    path = root / RUN_SUMMARY_NAME
    if path.is_file():
        try:
            summary = load_run_state(path)
        except Exception as exc:
            raise WorkflowStateError(f"unable to load workflow state: {path}") from exc
        return summary

    run_id = None
    for candidate in (root / "batch-manifest.json", root / "facts" / "manifest.json"):
        if candidate.is_file():
            try:
                payload = _read_json(candidate)
            except (OSError, ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict):
                run_id = payload.get("runId") or payload.get("run_id")
                if run_id:
                    break
    return {
        "schemaVersion": 3,
        "runId": run_id or uuid.uuid4().hex,
        "status": "collecting",
    }


def _refresh_legacy_summary_artifact(root: Path) -> None:
    """Keep old manifests valid when the mutable workflow summary is updated."""
    manifest_path = root / "facts" / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError, TypeError):
        return
    artifacts = manifest.get("source_artifacts")
    if not isinstance(artifacts, list):
        return
    summary_path = root / RUN_SUMMARY_NAME
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("path") != RUN_SUMMARY_NAME:
            continue
        if not summary_path.is_file():
            return
        artifact["size_bytes"] = summary_path.stat().st_size
        artifact["sha256"] = sha256_file(summary_path)
        write_json_atomically(manifest, manifest_path)
        return


def set_stage(run_root: Path, stage: str, **metadata) -> dict:
    """Persist a monotonic workflow stage while retaining existing run fields."""
    root = Path(run_root)
    if stage not in WORKFLOW_STAGES:
        raise WorkflowStateError(f"unsupported workflow stage: {stage}")
    root.mkdir(parents=True, exist_ok=True)
    summary = _load_or_create_summary(root)
    current = _legacy_stage(root, summary)

    if current in FAILURE_STAGES and stage not in FAILURE_STAGES + ("collecting",):
        raise WorkflowStateError(f"cannot transition from {current} to {stage}")
    if stage in _STAGE_INDEX and current in _STAGE_INDEX:
        repeat_analysis = current == "analysis_completed" and stage in {
            "awaiting_fact_selection", "analysis_running",
        }
        retry_analysis = current in {"analysis_running", "analysis_failed"} and stage == "analysis_running"
        if _STAGE_INDEX[stage] < _STAGE_INDEX[current] and not (repeat_analysis or retry_analysis):
            raise WorkflowStateError(f"workflow stage cannot move backward: {current} -> {stage}")
    if current == "not_started" and stage != "collecting":
        raise WorkflowStateError(f"workflow must start at collecting, not {stage}")

    previous_status = summary.get("status")
    if previous_status not in WORKFLOW_STAGES and previous_status:
        summary.setdefault("collectionStatus", previous_status)
    workflow = summary.get("workflow")
    if not isinstance(workflow, dict):
        workflow = {}
    history = workflow.get("history")
    if not isinstance(history, list):
        history = []
    event = {
        "stage": stage,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    event.update(metadata)
    history.append(event)
    workflow.update({
        "protocol_version": WORKFLOW_PROTOCOL_VERSION,
        "stage": stage,
        "history": history,
    })
    if metadata:
        workflow["metadata"] = dict(metadata)
    summary["workflow"] = workflow
    summary["workflowProtocolVersion"] = WORKFLOW_PROTOCOL_VERSION
    summary["workflowStage"] = stage
    summary["status"] = stage
    save_run_state(summary, root / RUN_SUMMARY_NAME)
    _refresh_legacy_summary_artifact(root)
    return summary


def begin_model_analysis(run_root: Path, request_id: str, analysis_id: str | None = None) -> dict:
    """Move one isolated analysis context into model execution."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise WorkflowStateError("request_id is required")
    resolved_id = analysis_id or request_id
    status = get_analysis_status(run_root, resolved_id)
    if status["stage"] not in {"not_started", "facts_selected", "analysis_failed"}:
        raise WorkflowStateError("model analysis requires a new, selected, or failed analysis context")
    root = Path(run_root)
    attempt_id = uuid.uuid4().hex
    context_root = analysis_context_dir(root, resolved_id)
    attempts = _load_attempts(context_root)
    attempts.append({"attempt_id": attempt_id, "request_id": request_id.strip(), "status": "running", "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    _save_attempts(context_root, attempts)
    return _set_analysis_stage(root, resolved_id, "analysis_running", request_id=request_id.strip(), attempt_id=attempt_id)


def begin_fact_selection(run_root: Path, analysis_id: str) -> dict:
    """Begin the local fact-selection handoff for a phase-two analysis."""
    run_status = get_workflow_status(run_root)
    if run_status["stage"] != "ready_for_analysis":
        raise WorkflowStateError("fact selection requires ready_for_analysis")
    status = get_analysis_status(run_root, analysis_id)
    if status["stage"] != "not_started":
        raise WorkflowStateError("analysis context already exists")
    return _set_analysis_stage(run_root, analysis_id, "awaiting_fact_selection")


def complete_fact_selection(run_root: Path, analysis_id: str, selection_path: Path) -> dict:
    """Persist a validated local selection in one analysis context."""
    path = Path(selection_path)
    if not path.is_file():
        raise WorkflowStateError(f"fact selection is missing: {path}")
    status = get_analysis_status(run_root, analysis_id)
    if status["stage"] != "awaiting_fact_selection":
        raise WorkflowStateError("fact selection is not awaiting a result")
    return _set_analysis_stage(run_root, analysis_id, "facts_selected", selection_path=str(path))


def complete_model_analysis(run_root: Path, analysis_id: str, result_path: Path, report_path: Path | None = None) -> dict:
    """Persist successful completion metadata in one analysis context."""
    path = Path(result_path)
    if not path.is_file():
        raise WorkflowStateError(f"analysis result is missing: {path}")
    if report_path is not None and not Path(report_path).is_file():
        raise WorkflowStateError(f"analysis report is missing: {report_path}")
    status = get_analysis_status(run_root, analysis_id)
    if status["stage"] != "analysis_running":
        raise WorkflowStateError("model analysis is not running")
    root = Path(run_root)
    attempts = _load_attempts(analysis_context_dir(root, analysis_id))
    if attempts and attempts[-1].get("status") == "running":
        attempts[-1].update({"status": "completed", "result_path": str(path), "report_path": str(report_path) if report_path else None, "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        _save_attempts(analysis_context_dir(root, analysis_id), attempts)
    import hashlib
    return _set_analysis_stage(root, analysis_id, "analysis_completed", result_path=str(path.resolve()),
                               result_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def fail_model_analysis(run_root: Path, analysis_id: str, request_id: str, error: str) -> dict:
    """Return a failed model attempt to a retryable analysis context."""
    status = get_analysis_status(run_root, analysis_id)
    if status["stage"] != "analysis_running":
        raise WorkflowStateError("model analysis is not running")
    root = Path(run_root)
    attempts = _load_attempts(analysis_context_dir(root, analysis_id))
    if attempts and attempts[-1].get("status") == "running":
        attempts[-1].update({"status": "failed", "error": str(error), "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        _save_attempts(analysis_context_dir(root, analysis_id), attempts)
    return _set_analysis_stage(root, analysis_id, "analysis_failed", request_id=request_id, error=str(error))


def get_analysis_status(run_root: Path, analysis_id: str) -> dict:
    """Read one phase-two context without changing collection readiness."""
    state = _load_analysis_state(run_root, analysis_id)
    stage = state.get("stage", "not_started")
    if stage not in {"not_started", *ANALYSIS_STAGES}:
        return {"status": "failed", "stage": "failed", "analysis_id": analysis_id, "error": "analysis state is invalid"}
    return {
        "status": stage,
        "stage": stage,
        "analysis_id": analysis_id.strip(),
        "state_path": str(_analysis_state_path(run_root, analysis_id)),
        "state": state,
    }


def get_workflow_status(run_root: Path) -> dict:
    """Read workflow status, inferring a ready legacy state without mutation."""
    root = Path(run_root)
    summary_path = root / RUN_SUMMARY_NAME
    summary = {}
    if summary_path.is_file():
        try:
            summary = load_run_state(summary_path)
        except Exception:
            return {
                "status": "failed",
                "stage": "failed",
                "ready_for_analysis": False,
                "run_id": None,
                "error": "run summary is unreadable",
            }
    persisted_stage = _legacy_stage(root, summary)
    # v1 persisted analysis state beside collection state in run-summary.json.
    # Facts remain usable while those legacy contexts exist, so v2 exposes the
    # run as ready and keeps any new analysis state under its own context.
    stage = (
        "ready_for_analysis"
        if persisted_stage in ANALYSIS_STAGES and _manifest_is_complete(root)
        else persisted_stage
    )
    return {
        "status": stage,
        "stage": stage,
        "ready_for_analysis": stage == "ready_for_analysis",
        "run_id": summary.get("runId") or summary.get("run_id"),
        "summary_path": str(summary_path) if summary_path.is_file() else None,
        "collection_status": summary.get("collectionStatus") or summary.get("status"),
        "legacy_analysis_stage": persisted_stage if persisted_stage in ANALYSIS_STAGES else None,
        "summary": summary,
    }


def _script_availability(root: Path, manifest: dict) -> dict:
    package = manifest.get("packages", {}).get("script", {})
    relative = package.get("path") if isinstance(package, dict) else None
    if not isinstance(relative, str):
        return {"record_count": 0, "source_empty_fields": []}
    try:
        records = _read_json(root / relative).get("records", [])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"record_count": 0, "source_empty_fields": []}
    fields = set()
    for record in records:
        availability = record.get("data_availability", {}) if isinstance(record, dict) else {}
        for field, state in availability.items():
            if isinstance(state, dict) and state.get("missing_reason") == "source_empty":
                fields.add(field)
    return {"record_count": len(records), "source_empty_fields": sorted(fields)}


def build_ready_receipt(run_root: Path) -> dict:
    """Build the user-facing handoff from persisted facts and workflow state."""
    root = Path(run_root)
    status = get_workflow_status(root)
    manifest = {}
    manifest_path = root / "facts" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            manifest = {}
    packages = manifest.get("packages", {}) if isinstance(manifest, dict) else {}
    fact_packages = {
        name: package.get("path")
        for name, package in packages.items()
        if isinstance(package, dict) and isinstance(package.get("path"), str)
    }
    receipt = {
        "status": status["status"],
        "stage": status["stage"],
        "ready_for_analysis": status["ready_for_analysis"] and _manifest_is_complete(root),
        "run_id": status.get("run_id") or manifest.get("run_id"),
        "coverage": manifest.get("coverage", {}) if isinstance(manifest, dict) else {},
        "fact_packages": fact_packages,
        "available_modules": list(fact_packages),
        "brand_classification": manifest.get("brand_classification", {}) if isinstance(manifest, dict) else {},
        "script_availability": _script_availability(root, manifest) if manifest else {"record_count": 0, "source_empty_fields": []},
        "data_gaps": [],
        "retryable": status["status"] == "analysis_failed",
        "next_step": "数据已准备完成，请直接输入分析思路或商业问题；收到前不会启动商业分析。"
        if status["status"] == "ready_for_analysis" and _manifest_is_complete(root)
        else "分析结果校验失败；请修正模型结果后在同一 run 中重试，不会重新采集或修改事实包。"
        if status["status"] == "analysis_failed"
        else "采集或事实准备尚未完成，请先补齐可用数据。",
    }
    brand_metadata = receipt["brand_classification"]
    if isinstance(brand_metadata, dict) and brand_metadata.get("unknown_count", 0):
        receipt["data_gaps"].append("存在未知品牌记录；品牌识别仅限运行时候选范围。")
    for field in receipt["script_availability"]["source_empty_fields"]:
        receipt["data_gaps"].append(f"脚本字段 {field} 为 source_empty，不能据此生成脚本结论。")
    return receipt
