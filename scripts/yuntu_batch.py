"""Batch fetch orchestration for Yuntu rankings analysis."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import json
from pathlib import Path
import time

if __package__:
    from . import rpa_dynamic_detail_client as detail_dynamic_rpa
    from .rpa_io import (
        download_file,
        parse_csv_records,
        unpack_gateway_result,
        write_json_atomically,
    )
    from .yuntu_normalizer import normalize_detail_records, normalize_list_records
    from .yuntu_highlights import (
        details_missing_standard_highlights,
        details_missing_highlights_by_spec,
        generate_standard_highlights_for_detail,
        generate_highlights_for_detail,
        generate_missing_highlights_for_detail,
        persist_video_for_detail,
        load_highlights_manifest,
        write_highlights_manifest,
    )
    from . import yuntu_run_state as run_state
    from .workflow_state import get_workflow_status, set_stage
    from .material_field_registry import default_material_field_registry
    from .material_list_selection import select_materials, MaterialListSelectionError
else:
    import rpa_dynamic_detail_client as detail_dynamic_rpa
    from rpa_io import (
        download_file,
        parse_csv_records,
        unpack_gateway_result,
        write_json_atomically,
    )
    from yuntu_normalizer import normalize_detail_records, normalize_list_records
    from yuntu_highlights import (
        details_missing_standard_highlights,
        details_missing_highlights_by_spec,
        generate_standard_highlights_for_detail,
        generate_highlights_for_detail,
        generate_missing_highlights_for_detail,
        persist_video_for_detail,
        load_highlights_manifest,
        write_highlights_manifest,
    )
    import yuntu_run_state as run_state
    from workflow_state import get_workflow_status, set_stage
    from material_field_registry import default_material_field_registry
    from material_list_selection import select_materials, MaterialListSelectionError


class YuntuBatchError(RuntimeError):
    """Raised when a Yuntu batch cannot be started."""


DETAIL_WAVE_CONCURRENCY = 3
HIGHLIGHT_CONCURRENCY = 3
DEFAULT_MATERIAL_LIST = {
    "filters": [], "sort": {"field": "rank", "direction": "asc"}
}
FACT_PACKAGE_NAMES = (
    "material_ranking",
    "audience",
    "script",
    "highlight_frames",
)
SELECTION_GROUP_FIELDS = {
    "selectionGroupId": "id",
    "brandId": "brand_id",
    "brandName": "brand_name",
    "touchpointCode": "touchpoint_code",
    "touchpointName": "touchpoint_name",
}


class ConnectorInvoker:
    def __init__(self, invoke_list, invoke_detail, *, submit_detail=None, wait_detail=None):
        self._invoke_list = invoke_list
        self._invoke_detail = invoke_detail
        self._submit_detail = submit_detail
        self._wait_detail = wait_detail

    def invoke_list(self, shop_id, list_params):
        return self._invoke_list(shop_id, list_params)

    def invoke_detail(self, shop_id, detail_params):
        return self._invoke_detail(shop_id, detail_params)

    @property
    def supports_detail_task_recovery(self):
        return callable(self._submit_detail) and callable(self._wait_detail)

    def submit_detail(self, shop_id, detail_params):
        if not self.supports_detail_task_recovery:
            raise YuntuBatchError("detail task submission recovery is unavailable")
        return self._submit_detail(shop_id, detail_params)

    def wait_detail(self, task_group_id):
        if not self.supports_detail_task_recovery:
            raise YuntuBatchError("detail task status recovery is unavailable")
        return self._wait_detail(task_group_id)


def _supports_detail_task_recovery(invoker):
    return bool(getattr(invoker, "supports_detail_task_recovery", False))


class FileDownloader:
    def download_file(self, file_url, target_path):
        return download_file(file_url, target_path)


def _elapsed(started_at):
    return round(time.monotonic() - started_at, 6)


def _validate_positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise YuntuBatchError(f"{name} must be a positive integer")
    return value


def _selection_group(record):
    group = record.get("selection_group")
    if not isinstance(group, dict):
        group = {}
    return {
        "id": group.get("id") or "default",
        "brand_id": group.get("brand_id") or "",
        "brand_name": group.get("brand_name") or "",
        "touchpoint_code": group.get("touchpoint_code") or "",
        "touchpoint_name": group.get("touchpoint_name") or "",
    }


def _top_ranked_materials_from_list(records, target_top_n, material_list=None, registry=None):
    """Return unique local-selection leaders while retaining detail validation context."""
    material_list = DEFAULT_MATERIAL_LIST if material_list is None else material_list
    registry = default_material_field_registry() if registry is None else registry
    try:
        selected_records = select_materials(records, material_list, registry)
    except MaterialListSelectionError as exc:
        raise YuntuBatchError(str(exc)) from exc
    selected = []
    seen = set()
    for record in selected_records:
        material_id = record["material_id"]
        if material_id in seen:
            continue
        group = _selection_group(record)
        seen.add(material_id)
        selected.append(
            {
                "rank": record["rank"],
                "material_id": material_id,
                "selectionGroupId": group["id"],
                "selectionGroup": group,
            }
        )
        if len(selected) == target_top_n:
            break
    return selected


def _manifest_material(item):
    return {
        "rank": item["rank"],
        "material_id": item["material_id"],
        **_selection_group_fields(item),
    }


def _selection_group_fields(item):
    """Return the durable manifest shape for an optional selection group."""
    group_id = item.get("selectionGroupId")
    if not group_id or group_id == "default":
        return {}
    group = item.get("selectionGroup")
    if isinstance(group, dict):
        return {
            field: group.get(key, "")
            for field, key in SELECTION_GROUP_FIELDS.items()
        }
    return {
        field: item.get(field, "")
        for field in SELECTION_GROUP_FIELDS
    }


def _expected_selection_group(item):
    fields = _selection_group_fields(item)
    if not fields:
        return None
    return {key: fields[field] for field, key in SELECTION_GROUP_FIELDS.items()}


def _detail_request(item, detail_date_params):
    return {
        "material_id": item["material_id"],
        **detail_date_params,
        **_selection_group_fields(item),
    }


def _target_satisfied(manifest):
    candidates = manifest.get("candidateMaterials")
    if isinstance(candidates, list) and candidates:
        return len(manifest.get("selectedMaterials", [])) >= len(candidates)
    return len(manifest.get("selectedMaterials", [])) >= manifest.get("targetTopN", 0)


def _materialize_records(result, *, csv_path, json_path, downloader):
    unpacked = unpack_gateway_result(result)
    if unpacked.kind == "files":
        path = downloader.download_file(unpacked.file_url, csv_path)
        return parse_csv_records(path), str(path)
    write_json_atomically(unpacked.records, json_path)
    return unpacked.records, str(json_path)


def validate_detail_csv_records(records):
    """Require connector business fields and an explicit identity before normalization.

    The normalizer's historical identity fallback must not turn an error CSV
    into successful first-batch evidence.
    """
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise YuntuBatchError("detail CSV must contain exactly one business record")
    raw = records[0]
    core = raw.get("coreData") if isinstance(raw.get("coreData"), dict) else {}
    identities = [raw.get(key) for key in (
        "materialId", "material_id", "coreData.materialId", "core_data.material_id"
    )] + [core.get("objectId"), core.get("materialId")]
    structured_fields = ("trends", "coreData", "secondTrend", "contentFormula",
                         "creativeBreakdown", "audiencePortrait")
    business_values = any(isinstance(raw.get(key), (dict, list)) for key in structured_fields)
    business_values = business_values or any(
        isinstance(raw.get(key), str) and raw[key].strip() for key in ("title", "videoUrl", "video_url")
    ) or any(value not in (None, "") and key not in raw.get("__parse_failed_fields__", [])
             for key, value in raw.items() if key.startswith((
                 "coreData.", "secondTrend.", "contentFormula.", "creativeBreakdown."
             )))
    if not any(value not in (None, "") for value in identities) or not business_values:
        raise YuntuBatchError("detail CSV lacks explicit material identity or business fields")


def _update_first_batch_gate(manifest, *, drained=False):
    gate = manifest.get("firstBatchCsvGate")
    if not gate or gate.get("status") in {"passed", "blocked"}:
        return gate
    ids = gate["materialIds"]
    outcomes = gate.setdefault("outcomes", {})
    gate["validCsvCount"] = sum(outcomes.get(mid) == "valid_csv" for mid in ids)
    if not drained:
        return gate
    if any(outcomes.get(mid) in (None, "unconfirmed") for mid in ids):
        gate.update(status="unconfirmed", errorCode="first_batch_unconfirmed",
                    message="首批素材详情任务状态尚未确认，已暂停后续所有 RPA 提交；仅可查询已登记任务，不能补位或重新提交。")
    elif gate["validCsvCount"]:
        gate.update(status="passed", errorCode=None, message="首批详情 CSV 已验证，可继续后续批次。")
    else:
        gate.update(status="blocked", errorCode="first_batch_no_valid_csv",
                    message="首批素材详情未返回有效 CSV 数据，当前入参可能有问题。已暂停本次运行后续所有 RPA 任务；请核对账号、日期及详情入参，修正后使用新运行目录重测首批。")
    return gate


def _set_first_batch_outcome(manifest, material_id, outcome):
    gate = manifest.get("firstBatchCsvGate", {})
    if material_id in gate.get("materialIds", []):
        # Once business CSV has been verified, later persistence/media failures
        # cannot relabel the request as an input failure.
        if gate.setdefault("outcomes", {}).get(material_id) != "valid_csv":
            gate["outcomes"][material_id] = outcome
    _update_first_batch_gate(manifest)


def _restore_first_batch_gate(root, manifest, detail_date_params):
    """Recover first-wave evidence without allowing a later wave to become a probe."""
    gate = manifest.get("firstBatchCsvGate")
    if not gate:
        ids = list(dict.fromkeys(item["material_id"] for item in
            manifest.get("submittedMaterials", []) if isinstance(item, dict) and item.get("material_id")))
        if not ids:
            return
        gate = manifest["firstBatchCsvGate"] = {
            "status": "pending", "functionCode": detail_dynamic_rpa.CONNECTOR_CODE,
            "materialIds": ids[:DETAIL_WAVE_CONCURRENCY], "outcomes": {}, "validCsvCount": 0,
        }
    if gate.get("status") in {"passed", "blocked"}:
        return
    terminal_ids = {item.get("material_id") for bucket in ("selectedMaterials", "rejectedMaterials")
                    for item in manifest.get(bucket, []) if isinstance(item, dict)}
    candidates = {item.get("material_id"): item for item in manifest.get("candidateMaterials", [])}
    for mid in gate["materialIds"]:
        csv_path = root / "raw-csv" / "details" / f"{mid}.csv"
        try:
            records = parse_csv_records(csv_path)
            validate_detail_csv_records(records)
            normalize_detail_records(records, material_id=mid,
                source_path=_relative(root, csv_path), expected_context=detail_date_params,
                expected_selection_group=_expected_selection_group(candidates.get(mid, {})))
        except Exception:
            if mid in terminal_ids:
                _set_first_batch_outcome(manifest, mid, "no_valid_csv")
        else:
            _set_first_batch_outcome(manifest, mid, "valid_csv")
    _update_first_batch_gate(manifest, drained=True)


def _pause_for_first_batch(root, manifest, summary, manifest_path):
    gate = manifest["firstBatchCsvGate"]
    manifest.update(status="paused", error=gate["message"])
    summary.update(status="paused", collectionStatus="paused", firstBatchCsvGate=gate,
                   taskRuns=manifest.get("taskRuns", {}))
    summary["stages"]["details"]["requestStatus"] = "paused"
    _sync_stage_counts(summary, manifest, root)
    write_json_atomically(manifest, manifest_path)
    _save_summary(root, summary)
    set_stage(root, "partial", reason=gate["message"])
    return 1


def _new_manifest(
    *,
    list_params,
    detail_date_params,
    target_top_n,
    candidate_top_n,
    material_list,
    summary,
):
    return {
        "schemaVersion": 3,
        "runId": summary["runId"],
        "parameterFingerprint": summary["parameterFingerprint"],
        "collectionContract": summary["collectionContract"],
        "status": "running",
        "filters": list_params,
        "detailDateRange": detail_date_params,
        "targetTopN": target_top_n,
        "candidateTopN": candidate_top_n,
        "materialList": material_list,
        "fullListCount": 0,
        "matchedMaterialCount": 0,
        "candidateMaterials": [],
        "selectionGroups": [],
        "taskRuns": {},
        "submittedMaterials": [],
        "selectedMaterials": [],
        "rejectedMaterials": [],
        "unresolvedMaterials": [],
        "cancelledMaterials": [],
        "timingsSec": {},
    }


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _open_or_new_manifest(
    *,
    manifest_path,
    list_params,
    detail_date_params,
    target_top_n,
    candidate_top_n,
    material_list,
    summary,
):
    if not manifest_path.is_file():
        return _new_manifest(
            list_params=list_params,
            detail_date_params=detail_date_params,
            target_top_n=target_top_n,
            candidate_top_n=candidate_top_n,
            material_list=material_list,
            summary=summary,
        )
    try:
        manifest = _load_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise YuntuBatchError("existing batch manifest is unreadable") from exc
    if (
        manifest.get("filters") != list_params
        or manifest.get("detailDateRange") != detail_date_params
        or manifest.get("targetTopN") != target_top_n
        or manifest.get("candidateTopN") != candidate_top_n
        or manifest.get("materialList", DEFAULT_MATERIAL_LIST) != material_list
    ):
        raise YuntuBatchError(
            "output directory belongs to a different collection contract; choose a new output directory"
        )
    manifest.update(
        {
            "schemaVersion": 3,
            "runId": summary["runId"],
            "parameterFingerprint": summary["parameterFingerprint"],
            "collectionContract": summary["collectionContract"],
            "materialList": material_list,
            "status": "running",
        }
    )
    for key in (
        "candidateMaterials",
        "selectionGroups",
        "taskRuns",
        "submittedMaterials",
        "selectedMaterials",
        "rejectedMaterials",
        "unresolvedMaterials",
        "cancelledMaterials",
    ):
        manifest.setdefault(key, [])
    manifest.setdefault("timingsSec", {})
    if not isinstance(manifest.get("taskRuns"), dict):
        manifest["taskRuns"] = {}
    return manifest


def _detail_source_path(root, material_id):
    csv_path = root / "raw-csv" / "details" / f"{material_id}.csv"
    json_path = root / "raw-json" / "details" / f"{material_id}.json"
    if csv_path.is_file():
        return _relative(root, csv_path)
    if json_path.is_file():
        return _relative(root, json_path)
    return None


def _restore_persisted_details(root, manifest, candidates):
    """Rebuild successful manifest entries from durable normalized detail files."""
    candidate_by_id = {item["material_id"]: item for item in candidates}
    existing = {
        item.get("material_id"): item
        for item in manifest.get("selectedMaterials", [])
        if isinstance(item, dict) and isinstance(item.get("material_id"), str)
    }
    restored = []
    detail_dir = root / "normalized" / "detail-records"
    for material_id, item in candidate_by_id.items():
        detail_path = detail_dir / f"{material_id}.json"
        if not detail_path.is_file():
            continue
        try:
            detail = _load_json(detail_path)
        except (OSError, ValueError):
            continue
        if detail.get("material_id") != material_id:
            continue
        previous = existing.get(material_id, {})
        restored.append(
            {
                **_manifest_material(item),
                "listRecordPath": "normalized/list-records.json",
                "detailRecordPath": _relative(root, detail_path),
                "rawDetailPath": previous.get("rawDetailPath")
                or _detail_source_path(root, material_id),
                "timingsSec": previous.get("timingsSec", {}),
            }
        )
    manifest["selectedMaterials"] = sorted(restored, key=lambda entry: entry["rank"])


def _mark_unresolved_submissions(manifest, candidates):
    """Keep interrupted detail requests visible without silently retrying them."""
    _sync_task_run_field_aliases(manifest)
    candidate_by_id = {item["material_id"]: item for item in candidates}
    terminal_ids = _selected_ids(manifest)
    for key in ("rejectedMaterials", "cancelledMaterials", "unresolvedMaterials"):
        terminal_ids.update(
            item.get("material_id")
            for item in manifest.get(key, [])
            if isinstance(item, dict) and isinstance(item.get("material_id"), str)
        )
    unresolved = list(manifest.get("unresolvedMaterials", []))
    for submitted in manifest.get("submittedMaterials", []):
        if not isinstance(submitted, dict):
            continue
        material_id = submitted.get("material_id")
        item = candidate_by_id.get(material_id)
        if item is None or material_id in terminal_ids:
            continue
        task_run = manifest.get("taskRuns", {}).get(f"detail:{material_id}")
        attempts = task_run.get("attempts", []) if isinstance(task_run, dict) else []
        active = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        if active.get("task_group_id") and active.get("remote_status") in {
            "submitted", "pending", "running"
        }:
            continue
        unresolved.append(
            {
                **_manifest_material(item),
                "reason": "interrupted_before_persistence",
            }
        )
        terminal_ids.add(material_id)
    manifest["unresolvedMaterials"] = unresolved


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _task_run(manifest, item, detail_date_params):
    material_id = item["material_id"]
    task_runs = manifest.setdefault("taskRuns", {})
    return task_runs.setdefault(
        f"detail:{material_id}",
        {
            "material_id": material_id,
            "selection_group": item.get("selectionGroup", {}),
            "operation": "detail",
            "function_code": detail_dynamic_rpa.CONNECTOR_CODE,
            "functionCode": detail_dynamic_rpa.CONNECTOR_CODE,
            "business_params": {"material_id": material_id, **detail_date_params},
            "businessParams": {"material_id": material_id, **detail_date_params},
            "task_group_id": None,
            "taskGroupId": None,
            "status": "pending",
            "attempted_at": None,
            "attemptedAt": None,
            "error_class": None,
            "errorClass": None,
            "attempts": [],
        },
    )


def _sync_task_run_field_aliases(manifest):
    """Keep the durable ledger readable by both old and new consumers."""
    task_runs = manifest.get("taskRuns")
    if not isinstance(task_runs, dict):
        return
    field_pairs = (
        ("function_code", "functionCode"),
        ("business_params", "businessParams"),
        ("task_group_id", "taskGroupId"),
        ("attempted_at", "attemptedAt"),
        ("error_class", "errorClass"),
    )
    attempt_pairs = (
        ("task_group_id", "taskGroupId"),
        ("request_status", "requestStatus"),
        ("remote_status", "remoteStatus"),
        ("artifact_status", "artifactStatus"),
        ("submitted_at", "submittedAt"),
        ("last_observed_at", "lastObservedAt"),
        ("error_class", "errorClass"),
        ("artifact_paths", "artifactPaths"),
    )
    for task_run in task_runs.values():
        if not isinstance(task_run, dict):
            continue
        for snake, camel in field_pairs:
            if snake in task_run:
                task_run[camel] = task_run[snake]
            elif camel in task_run:
                task_run[snake] = task_run[camel]
        for attempt in task_run.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            for snake, camel in attempt_pairs:
                if snake in attempt:
                    attempt[camel] = attempt[snake]
                elif camel in attempt:
                    attempt[snake] = attempt[camel]


def _active_detail_task_attempt(manifest, material_id):
    task_run = manifest.get("taskRuns", {}).get(f"detail:{material_id}")
    attempts = task_run.get("attempts", []) if isinstance(task_run, dict) else []
    if not attempts or not isinstance(attempts[-1], dict):
        return None
    attempt = attempts[-1]
    task_group_id = attempt.get("task_group_id") or attempt.get("taskGroupId")
    remote_status = attempt.get("remote_status") or attempt.get("remoteStatus")
    if task_group_id and remote_status in {
        "submitted", "pending", "running"
    }:
        attempt.setdefault("task_group_id", task_group_id)
        attempt.setdefault("taskGroupId", task_group_id)
        attempt.setdefault("remote_status", remote_status)
        attempt.setdefault("remoteStatus", remote_status)
        return attempt
    return None


def _new_detail_attempt(manifest, item, detail_date_params):
    task_run = _task_run(manifest, item, detail_date_params)
    now = _now_iso()
    attempt = {
        "attempt": len(task_run["attempts"]) + 1,
        "task_group_id": None,
        "taskGroupId": None,
        "request_status": "submitting",
        "requestStatus": "submitting",
        "remote_status": "submitted",
        "remoteStatus": "submitted",
        "artifact_status": "pending",
        "artifactStatus": "pending",
        "submitted_at": now,
        "submittedAt": now,
        "last_observed_at": now,
        "lastObservedAt": now,
        "errors": [],
        "error_class": None,
        "errorClass": None,
        "artifact_paths": [],
        "artifactPaths": [],
    }
    task_run["attempts"].append(attempt)
    task_run.update({
        "task_group_id": None,
        "taskGroupId": None,
        "status": "submitting",
        "attempted_at": now,
        "attemptedAt": now,
        "error_class": None,
        "errorClass": None,
    })
    return attempt


def _wait_detail_task_result(*, item, task_group_id, invoker):
    started = time.monotonic()
    try:
        return {
            "item": item,
            "result": invoker.wait_detail(task_group_id),
            "started": started,
            "task_group_id": task_group_id,
            "error": None,
        }
    except Exception as exc:
        return {
            "item": item,
            "result": None,
            "started": started,
            "task_group_id": task_group_id,
            "error": exc,
        }


def _detail_wait_error_class(error):
    """Classify polling failures without treating protocol drift as transient."""
    message = str(error).casefold()
    if "rpa task failed" in message:
        return "remote_failed"
    if "unknown status" in message:
        return "protocol"
    if "poll timeout" in message:
        return "poll_timeout"
    return "poll_timeout"


def _detail_artifact_error_class(error):
    """Map local materialization failures to the persisted task error contract."""
    message = str(error).casefold()
    if "normalize" in error.__class__.__name__.casefold():
        return "normalize"
    if "download" in message or "file" in message and "url" in message:
        return "download"
    if any(token in message for token in ("parse", "payload", "records", "csv")):
        return "parse"
    return "protocol"


def _remove_material_from_bucket(manifest, bucket, material_id):
    manifest[bucket] = [
        item for item in manifest.get(bucket, [])
        if not isinstance(item, dict) or item.get("material_id") != material_id
    ]


def _has_existing_collection_artifacts(root):
    return any(
        path.exists()
        for path in (
            root / run_state.RUN_SUMMARY_NAME,
            root / "batch-manifest.json",
            root / "raw-csv" / "list.csv",
            root / "raw-json" / "list-response.json",
            root / "normalized" / "list-records.json",
        )
    )


def _open_collection_summary(root, contract):
    summary_path = root / run_state.RUN_SUMMARY_NAME
    if _has_existing_collection_artifacts(root):
        if not summary_path.is_file():
            raise YuntuBatchError(
                "output directory contains an unverifiable existing batch; choose a new output directory"
            )
        try:
            summary = run_state.load_run_state(summary_path)
        except run_state.YuntuRunStateError as exc:
            raise YuntuBatchError(
                "output directory contains an unverifiable existing batch; choose a new output directory"
            ) from exc
        if not run_state.collection_contract_matches(summary, contract):
            raise YuntuBatchError(
                "output directory belongs to a different collection contract; choose a new output directory"
            )
        return summary
    summary = run_state.new_collection_summary(contract)
    run_state.save_run_state(summary, summary_path)
    return summary


def _save_summary(root, summary):
    run_state.save_run_state(summary, root / run_state.RUN_SUMMARY_NAME)


def _record_artifact(summary, root, key, path, source):
    try:
        run_state.register_artifact(summary, root, key, path, source)
    except run_state.YuntuRunStateError:
        # State remains useful even when an optional raw artifact is not available.
        pass


def _sync_stage_counts(summary, manifest, root):
    details_dir = root / "normalized" / "detail-records"
    persisted_count = len(list(details_dir.glob("*.json"))) if details_dir.is_dir() else 0
    counts = summary["stageCounts"]
    counts.update(
        {
            "candidateCount": len(manifest.get("candidateMaterials", [])),
            "fullListCount": manifest.get("fullListCount", 0),
            "matchedMaterialCount": manifest.get("matchedMaterialCount", 0),
            "detailSubmittedCount": len(manifest.get("submittedMaterials", [])),
            "detailRecordsPersistedCount": persisted_count,
            "selectedCount": len(manifest.get("selectedMaterials", [])),
            "rejectedCount": len(manifest.get("rejectedMaterials", [])),
            "unresolvedCount": len(manifest.get("unresolvedMaterials", [])),
            "cancelledCount": len(manifest.get("cancelledMaterials", [])),
        }
    )


def _brand_tokens(value):
    return {
        part.strip().casefold()
        for part in str(value or "").split("/")
        if part.strip()
    }


def _display_date(value):
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _validate_list_context(list_records, list_params):
    if not list_records:
        return {"valid": False, "issues": ["list records are empty"], "observed": {}}
    context = list_records[0].get("list_context")
    context = context if isinstance(context, dict) else {}
    issues = []
    if "industry" in list_params and context.get("industry") != list_params["industry"]:
        issues.append("industry")
    period_type = list_params.get("date_type", list_params.get("time_range_type"))
    if period_type == "CUSTOM":
        expected = (
            f"{_display_date(list_params['custom_start_date'])} ~ "
            f"{_display_date(list_params['custom_end_date'])}"
        )
        if context.get("custom_date_range") != expected:
            issues.append("custom_date_range")
    if list_params.get("brand_scope_type") == "SPECIFIED_BRANDS":
        if context.get("brand_scope") not in {"指定品牌", "SPECIFIED_BRANDS"}:
            issues.append("brand_scope")
        actual_brands = context.get("brands")
        if not isinstance(actual_brands, list):
            actual_brands = []
        actual_tokens = [_brand_tokens(brand) for brand in actual_brands]
        for brand in list_params.get("brands", []):
            if not any(_brand_tokens(brand) & tokens for tokens in actual_tokens):
                issues.append("brands")
                break
    if "ranking_limit_type" in list_params:
        expected_labels = {
            "EXPOSURE_TOP1000": {"曝光量TOP1000", "EXPOSURE_TOP1000"},
            "CTR_TOP1000": {"点击率TOP1000", "CTR_TOP1000", "CTRTOP1000"},
            "INTERACTION_RATE_TOP1000": {"互动率TOP1000", "INTERACTION_RATE_TOP1000"},
            "COMPLETION_RATE_TOP1000": {"完播率TOP1000", "COMPLETION_RATE_TOP1000"},
        }
        if context.get("ranking_limit") not in expected_labels[list_params["ranking_limit_type"]]:
            issues.append("ranking_limit")
    return {"valid": not issues, "issues": issues, "observed": context}


def _reusable_list_artifact(root, list_params, detail_date_params):
    """Return a completed list CSV from the same run, if one is available."""
    manifest_path = root / "batch-manifest.json"
    list_path = root / "raw-csv" / "list.csv"
    if not manifest_path.is_file() or not list_path.is_file() or not list_path.stat().st_size:
        return None
    try:
        previous = _load_json(manifest_path)
    except (OSError, ValueError):
        return None
    if (
        previous.get("filters") != list_params
        or previous.get("detailDateRange") != detail_date_params
    ):
        return None
    return list_path


def _selected_ids(manifest):
    return {item["material_id"] for item in manifest.get("selectedMaterials", [])}


def _refresh_ready_materials(root, manifest):
    list_records = _load_json(root / "normalized" / "list-records.json")
    list_by_id = {
        record["material_id"]: record
        for record in list_records
        if isinstance(record, dict) and "material_id" in record
    }
    selected_materials = sorted(
        manifest.get("selectedMaterials", []), key=lambda item: item.get("rank", 0)
    )
    ready_materials = [
        list_by_id[item["material_id"]]
        for item in selected_materials
        if item["material_id"] in list_by_id
    ]
    write_json_atomically(
        {"materials": ready_materials},
        root / "normalized" / "top-materials-rpa-ready.json",
    )


def _finalize_details(
    root,
    manifest,
    summary,
    manifest_path,
    *,
    highlight_spec=None,
    incomplete_status="partial",
    require_no_unresolved=True,
):
    """Write the shared collection handoff after collection or local highlight recovery."""
    _refresh_ready_materials(root, manifest)
    postprocess_complete = all(
        item.get("postprocessStatus") in {"succeeded", "reused", "not_requested"}
        for item in manifest.get("selectedMaterials", [])
    )
    highlights_complete = True
    if highlight_spec is not None:
        details = []
        incomplete_highlight_ids = set()
        for item in manifest.get("selectedMaterials", []):
            material_id = item.get("material_id")
            path = item.get("detailRecordPath")
            if not isinstance(path, str):
                incomplete_highlight_ids.add(material_id)
                highlights_complete = False
                continue
            try:
                details.append(_load_json(root / path))
            except (OSError, ValueError):
                incomplete_highlight_ids.add(material_id)
                highlights_complete = False
        incomplete_highlight_ids.update(
            detail["material_id"]
            for detail in details_missing_highlights_by_spec(
                details, root, highlight_spec
            )
        )
        if incomplete_highlight_ids:
            highlights_complete = False
            for item in manifest.get("selectedMaterials", []):
                if item.get("material_id") in incomplete_highlight_ids:
                    item["postprocessStatus"] = "failed"
                    item.setdefault(
                        "postprocessError",
                        "configured highlight frames are missing or unavailable",
                    )
        postprocess_complete = all(
            item.get("postprocessStatus") in {"succeeded", "reused"}
            for item in manifest.get("selectedMaterials", [])
        )
    succeeded = (
        not manifest.get("rejectedMaterials")
        and (not require_no_unresolved or not manifest.get("unresolvedMaterials"))
        and _target_satisfied(manifest)
        and postprocess_complete
        and highlights_complete
    )
    manifest["status"] = "success" if succeeded else incomplete_status
    summary["status"] = manifest["status"]
    summary["firstBatchCsvGate"] = manifest.get("firstBatchCsvGate")
    summary["stages"]["details"]["requestStatus"] = (
        "completed" if succeeded else "partial"
    )
    _sync_postprocess_counts(summary, manifest)
    _sync_stage_counts(summary, manifest, root)
    _sync_task_run_field_aliases(manifest)
    summary["taskRuns"] = manifest.get("taskRuns", {})
    write_json_atomically(manifest, manifest_path)
    _save_summary(root, summary)
    if not succeeded:
        set_stage(root, "partial", reason=manifest.get("error") or "collection gate incomplete")
    return 0 if succeeded else 1


def _invoke_detail_result(*, item, shop_id, detail_date_params, invoker):
    """Run only the gateway call in a worker; the scheduler owns all writes."""
    started = time.monotonic()
    try:
        return {
            "item": item,
            "result": invoker.invoke_detail(
                shop_id, _detail_request(item, detail_date_params)
            ),
            "started": started,
            "error": None,
        }
    except Exception as exc:
        return {"item": item, "result": None, "started": started, "error": exc}


def _persist_detail(
    *, root, summary, item, gateway_result, downloader, detail_fetch_sec,
    expected_context=None, on_csv_valid=None,
):
    """Normalize a successful detail response and build its durable manifest entry."""
    material_id = item["material_id"]
    if unpack_gateway_result(gateway_result).kind != "files":
        raise YuntuBatchError("detail response must return a downloadable CSV file")
    raw_detail, detail_source = _materialize_records(
        gateway_result,
        csv_path=root / "raw-csv" / "details" / f"{material_id}.csv",
        json_path=root / "raw-json" / "details" / f"{material_id}.json",
        downloader=downloader,
    )
    validate_detail_csv_records(raw_detail)
    detail_record = normalize_detail_records(
        raw_detail,
        material_id=material_id,
        source_path=_relative(root, detail_source),
        expected_selection_group=_expected_selection_group(item),
        expected_context=expected_context,
    )
    if on_csv_valid is not None:
        on_csv_valid()
    detail_output = root / "normalized" / "detail-records" / f"{material_id}.json"
    write_json_atomically(detail_record, detail_output)
    _record_artifact(
        summary,
        root,
        f"detailRecord:{material_id}",
        detail_output,
        "normalized_detail",
    )
    return detail_record, {
        **_manifest_material(item),
        "listRecordPath": "normalized/list-records.json",
        "detailRecordPath": _relative(root, detail_output),
        "rawDetailPath": _relative(root, detail_source),
        "timingsSec": {"detailFetchSec": detail_fetch_sec},
    }


def _generate_highlight_result(detail, root, highlight_spec=None):
    """Run a material's video download and frame extraction in a worker."""
    started = time.monotonic()
    try:
        return {
            "material_id": detail["material_id"],
            "frames": generate_highlights_for_detail(detail, root, highlight_spec) if highlight_spec is not None else generate_standard_highlights_for_detail(detail, root),
            "duration": _elapsed(started),
            "error": None,
        }
    except Exception as exc:
        return {
            "material_id": detail["material_id"],
            "frames": [],
            "duration": _elapsed(started),
            "error": exc,
        }


def _generate_video_result(detail, root):
    """Persist a report video when no optional highlight frames were requested."""
    started = time.monotonic()
    try:
        persist_video_for_detail(detail, root)
        return {
            "material_id": detail["material_id"],
            "frames": [],
            "duration": _elapsed(started),
            "error": None,
            "video_only": True,
        }
    except Exception as exc:
        return {
            "material_id": detail["material_id"],
            "frames": [],
            "duration": _elapsed(started),
            "error": exc,
            "video_only": True,
        }


def _generate_missing_highlight_result(detail, root, highlight_spec):
    """Run a highlighter-only retry without touching usable local frame assets."""
    started = time.monotonic()
    try:
        return {
            "material_id": detail["material_id"],
            "frames": generate_missing_highlights_for_detail(
                detail, root, highlight_spec
            ),
            "duration": _elapsed(started),
            "error": None,
        }
    except Exception as exc:
        return {
            "material_id": detail["material_id"],
            "frames": [],
            "duration": _elapsed(started),
            "error": exc,
        }


def _next_detail_wave(
    candidates,
    next_index,
    *,
    selected_ids,
    rejected_ids,
    unresolved_ids,
):
    """Select the next bounded wave from the fixed global Top N list."""
    wave = []
    known_ids = selected_ids | rejected_ids | unresolved_ids
    while next_index < len(candidates) and len(wave) < DETAIL_WAVE_CONCURRENCY:
        item = candidates[next_index]
        next_index += 1
        material_id = item["material_id"]
        if material_id in known_ids:
            continue
        wave.append(item)
        known_ids.add(material_id)
    return wave, next_index


def _selected_entry(manifest, material_id):
    return next(
        (
            item
            for item in manifest["selectedMaterials"]
            if item.get("material_id") == material_id
        ),
        None,
    )


def _facts_status(root):
    """Validate fact-package artifacts without rewriting the run directory."""
    manifest_path = root / "facts" / "manifest.json"
    if not manifest_path.is_file():
        return "missing"
    try:
        manifest = _load_json(manifest_path)
        schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
        if not isinstance(schema_version, int) or schema_version < 1:
            return "invalid"
        packages = manifest.get("packages")
        if not isinstance(packages, dict):
            return "invalid"
        for name, package in packages.items():
            if not isinstance(package, dict):
                return "invalid"
            relative_path = package.get("path")
            record_count = package.get("record_count")
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or isinstance(record_count, bool)
                or not isinstance(record_count, int)
                or record_count < 0
            ):
                return "invalid"
            package_path = (root / relative_path).resolve()
            try:
                package_path.relative_to(root.resolve())
            except ValueError:
                return "invalid"
            if not package_path.is_file():
                return "invalid"
            payload = _load_json(package_path)
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != schema_version
                or not isinstance(payload.get("records"), list)
                or len(payload["records"]) != record_count
            ):
                return "invalid"
    except (OSError, ValueError, TypeError):
        return "invalid"
    return "complete"


def _sync_postprocess_counts(summary, manifest):
    detail_stage = summary["stages"]["details"]
    detail_stage["postprocessSubmittedCount"] = sum(
        item.get("postprocessStatus") in {"running", "succeeded", "failed", "reused"}
        for item in manifest["selectedMaterials"]
    )
    detail_stage["postprocessSucceededCount"] = sum(
        item.get("postprocessStatus") in {"succeeded", "reused"}
        for item in manifest["selectedMaterials"]
    )
    detail_stage["postprocessFailedCount"] = sum(
        item.get("postprocessStatus") == "failed"
        for item in manifest["selectedMaterials"]
    )


def _record_highlight_result(manifest, frames_by_material, result):
    """Persist frame availability before deriving the material postprocess state."""
    material_id = result["material_id"]
    entry = _selected_entry(manifest, material_id)
    if entry is None:
        return
    entry["postprocessTimingsSec"] = {"highlightSec": result["duration"]}
    if result.get("video_only"):
        if result["error"] is None:
            entry["videoStatus"] = "available"
            entry.pop("videoError", None)
        else:
            entry["videoStatus"] = "failed"
            entry["videoError"] = str(result["error"])
        entry["postprocessStatus"] = "not_requested"
        return
    if result["frames"]:
        frames_by_material[material_id] = result["frames"]
    if result["error"] is None and all(
        isinstance(frame, dict) and frame.get("available")
        for frame in result["frames"]
    ):
        entry["postprocessStatus"] = "succeeded"
        entry.pop("postprocessError", None)
    else:
        entry["postprocessStatus"] = "failed"
        entry["postprocessError"] = (
            str(result["error"])
            if result["error"] is not None
            else "one or more configured highlight frames are unavailable"
        )


def fetch_batch(
    *,
    shop_id,
    list_params,
    detail_date_params,
    target_top_n,
    candidate_top_n,
    material_list=None,
    output_dir,
    invoker,
    downloader=None,
    highlight_spec=None,
    configuration_fingerprint=None,
):
    target_top_n = _validate_positive_int(target_top_n, "target_top_n")
    candidate_top_n = _validate_positive_int(candidate_top_n, "candidate_top_n")
    if candidate_top_n < target_top_n:
        raise YuntuBatchError("candidate_top_n cannot be smaller than target_top_n")
    if not isinstance(shop_id, str) or not shop_id.strip():
        raise YuntuBatchError("shop_id is required")
    downloader = FileDownloader() if downloader is None else downloader
    material_list = DEFAULT_MATERIAL_LIST if material_list is None else material_list
    registry = default_material_field_registry()
    root = Path(output_dir)
    manifest_path = root / "batch-manifest.json"
    contract = run_state.collection_contract(
        shop_id=shop_id,
        list_params=list_params,
        detail_date_params=detail_date_params,
        target_top_n=target_top_n,
        candidate_top_n=candidate_top_n,
        material_list=material_list,
        configuration_fingerprint=configuration_fingerprint,
    )
    try:
        summary = _open_collection_summary(root, contract)
    except run_state.YuntuRunStateError as exc:
        raise YuntuBatchError(str(exc)) from exc
    if manifest_path.is_file():
        existing = _load_json(manifest_path)
        _restore_first_batch_gate(root, existing, detail_date_params)
        write_json_atomically(existing, manifest_path)
        if existing.get("firstBatchCsvGate", {}).get("status") == "blocked":
            return _pause_for_first_batch(root, existing, summary, manifest_path)
    if get_workflow_status(root)["stage"] in {"not_started", "collecting"}:
        summary = set_stage(root, "collecting")
    manifest = _open_or_new_manifest(
        manifest_path=manifest_path,
        list_params=list_params,
        detail_date_params=detail_date_params,
        target_top_n=target_top_n,
        candidate_top_n=candidate_top_n,
        material_list=material_list,
        summary=summary,
    )
    write_json_atomically(manifest, manifest_path)

    started = time.monotonic()
    try:
        reusable_list = _reusable_list_artifact(root, list_params, detail_date_params)
        resuming_gate = bool(manifest.get("firstBatchCsvGate"))
        if resuming_gate:
            # Contract was checked above. Resume only the existing first-batch
            # tasks; even a new list request would violate the closed gate.
            list_records = _load_json(root / "normalized" / "list-records.json")
            raw_list, list_source = None, None
            summary["stages"]["list"]["requestStatus"] = "reused"
        elif reusable_list is not None:
            raw_list, list_source = parse_csv_records(reusable_list), str(reusable_list)
            summary["stages"]["list"]["requestStatus"] = "reused"
        else:
            summary["stages"]["list"]["requestStatus"] = "requested"
            _save_summary(root, summary)
            raw_list, list_source = _materialize_records(
                invoker.invoke_list(shop_id, list_params),
                csv_path=root / "raw-csv" / "list.csv",
                json_path=root / "raw-json" / "list-response.json",
                downloader=downloader,
            )
        if not resuming_gate:
            list_records = normalize_list_records(raw_list, source_path=_relative(root, list_source))
        manifest["fullListCount"] = len(list_records)
        list_record_path = root / "normalized" / "list-records.json"
        write_json_atomically(list_records, list_record_path)
        _record_artifact(summary, root, "listRecords", list_record_path, "normalized_list")
        summary["stages"]["list"]["normalized"] = True
        if get_workflow_status(root)["stage"] in {"collecting", "normalizing"}:
            _save_summary(root, summary)
            summary = set_stage(root, "normalizing", normalized_count=len(list_records))
        context_verification = _validate_list_context(list_records, list_params)
        summary["stages"]["list"]["contextVerified"] = context_verification["valid"]
        summary["stages"]["list"]["contextIssues"] = context_verification["issues"]
        summary["stages"]["list"]["observedContext"] = context_verification["observed"]
        if not context_verification["valid"]:
            manifest["status"] = "list_context_mismatch"
            manifest["error"] = "list context does not match requested filters: " + ", ".join(
                context_verification["issues"]
            )
            manifest["timingsSec"]["totalSec"] = _elapsed(started)
            summary["status"] = "list_context_mismatch"
            _sync_stage_counts(summary, manifest, root)
            write_json_atomically(manifest, manifest_path)
            _save_summary(root, summary)
            set_stage(root, "failed", reason="list context mismatch")
            return 1
    except Exception as exc:
        manifest["status"] = "list_failed"
        manifest["error"] = str(exc)
        manifest["timingsSec"]["totalSec"] = _elapsed(started)
        summary["status"] = "list_failed"
        summary["stages"]["list"]["requestStatus"] = "failed"
        _sync_stage_counts(summary, manifest, root)
        write_json_atomically(manifest, manifest_path)
        _save_summary(root, summary)
        set_stage(root, "failed", reason="list collection failed")
        return 1

    try:
        matched_records = select_materials(list_records, material_list, registry)
    except MaterialListSelectionError as exc:
        manifest["status"] = "selection_failed"
        manifest["error"] = str(exc)
        summary["status"] = "selection_failed"
        write_json_atomically(manifest, manifest_path)
        _save_summary(root, summary)
        set_stage(root, "failed", reason="local material selection failed")
        return 1
    manifest["materialList"] = material_list
    manifest["matchedMaterialCount"] = len(matched_records)
    candidates = _top_ranked_materials_from_list(
        list_records, target_top_n, material_list, registry
    )
    manifest["candidateMaterials"] = [_manifest_material(item) for item in candidates]
    manifest["selectionGroups"] = []
    _restore_persisted_details(root, manifest, candidates)
    _mark_unresolved_submissions(manifest, candidates)
    detail_requests = [
        _detail_request(item, detail_date_params)
        for item in candidates
    ]
    detail_request_path = root / "normalized" / "detail-request-params.json"
    write_json_atomically(detail_requests, detail_request_path)
    manifest["detailRequestPath"] = _relative(root, detail_request_path)
    _record_artifact(summary, root, "detailRequests", detail_request_path, "detail_request_params")
    if not matched_records:
        manifest["status"] = "failed"
        manifest["error"] = "no_materials_matched"
        manifest["timingsSec"]["totalSec"] = _elapsed(started)
        summary["status"] = "failed"
        summary["stages"]["details"]["requestStatus"] = "not_requested"
        _refresh_ready_materials(root, manifest)
        _sync_stage_counts(summary, manifest, root)
        write_json_atomically(manifest, manifest_path)
        _save_summary(root, summary)
        set_stage(root, "failed", reason="no_materials_matched")
        return 1
    summary["stages"]["details"]["requestStatus"] = "running"
    _sync_stage_counts(summary, manifest, root)
    _save_summary(root, summary)
    selected_ids = _selected_ids(manifest)
    rejected_ids = {
        item.get("material_id")
        for item in manifest["rejectedMaterials"]
        if isinstance(item, dict) and isinstance(item.get("material_id"), str)
    }
    unresolved_ids = {
        item.get("material_id")
        for item in manifest["unresolvedMaterials"]
        if isinstance(item, dict) and isinstance(item.get("material_id"), str)
    }
    async_details = _supports_detail_task_recovery(invoker)
    active_task_ids = {
        item["material_id"]
        for item in candidates
        if _active_detail_task_attempt(manifest, item["material_id"])
    }
    unresolved_ids.update(active_task_ids)
    frames_by_material = load_highlights_manifest(root) if highlight_spec is not None else {}
    pending_details = {}
    pending_highlights = {}
    next_index = 0

    def checkpoint():
        summary["firstBatchCsvGate"] = manifest.get("firstBatchCsvGate")
        _sync_task_run_field_aliases(manifest)
        if highlight_spec is not None:
            write_highlights_manifest(root, frames_by_material, configuration_fingerprint)
        _sync_postprocess_counts(summary, manifest)
        _sync_stage_counts(summary, manifest, root)
        summary["taskRuns"] = manifest.get("taskRuns", {})
        write_json_atomically(manifest, manifest_path)
        _save_summary(root, summary)

    def submit_highlight(executor, detail):
        material_id = detail["material_id"]
        entry = _selected_entry(manifest, material_id)
        if entry is not None:
            entry["postprocessStatus"] = "running"
        worker = _generate_highlight_result if highlight_spec is not None else _generate_video_result
        future = executor.submit(worker, detail, root, highlight_spec) if highlight_spec is not None else executor.submit(worker, detail, root)
        pending_highlights[future] = material_id

    # Resume never repeats a persisted detail. Missing frames are a separate job.
    restored_details = []
    for selected in manifest["selectedMaterials"]:
        path = selected.get("detailRecordPath")
        if not isinstance(path, str):
            continue
        try:
            restored_details.append(_load_json(root / path))
        except (OSError, ValueError):
            continue

    with ThreadPoolExecutor(max_workers=DETAIL_WAVE_CONCURRENCY) as detail_executor, ThreadPoolExecutor(
        max_workers=HIGHLIGHT_CONCURRENCY
    ) as highlight_executor:
        if async_details:
            # Recovery is deliberately driven only by taskRuns. The persisted
            # task id is the authority; submittedMaterials alone is not enough
            # to justify another remote submission.
            for item in candidates:
                gate = manifest.get("firstBatchCsvGate", {})
                if item["material_id"] in selected_ids or (
                    gate.get("status") != "passed" and item["material_id"] not in gate.get("materialIds", [])
                ):
                    continue
                attempt = _active_detail_task_attempt(manifest, item["material_id"])
                if attempt is None:
                    continue
                future = detail_executor.submit(
                    _wait_detail_task_result,
                    item=item,
                    task_group_id=attempt["task_group_id"],
                    invoker=invoker,
                )
                pending_details[future] = item["material_id"]

        missing_details = details_missing_highlights_by_spec(restored_details, root, highlight_spec) if highlight_spec is not None else restored_details
        missing_highlight_ids = {
            detail["material_id"] for detail in missing_details
        }
        for detail in restored_details:
            if detail["material_id"] not in missing_highlight_ids:
                entry = _selected_entry(manifest, detail["material_id"])
                if entry is not None:
                    entry["postprocessStatus"] = "reused" if highlight_spec is not None else "not_requested"
        for detail in missing_details:
            submit_highlight(highlight_executor, detail)

        while True:
            if not pending_details and not pending_highlights:
                gate = _update_first_batch_gate(manifest, drained=True)
                if gate and gate["status"] in {"blocked", "unconfirmed"}:
                    checkpoint()
                    break
                wave, next_index = _next_detail_wave(
                    candidates,
                    next_index,
                    selected_ids=selected_ids,
                    rejected_ids=rejected_ids,
                    unresolved_ids=unresolved_ids,
                )
                if not wave:
                    break
                if not manifest.get("firstBatchCsvGate"):
                    manifest["firstBatchCsvGate"] = {
                        "status": "pending", "functionCode": detail_dynamic_rpa.CONNECTOR_CODE,
                        "materialIds": [item["material_id"] for item in wave],
                        "outcomes": {}, "validCsvCount": 0,
                    }
                    checkpoint()
                for item in wave:
                    manifest["submittedMaterials"].append(_manifest_material(item))
                    if async_details:
                        # Record the attempt before submitting so that a
                        # checkpoint exists even if submission itself fails.
                        attempt = _new_detail_attempt(
                            manifest, item, detail_date_params
                        )
                        checkpoint()
                        try:
                            task_group_id = invoker.submit_detail(
                                shop_id, _detail_request(item, detail_date_params)
                            )
                        except Exception as exc:
                            _set_first_batch_outcome(manifest, item["material_id"], "unconfirmed")
                            task_run = _task_run(manifest, item, detail_date_params)
                            attempt.update({
                                "request_status": "unresolved",
                                "last_observed_at": _now_iso(),
                                "error_class": "transport",
                                "errors": [str(exc)],
                            })
                            task_run.update({"status": "unresolved", "error_class": "transport"})
                            unresolved_ids.add(item["material_id"])
                            manifest["unresolvedMaterials"].append({
                                **_manifest_material(item),
                                "reason": "submit_unconfirmed",
                                "error": str(exc),
                                "error_class": "transport",
                            })
                            continue
                        if not isinstance(task_group_id, str) or not task_group_id.strip():
                            _set_first_batch_outcome(manifest, item["material_id"], "unconfirmed")
                            error_text = "detail task submission returned an empty task_group_id"
                            attempt.update({
                                "request_status": "unresolved",
                                "last_observed_at": _now_iso(),
                                "error_class": "protocol",
                                "errors": [error_text],
                            })
                            _task_run(manifest, item, detail_date_params).update({
                                "status": "unresolved", "error_class": "protocol"
                            })
                            unresolved_ids.add(item["material_id"])
                            manifest["unresolvedMaterials"].append({
                                **_manifest_material(item),
                                "reason": "submit_unconfirmed",
                                "error": error_text,
                                "error_class": "protocol",
                            })
                            continue
                        attempt.update({
                            "task_group_id": task_group_id.strip(),
                            "request_status": "submitted",
                            "last_observed_at": _now_iso(),
                        })
                        _task_run(manifest, item, detail_date_params).update({
                            "task_group_id": task_group_id.strip(),
                            "status": "submitted",
                        })
                        checkpoint()
                        future = detail_executor.submit(
                            _wait_detail_task_result,
                            item=item,
                            task_group_id=task_group_id.strip(),
                            invoker=invoker,
                        )
                    else:
                        future = detail_executor.submit(
                            _invoke_detail_result,
                            item=item,
                            shop_id=shop_id,
                            detail_date_params=detail_date_params,
                            invoker=invoker,
                        )
                    pending_details[future] = item["material_id"]
                checkpoint()
                if not pending_details and not pending_highlights:
                    continue

            done, _ = wait(
                set(pending_details) | set(pending_highlights),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                if future in pending_details:
                    result = future.result()
                    pending_details.pop(future)
                    item = result["item"]
                    material_id = item["material_id"]
                    task_attempt = _active_detail_task_attempt(manifest, material_id)
                    try:
                        if result["error"] is not None:
                            if task_attempt is not None:
                                error_text = str(result["error"])
                                error_class = _detail_wait_error_class(result["error"])
                                if error_class != "remote_failed":
                                    _set_first_batch_outcome(manifest, material_id, "unconfirmed")
                                    task_attempt.update({
                                        "request_status": "unresolved",
                                        "remote_status": task_attempt.get("remote_status", "submitted"),
                                        "last_observed_at": _now_iso(),
                                        "error_class": error_class,
                                        "errors": task_attempt.get("errors", []) + [error_text],
                                    })
                                    _task_run(manifest, item, detail_date_params).update({
                                        "status": "unresolved",
                                        "error_class": error_class,
                                    })
                                    unresolved_ids.add(material_id)
                                    if not any(
                                        entry.get("material_id") == material_id
                                        for entry in manifest["unresolvedMaterials"]
                                        if isinstance(entry, dict)
                                    ):
                                        manifest["unresolvedMaterials"].append({
                                            **_manifest_material(item),
                                            "reason": "poll_unconfirmed",
                                            "error": error_text,
                                            "error_class": error_class,
                                        })
                                    continue
                            raise result["error"]
                        response = result["result"]
                        if task_attempt is None and isinstance(response, dict) and "status" in response:
                            if str(response["status"]).casefold() not in {"completed", "partial_success", "failed"}:
                                _set_first_batch_outcome(manifest, material_id, "unconfirmed")
                                unresolved_ids.add(material_id)
                                manifest["unresolvedMaterials"].append({
                                    **_manifest_material(item), "reason": "poll_unconfirmed",
                                })
                                continue
                        if task_attempt is not None:
                            terminal_status = str(
                                result["result"].get("status") or ""
                            ).casefold()
                            if terminal_status == "failed":
                                raise YuntuBatchError(
                                    f"RPA task failed: {material_id}"
                                )
                            if terminal_status not in {"completed", "partial_success"}:
                                _set_first_batch_outcome(manifest, material_id, "unconfirmed")
                                task_attempt.update(request_status="unresolved", remote_status="pending")
                                _task_run(manifest, item, detail_date_params)["status"] = "unresolved"
                                unresolved_ids.add(material_id)
                                _remove_material_from_bucket(manifest, "unresolvedMaterials", material_id)
                                manifest["unresolvedMaterials"].append({
                                    **_manifest_material(item), "reason": "poll_unconfirmed",
                                    "error": f"unconfirmed remote status: {terminal_status}",
                                })
                                continue
                            task_run = _task_run(manifest, item, detail_date_params)
                            task_attempt.update({
                                "request_status": "completed",
                                "remote_status": result["result"].get("status", "completed"),
                                "last_observed_at": _now_iso(),
                            })
                            _task_run(manifest, item, detail_date_params).update({
                                "status": "completed",
                                "error_class": None,
                            })
                        detail_record, selected_entry = _persist_detail(
                            root=root,
                            summary=summary,
                            item=item,
                            gateway_result=result["result"],
                            downloader=downloader,
                            detail_fetch_sec=_elapsed(result["started"]),
                            expected_context=detail_date_params,
                            on_csv_valid=lambda: _set_first_batch_outcome(manifest, material_id, "valid_csv"),
                        )
                        if task_attempt is not None:
                            task_run = _task_run(manifest, item, detail_date_params)
                            task_attempt.update({
                                "artifact_status": "normalized",
                                "artifact_paths": [
                                    selected_entry["rawDetailPath"],
                                    selected_entry["detailRecordPath"],
                                ],
                                "last_observed_at": _now_iso(),
                            })
                            task_run["status"] = "persisted"
                        selected_entry["postprocessStatus"] = "pending" if highlight_spec is not None else "not_requested"
                        manifest["selectedMaterials"].append(selected_entry)
                        selected_ids.add(material_id)
                        _remove_material_from_bucket(manifest, "unresolvedMaterials", material_id)
                        unresolved_ids.discard(material_id)
                        submit_highlight(highlight_executor, detail_record)
                    except Exception as exc:
                        if (task_attempt is None and result["error"] is not None
                                and _detail_wait_error_class(exc) != "remote_failed"):
                            _set_first_batch_outcome(manifest, material_id, "unconfirmed")
                            unresolved_ids.add(material_id)
                            manifest["unresolvedMaterials"].append({
                                **_manifest_material(item), "reason": "poll_unconfirmed", "error": str(exc),
                            })
                            continue
                        _set_first_batch_outcome(manifest, material_id, "no_valid_csv")
                        rejected_ids.add(material_id)
                        if task_attempt is not None:
                            error_text = str(exc)
                            task_run = _task_run(manifest, item, detail_date_params)
                            task_attempt.update({
                                "request_status": "failed",
                                "remote_status": "failed",
                                "artifact_status": "failed",
                                "last_observed_at": _now_iso(),
                                "error_class": (
                                    "remote_failed" if "RPA task failed" in error_text
                                    else _detail_artifact_error_class(exc)
                                ),
                                "errors": task_attempt.get("errors", []) + [error_text],
                            })
                            task_run.update({
                                "status": "failed",
                                "error_class": task_attempt["error_class"],
                            })
                        manifest["rejectedMaterials"].append(
                            {
                                **_manifest_material(item),
                                "reason": exc.__class__.__name__,
                                "error": str(exc),
                                "timingsSec": {"detailFetchSec": _elapsed(result["started"])},
                            }
                        )
                else:
                    result = future.result()
                    pending_highlights.pop(future)
                    _record_highlight_result(manifest, frames_by_material, result)
            checkpoint()

    if manifest.get("firstBatchCsvGate", {}).get("status") in {"blocked", "unconfirmed"}:
        return _pause_for_first_batch(root, manifest, summary, manifest_path)
    manifest["selectedMaterials"] = sorted(
        manifest["selectedMaterials"], key=lambda item: item.get("rank", 0)
    )
    manifest["timingsSec"]["totalSec"] = _elapsed(started)
    return _finalize_details(
        root,
        manifest,
        summary,
        manifest_path,
        highlight_spec=highlight_spec,
        require_no_unresolved=False,
    )


def collection_status(*, output_dir):
    """Read collection progress from state and landed artifacts without mutating them."""
    root = Path(output_dir)
    summary_path = root / run_state.RUN_SUMMARY_NAME
    manifest_path = root / "batch-manifest.json"
    try:
        summary = run_state.load_run_state(summary_path) if summary_path.is_file() else {}
    except run_state.YuntuRunStateError:
        summary = {"status": "state_unreadable"}
    try:
        manifest = _load_json(manifest_path) if manifest_path.is_file() else {}
    except (OSError, ValueError):
        manifest = {"status": "manifest_unreadable"}

    def _records(path):
        try:
            records = _load_json(path)
        except (OSError, ValueError):
            return []
        return records if isinstance(records, list) else []

    list_records = _records(root / "normalized" / "list-records.json")
    detail_requests = _records(root / "normalized" / "detail-request-params.json")
    detail_dir = root / "normalized" / "detail-records"
    persisted_details = len(list(detail_dir.glob("*.json"))) if detail_dir.is_dir() else 0
    stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
    list_stage = stages.get("list") if isinstance(stages.get("list"), dict) else {}
    detail_stage = stages.get("details") if isinstance(stages.get("details"), dict) else {}
    workflow_status = get_workflow_status(root)
    return {
        "status": workflow_status["status"],
        "collection_status": summary.get("status") or manifest.get("status") or "not_started",
        "ready_for_analysis": workflow_status["ready_for_analysis"],
        "workflow": workflow_status,
        "facts_status": _facts_status(root),
        "runId": summary.get("runId") or manifest.get("runId"),
        "parameterFingerprint": summary.get("parameterFingerprint"),
        "collectionContract": summary.get("collectionContract"),
        "taskRuns": manifest.get("taskRuns", {}),
        "firstBatchCsvGate": manifest.get("firstBatchCsvGate"),
        "rpa_paused": manifest.get("firstBatchCsvGate", {}).get("status") in {"blocked", "unconfirmed"},
        "stages": {
            "list": {
                "requestStatus": list_stage.get("requestStatus", "unknown"),
                "normalizedRecordCount": len(list_records),
                "contextVerified": list_stage.get("contextVerified"),
                "contextIssues": list_stage.get("contextIssues", []),
            },
            "details": {
                "requestStatus": detail_stage.get("requestStatus", "unknown"),
                "requestParamCount": len(detail_requests),
                "fullListCount": manifest.get("fullListCount", 0),
                "matchedMaterialCount": manifest.get("matchedMaterialCount", 0),
                "submittedCount": len(manifest.get("submittedMaterials", [])),
                "taskRunCount": len(manifest.get("taskRuns", {})),
                "persistedRecordCount": persisted_details,
                "selectedCount": len(manifest.get("selectedMaterials", [])),
                "rejectedCount": len(manifest.get("rejectedMaterials", [])),
                "unresolvedCount": len(manifest.get("unresolvedMaterials", [])),
                "cancelledCount": len(manifest.get("cancelledMaterials", [])),
                "postprocessSubmittedCount": detail_stage.get("postprocessSubmittedCount", 0),
                "postprocessSucceededCount": detail_stage.get("postprocessSucceededCount", 0),
                "postprocessFailedCount": detail_stage.get("postprocessFailedCount", 0),
            },
        },
        "evidencePaths": {
            "runSummary": str(summary_path) if summary_path.is_file() else None,
            "batchManifest": str(manifest_path) if manifest_path.is_file() else None,
            "listRecords": str(root / "normalized" / "list-records.json")
            if list_records
            else None,
            "detailRequests": str(root / "normalized" / "detail-request-params.json")
            if detail_requests
            else None,
            "detailRecordsDir": str(detail_dir) if detail_dir.is_dir() else None,
        },
    }








def retry_highlights_batch(*, output_dir, highlight_spec):
    """Retry only missing configured highlight assets from persisted details."""
    root = Path(output_dir)
    manifest_path = root / "batch-manifest.json"
    summary_path = root / run_state.RUN_SUMMARY_NAME
    if not summary_path.is_file():
        raise YuntuBatchError(
            "output directory contains an unverifiable existing batch; choose a new output directory"
        )
    try:
        summary = run_state.load_run_state(summary_path)
    except run_state.YuntuRunStateError as exc:
        raise YuntuBatchError(
            "output directory contains an unverifiable existing batch; choose a new output directory"
        ) from exc
    contract = summary.get("collectionContract")
    if not isinstance(contract, dict) or not run_state.collection_contract_matches(
        summary, contract
    ):
        raise YuntuBatchError(
            "output directory contains an unverifiable existing batch; choose a new output directory"
        )
    try:
        manifest = _load_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise YuntuBatchError("existing batch manifest is unreadable") from exc
    if manifest.get("firstBatchCsvGate", {}).get("status") in {"blocked", "unconfirmed"}:
        return _pause_for_first_batch(root, manifest, summary, manifest_path)
    summary = set_stage(root, "collecting", recovery="retry_highlights")

    details = []
    for item in manifest.get("selectedMaterials", []):
        path = item.get("detailRecordPath")
        if not isinstance(path, str):
            item["postprocessStatus"] = "failed"
            item["postprocessError"] = "persisted detail record path is missing"
            continue
        try:
            details.append(_load_json(root / path))
        except (OSError, ValueError):
            item["postprocessStatus"] = "failed"
            item["postprocessError"] = "persisted detail record is unreadable"

    missing_details = details_missing_highlights_by_spec(details, root, highlight_spec)
    missing_ids = {detail["material_id"] for detail in missing_details}
    for detail in details:
        if detail["material_id"] not in missing_ids:
            entry = _selected_entry(manifest, detail["material_id"])
            if entry is not None:
                entry["postprocessStatus"] = "reused"
                entry.pop("postprocessError", None)

    frames_by_material = load_highlights_manifest(root)
    with ThreadPoolExecutor(max_workers=HIGHLIGHT_CONCURRENCY) as highlight_executor:
        pending_highlights = {}
        for detail in missing_details:
            entry = _selected_entry(manifest, detail["material_id"])
            if entry is not None:
                entry["postprocessStatus"] = "running"
            pending_highlights[
                highlight_executor.submit(
                    _generate_missing_highlight_result, detail, root, highlight_spec
                )
            ] = detail["material_id"]
        for future in pending_highlights:
            _record_highlight_result(
                manifest, frames_by_material, future.result()
            )

    write_highlights_manifest(
        root, frames_by_material, contract.get("configurationFingerprint")
    )
    _sync_postprocess_counts(summary, manifest)
    manifest["selectedMaterials"] = sorted(
        manifest.get("selectedMaterials", []), key=lambda item: item.get("rank", 0)
    )
    return _finalize_details(
        root,
        manifest,
        summary,
        manifest_path,
        highlight_spec=highlight_spec,
    )


def _relative(root, path):
    return str(Path(path).resolve().relative_to(Path(root).resolve()))
