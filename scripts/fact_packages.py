"""Build auditable, source-only fact packages from a Yuntu run directory."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

try:
    from .brand_classifier import BRAND_RULE_VERSION, UNKNOWN_BRAND, classify_brand
except ImportError:  # pragma: no cover - direct script imports
    from brand_classifier import BRAND_RULE_VERSION, UNKNOWN_BRAND, classify_brand

try:
    from .rpa_io import write_json_atomically
except ImportError:  # pragma: no cover - direct script imports
    from rpa_io import write_json_atomically

try:
    from .workflow_state import get_workflow_status, set_stage
except ImportError:  # pragma: no cover - direct script imports
    from workflow_state import get_workflow_status, set_stage


SCHEMA_VERSION = 3
PACKAGE_NAMES = ("material_ranking", "audience", "content_tags", "script", "interaction_series", "highlight_frames")
AGE_PATTERN = re.compile(r"^(?:\d{2}-\d{2}|\d+\+)$")
CROWD_GROUPS = {
    "genz",
    "小镇中老年",
    "小镇青年",
    "新锐白领",
    "精致妈妈",
    "资深中产",
    "都市蓝领",
    "都市银发",
}


class FactPackageError(ValueError):
    """Raised when a run cannot be converted into consistent fact packages."""


def extract_runtime_brands(records: list[dict]) -> list[str]:
    """Extract the current run's explicit brand scope without widening it."""

    result: list[str] = []
    seen: set[str] = set()
    for record in records:
        context = record.get("list_context") if isinstance(record, dict) else None
        candidates = None
        if isinstance(context, dict) and "brands" in context:
            candidates = context.get("brands")
        elif isinstance(context, dict) and isinstance(context.get("pre_input"), dict):
            candidates = context["pre_input"].get("brands")
        elif isinstance(record, dict) and isinstance(record.get("pre_input"), dict):
            candidates = record["pre_input"].get("brands")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return result


def _classification_status(classification: dict, brands: list[str]) -> tuple[str, str]:
    if classification.get("value") != UNKNOWN_BRAND:
        return "classified", "runtime_title_match"
    if not brands:
        return "unclassified", "runtime_brand_scope_missing"
    return "unclassified", "no_runtime_brand_match"


def _add_brand_fields(record: dict, classification: dict, brands: list[str]) -> dict:
    item = dict(record)
    item["brand_classification"] = classification
    item["brand_rule_version"] = BRAND_RULE_VERSION
    status, reason = _classification_status(classification, brands)
    item["classification_status"] = status
    item["classification_reason"] = reason
    return item


def _read_json(path: Path, label: str):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise FactPackageError(f"unable to read {label}: {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _source_paths(root: Path, *, normalized: str, raw: str | None = None) -> dict:
    paths = {"normalized": normalized}
    if raw:
        paths["raw"] = raw
    return paths


def _detail_id(path: Path, detail: dict) -> str:
    value = detail.get("material_id")
    if value is None or str(value).strip() == "":
        raise FactPackageError(f"detail record is missing material_id: {path}")
    material_id = str(value).strip()
    if path.stem != material_id:
        raise FactPackageError(
            f"detail material_id conflicts with filename: {path.name} vs {material_id}"
        )
    return material_id


def _audience_dimension(tag: str) -> str:
    if AGE_PATTERN.fullmatch(tag):
        return "age"
    if tag in {"男", "女"}:
        return "gender"
    if tag in CROWD_GROUPS:
        return "crowd_group"
    return "unclassified"


def _load_details(root: Path) -> dict[str, dict]:
    directory = root / "normalized" / "detail-records"
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise FactPackageError(f"detail-records is not a directory: {directory}")
    details = {}
    for path in sorted(directory.glob("*.json")):
        detail = _read_json(path, "detail record")
        if not isinstance(detail, dict):
            raise FactPackageError(f"detail record must be an object: {path}")
        material_id = _detail_id(path, detail)
        if material_id in details:
            raise FactPackageError(f"duplicate detail material_id: {material_id}")
        details[material_id] = detail
    return details


def _load_highlights(root: Path) -> dict[str, dict]:
    path = root / "highlights-manifest.json"
    if not path.exists():
        return {}
    payload = _read_json(path, "highlights manifest")
    materials = payload.get("materials") if isinstance(payload, dict) else None
    if materials is None:
        raise FactPackageError("highlights manifest materials must be an array")
    if not isinstance(materials, list):
        raise FactPackageError("highlights manifest materials must be an array")
    result = {}
    for item in materials:
        if not isinstance(item, dict) or not str(item.get("material_id", "")).strip():
            raise FactPackageError("highlight material must contain material_id")
        material_id = str(item["material_id"]).strip()
        if material_id in result:
            raise FactPackageError(f"duplicate highlight material_id: {material_id}")
        result[material_id] = item
    return result


def _ranking_payload(root: Path, records: list[dict], brands: list[str] | None = None) -> dict:
    runtime_brands = extract_runtime_brands(records) if brands is None else list(brands)
    ordered = []
    for record in sorted(records, key=lambda item: item.get("rank", 0)):
        item = dict(record)
        item.pop("people_tags", None)
        classification = classify_brand(item.get("title", ""), brands=runtime_brands)
        item = _add_brand_fields(item, classification, runtime_brands)
        item["source_paths"] = _source_paths(
            root,
            normalized="normalized/list-records.json",
            raw="raw-csv/list.csv",
        )
        ordered.append(item)
    return {"schema_version": SCHEMA_VERSION, "records": ordered}


def _audience_payload(root: Path, records: list[dict], classifications: dict[str, dict], brands: list[str]) -> dict:
    ordered = []
    for record in sorted(records, key=lambda item: item.get("rank", 0)):
        raw_tags = record.get("people_tags")
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        observations = [
            {"tag": tag, "dimension": _audience_dimension(tag)} for tag in tags
        ]
        classification = classifications.get(
            str(record.get("material_id")),
            classify_brand("", brands=brands),
        )
        ordered.append(_add_brand_fields({
            "material_id": record.get("material_id"),
            "rank": record.get("rank"),
            "observations": observations,
            "raw_people_tags": tags,
            "data_availability": record.get("data_availability", {}).get("people_tags", {}),
            "source_paths": _source_paths(
                root,
                normalized="normalized/list-records.json",
                raw="raw-csv/list.csv",
            ),
        }, classification, brands))
    return {"schema_version": SCHEMA_VERSION, "records": ordered}


def _script_payload(root: Path, details: dict[str, dict], classifications: dict[str, dict], brands: list[str]) -> dict:
    records = []
    asr_manifest_path = root / "asr" / "manifest.json"
    asr_manifest = _read_json(asr_manifest_path, "ASR manifest") if asr_manifest_path.is_file() else {}
    asr_entries = {str(item.get("materialId")): item for item in asr_manifest.get("materials", []) if isinstance(item, dict)}
    for material_id in sorted(details):
        detail = details[material_id]
        detail_source = detail.get("source_paths", {}).get(
            "detail", f"raw-csv/details/{material_id}.csv"
        )
        availability = detail.get("data_availability", {})
        classification = classifications.get(material_id, classify_brand("", brands=brands))
        platform_text = str(detail.get("script_text") or "")
        asr_path = root / "asr" / "materials" / f"{material_id}.json"
        asr = _read_json(asr_path, "ASR transcript") if asr_path.is_file() else None
        if asr is not None and (asr_entries.get(material_id, {}).get("asrPath") != f"asr/materials/{material_id}.json"
                                or asr_entries[material_id].get("asrSha256") != _sha256(asr_path)):
            raise FactPackageError(f"ASR receipt hash mismatch: {material_id}")
        if asr is not None and str(asr.get("materialId")) != material_id:
            raise FactPackageError(f"ASR material identity mismatch: {material_id}")
        if asr is not None and asr.get("attempts"):
            attempts = asr["attempts"]
            if not isinstance(attempts, list) or attempts != asr_entries.get(material_id, {}).get("attempts"):
                raise FactPackageError(f"ASR attempt receipt mismatch: {material_id}")
            for attempt in attempts:
                relative = attempt.get("path") if isinstance(attempt, dict) else None
                if not isinstance(relative, str) or not relative.startswith("asr/attempts/"):
                    raise FactPackageError(f"ASR attempt path invalid: {material_id}")
                attempt_path = (root / relative).resolve()
                if not attempt_path.is_relative_to(root.resolve()) or not attempt_path.is_file() or _sha256(attempt_path) != attempt.get("sha256"):
                    raise FactPackageError(f"ASR attempt hash mismatch: {material_id}")
        if asr is not None and asr.get("status") == "available":
            video_path = root / "videos" / material_id / "source-video.mp4"
            if not video_path.is_file() or _sha256(video_path) != asr.get("videoSha256"):
                raise FactPackageError(f"ASR source video hash mismatch: {material_id}")
        asr_text = str(asr.get("text") or "") if asr and asr.get("status") == "available" else ""
        script_text = platform_text or asr_text
        script_source = "platform" if platform_text else "asr" if asr_text else "unavailable"
        source_paths = _source_paths(
            root, normalized=f"normalized/detail-records/{material_id}.json", raw=detail_source,
        )
        if asr is not None:
            source_paths["asr"] = f"asr/materials/{material_id}.json"
        records.append(_add_brand_fields({
            "material_id": material_id,
            "date_range": detail.get("date_range"),
            "audience_portrait": detail.get("audience_portrait", {}),
            "script_text": script_text,
            "platform_script_text": platform_text,
            "asr_transcript": asr if asr is not None else None,
            "script_source": script_source,
            "content_formula": detail.get("content_formula"),
            "creative_breakdown": detail.get("creative_breakdown"),
            "script_segments": detail.get("script_segments", []),
            "data_availability": {
                key: availability.get(key, {"available": False, "missing_reason": "source_empty"})
                for key in (
                    "script_text", "content_formula", "creative_breakdown",
                    "audience_portrait", "script_segments",
                )
            } | {"script_text": {"available": bool(script_text), "missing_reason": "" if script_text else "source_empty"}},
            "source_paths": source_paths,
        }, classification, brands))
    return {"schema_version": SCHEMA_VERSION, "records": records}


def _content_tags_payload(root: Path, records: list[dict], classifications: dict[str, dict], brands: list[str]) -> dict:
    result = []
    for record in sorted(records, key=lambda item: item.get("rank", 0)):
        material_id = str(record.get("material_id"))
        result.append(_add_brand_fields({
            "material_id": material_id,
            "platform_content_tags": record.get("material_tags", []),
            "data_availability": {"material_tags": record.get("data_availability", {}).get("material_tags", {"available": False, "missing_reason": "source_empty"})},
            "source_paths": _source_paths(root, normalized="normalized/list-records.json", raw="raw-csv/list.csv"),
        }, classifications.get(material_id, classify_brand("", brands=brands)), brands))
    return {"schema_version": SCHEMA_VERSION, "records": result}


def _interaction_payload(root: Path, details: dict[str, dict], classifications: dict[str, dict], brands: list[str]) -> dict:
    result = []
    for material_id, detail in sorted(details.items()):
        detail_source = detail.get("source_paths", {}).get(
            "detail", f"raw-csv/details/{material_id}.csv"
        )
        result.append(_add_brand_fields({
            "material_id": material_id,
            "date_range": detail.get("date_range"),
            "core_metrics": detail.get("core_metrics", {}),
            "trend_series": detail.get("trend_series", []),
            "high_points": detail.get("high_points", []),
            "loss_points": detail.get("loss_points", []),
            "data_availability": detail.get("data_availability", {}),
            "source_paths": _source_paths(root, normalized=f"normalized/detail-records/{material_id}.json", raw=detail_source),
        }, classifications.get(material_id, classify_brand("", brands=brands)), brands))
    return {"schema_version": SCHEMA_VERSION, "records": result}


def _highlight_payload(root: Path, details: dict[str, dict], highlights: dict[str, dict], classifications: dict[str, dict], brands: list[str]) -> dict:
    records = []
    for material_id in sorted(set(details) | set(highlights)):
        detail = details.get(material_id, {})
        manifest_item = highlights.get(material_id, {})
        frames = manifest_item.get("frames", [])
        if not isinstance(frames, list):
            raise FactPackageError(f"highlight frames must be an array: {material_id}")
        classification = classifications.get(material_id, classify_brand("", brands=brands))
        records.append(_add_brand_fields({
            "material_id": material_id,
            "date_range": detail.get("date_range"),
            "frames": frames,
            "data_availability": detail.get("data_availability", {}),
            "source_paths": {
                "detail_normalized": f"normalized/detail-records/{material_id}.json",
                "detail_raw": f"raw-csv/details/{material_id}.csv",
                "manifest": "highlights-manifest.json",
            },
        }, classification, brands))
    return {"schema_version": SCHEMA_VERSION, "records": records}


def _source_artifacts(root: Path, details: dict[str, dict]) -> list[dict]:
    candidates = [root / "normalized/list-records.json"]
    candidates.extend(root / "normalized/detail-records" / f"{mid}.json" for mid in sorted(details))
    candidates.append(root / "highlights-manifest.json")
    candidates.append(root / "asr" / "manifest.json")
    candidates.extend((root / "asr" / "materials").glob("*.json"))
    candidates.extend((root / "asr" / "attempts").glob("*/*.json"))
    candidates.extend(root / "videos" / mid / "source-video.mp4" for mid in sorted(details)
                      if (root / "asr" / "materials" / f"{mid}.json").is_file())
    artifacts = []
    for path in candidates:
        if not path.is_file():
            continue
        artifacts.append({
            "path": _relative(path, root),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return artifacts


def _manifest(
    root: Path,
    details: dict[str, dict],
    highlights: dict[str, dict],
    payloads: dict[str, dict],
    brands: list[str],
) -> dict:
    available_frames = sum(
        1
        for item in highlights.values()
        for frame in item.get("frames", [])
        if isinstance(frame, dict) and frame.get("available") is True
    )
    run_summary = root / "run-summary.json"
    run_id = None
    if run_summary.is_file():
        summary = _read_json(run_summary, "run summary")
        if isinstance(summary, dict):
            run_id = summary.get("runId") or summary.get("run_id")
    filenames = {
        "material_ranking": "material-ranking.json",
        "audience": "audience.json",
        "content_tags": "content-tags.json",
        "script": "script.json",
        "interaction_series": "interaction-series.json",
        "highlight_frames": "highlight-frames.json",
    }
    classifications = payloads["material_ranking"]["records"]
    brand_counts = {brand: 0 for brand in brands}
    unknown_count = 0
    for record in classifications:
        matched = record.get("brand_classification", {}).get("matchedBrands", [])
        if not matched:
            unknown_count += 1
        for brand in matched:
            brand_counts[brand] = brand_counts.get(brand, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_root": ".",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "classification": {"packages": list(payloads), "principle": "source_facts_and_optional_derived_assets"},
        "brand_classification": {
            "rule_version": BRAND_RULE_VERSION,
            "candidate_brands": list(brands),
            "counts": brand_counts,
            "unknown_count": unknown_count,
            "total_records": len(classifications),
        },
        "coverage": {
            "ranking_records": len(payloads["material_ranking"]["records"]),
            "detail_records": len(details),
            "script_records": len(payloads["script"]["records"]),
            "highlight_materials": len(highlights),
            "available_highlight_frames": available_frames,
        },
        "packages": {
            name: {
                "path": f"facts/{filenames[name]}",
                "record_count": len(payloads[name]["records"]),
                "material_ids": sorted({
                    str(record.get("material_id"))
                    for record in payloads[name]["records"]
                    if isinstance(record, dict) and record.get("material_id") is not None
                }),
                "source_paths": {
                    "material_ranking": ["normalized/list-records.json", "raw-csv/list.csv"],
                    "audience": ["normalized/list-records.json", "raw-csv/list.csv"],
                    "content_tags": ["normalized/list-records.json", "raw-csv/list.csv"],
                    "script": ["normalized/detail-records/", "raw-csv/details/", "asr/materials/", "videos/"],
                    "interaction_series": ["normalized/detail-records/", "raw-csv/details/"],
                    "highlight_frames": ["normalized/detail-records/", "raw-csv/details/", "highlights-manifest.json"],
                }[name],
            }
            for name in payloads
        },
        "source_artifacts": _source_artifacts(root, details),
    }


def _collection_succeeded(root: Path) -> bool:
    """Return whether the persisted collection gate permits a ready handoff."""
    statuses = []
    for path in (root / "run-summary.json", root / "batch-manifest.json"):
        if not path.is_file():
            continue
        try:
            payload = _read_json(path, "collection state")
        except FactPackageError:
            continue
        if isinstance(payload, dict):
            statuses.extend([
                payload.get("status"),
                payload.get("collectionStatus"),
            ])
    return not any(status in {"partial", "failed", "list_failed", "list_context_mismatch"} for status in statuses)


def _legacy_complete_run(root: Path) -> bool:
    summary_path = root / "run-summary.json"
    facts_manifest = root / "facts" / "manifest.json"
    if not summary_path.is_file() or not facts_manifest.is_file():
        return False
    try:
        summary = _read_json(summary_path, "run summary")
        return (
            isinstance(summary, dict)
            and summary.get("status") == "success"
            and not isinstance(summary.get("workflow"), dict)
        )
    except FactPackageError:
        return False


def _build_fact_packages(run_dir: str | Path, *, output_dir: str | Path | None = None) -> dict:
    """Read a Yuntu run and atomically write four auditable fact packages."""
    root = Path(run_dir)
    if not root.is_dir():
        raise FactPackageError(f"run directory does not exist: {root}")
    list_path = root / "normalized" / "list-records.json"
    if not list_path.is_file():
        raise FactPackageError(f"required normalized list is missing: {list_path}")
    collection_succeeded = _collection_succeeded(root)
    legacy_complete = _legacy_complete_run(root)
    current_stage = get_workflow_status(root)["stage"]
    active_analysis = current_stage in {"analysis_running", "analysis_completed"}
    if collection_succeeded and not legacy_complete and not active_analysis and current_stage != "ready_for_analysis":
        set_stage(root, "fact_packaging")
    records = _read_json(list_path, "normalized list")
    if not isinstance(records, list):
        raise FactPackageError("normalized list records must be an array")
    if any(not isinstance(record, dict) for record in records):
        raise FactPackageError("normalized list records must contain objects")
    details = _load_details(root)
    highlights = _load_highlights(root)
    runtime_brands = extract_runtime_brands(records)
    if collection_succeeded and not legacy_complete and not active_analysis and current_stage != "ready_for_analysis":
        set_stage(root, "brand_classifying", candidate_brands=runtime_brands)
    ranking_payload = _ranking_payload(root, records, runtime_brands)
    classifications = {
        str(record.get("material_id")): record["brand_classification"]
        for record in ranking_payload["records"]
        if record.get("material_id") is not None
    }
    payloads = {
        "material_ranking": ranking_payload,
        "audience": _audience_payload(root, records, classifications, runtime_brands),
        "content_tags": _content_tags_payload(root, records, classifications, runtime_brands),
        "script": _script_payload(root, details, classifications, runtime_brands),
        "interaction_series": _interaction_payload(root, details, classifications, runtime_brands),
    }
    if (root / "highlights-manifest.json").is_file():
        payloads["highlight_frames"] = _highlight_payload(root, details, highlights, classifications, runtime_brands)
    manifest = _manifest(root, details, highlights, payloads, runtime_brands)
    destination = Path(output_dir) if output_dir is not None else root / "facts"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        files = {
            "material_ranking": "material-ranking.json",
            "audience": "audience.json",
            "content_tags": "content-tags.json",
            "script": "script.json",
            "interaction_series": "interaction-series.json",
            "highlight_frames": "highlight-frames.json",
        }
        for name, filename in files.items():
            if name not in payloads:
                continue
            write_json_atomically(payloads[name], staging / filename)
            manifest["packages"][name]["sha256"] = _sha256(staging / filename)
        write_json_atomically(manifest, staging / "manifest.json")
        destination.mkdir(parents=True, exist_ok=True)
        for path in staging.iterdir():
            os.replace(path, destination / path.name)
    except Exception:
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if collection_succeeded and not active_analysis:
        set_stage(root, "ready_for_analysis", coverage=manifest["coverage"])
    elif not collection_succeeded:
        set_stage(root, "partial", reason="collection did not complete all gates")
    return manifest


def build_fact_packages(run_dir: str | Path, *, output_dir: str | Path | None = None) -> dict:
    """Build facts without destroying an existing recoverable collection state."""
    root = Path(run_dir)
    initial_stage = get_workflow_status(root)["stage"]
    try:
        return _build_fact_packages(root, output_dir=output_dir)
    except Exception:
        try:
            failure_stage = initial_stage if initial_stage in {"partial", "failed"} else "failed"
            set_stage(root, failure_stage, error="fact package build failed")
        except Exception:
            pass
        raise
