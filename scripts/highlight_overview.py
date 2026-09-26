"""Project manifest-bound Yuntu highlight frames for the offline overview."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

try:
    from command_io import CommandFileCache
except ImportError:  # pragma: no cover
    from .command_io import CommandFileCache


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_KNOWN_REASON_TYPES = {"cover", "local_peak", "top_values"}


class HighlightOverviewError(ValueError):
    """Raised when the immutable highlight evidence chain is not trustworthy."""


def _read_json(path: Path, label: str, expected_type: type, io_cache=None) -> Any:
    try:
        payload = io_cache.read_json(path) if io_cache is not None else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HighlightOverviewError(f"{label} is missing or unreadable") from exc
    if not isinstance(payload, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise HighlightOverviewError(f"{label} must be a {expected}")
    return payload


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HighlightOverviewError(f"{label} is required")
    return value.strip()


def _safe_relative_path(run_root: Path, raw_path: Any, label: str) -> tuple[Path, str]:
    path_text = _nonblank(raw_path, label)
    path = Path(path_text)
    if path.is_absolute() or "\\" in path_text or "\x00" in path_text:
        raise ValueError(f"{label} must be a safe relative path")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative path")
    candidate = (run_root / path).resolve()
    try:
        relative = candidate.relative_to(run_root)
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the run") from exc
    return candidate, relative.as_posix()


def _verify_artifact(run_root: Path, artifact: Any, label: str, io_cache=None) -> tuple[Path, str]:
    if not isinstance(artifact, dict):
        raise HighlightOverviewError(f"{label} artifact is missing")
    try:
        path, relative = _safe_relative_path(run_root, artifact.get("path"), f"{label} path")
    except ValueError as exc:
        raise HighlightOverviewError(str(exc)) from exc
    if not path.is_file():
        raise HighlightOverviewError(f"{label} source file is missing")
    declared_hash = _nonblank(artifact.get("sha256"), f"{label} sha256")
    if not _SHA256.fullmatch(declared_hash):
        raise HighlightOverviewError(f"{label} sha256 is invalid")
    try:
        if io_cache is not None:
            actual_hash, actual_size = io_cache.verify(path, artifact)
        else:
            content = path.read_bytes()
            actual_hash, actual_size = hashlib.sha256(content).hexdigest(), len(content)
    except (OSError, ValueError) as exc:
        raise HighlightOverviewError(f"{label} sha256 or size does not match source") from exc
    if actual_hash != declared_hash.lower():
        raise HighlightOverviewError(f"{label} sha256 does not match source")
    declared_size = artifact.get("sizeBytes")
    if declared_size is not None and (type(declared_size) is not int or declared_size != actual_size):
        raise HighlightOverviewError(f"{label} size does not match source")
    return path, relative


def _package(run_root: Path, manifest: dict, name: str, expected_type: type, io_cache=None) -> tuple[dict, list]:
    packages = manifest.get("packages")
    package = packages.get(name) if isinstance(packages, dict) else None
    if not isinstance(package, dict):
        raise HighlightOverviewError(f"facts manifest {name} package is missing")
    try:
        path, _ = _safe_relative_path(run_root, package.get("path"), f"{name} package path")
    except ValueError as exc:
        raise HighlightOverviewError(str(exc)) from exc
    if io_cache is not None:
        try:
            payload = io_cache.verify_json(path, package)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise HighlightOverviewError(f"{name} package sha256 or size does not match source") from exc
    else:
        _verify_artifact(run_root, package, f"{name} package")
        payload = _read_json(path, f"{name} package", dict)
    if not isinstance(payload, dict):
        raise HighlightOverviewError(f"{name} package must be a object")
    records = payload.get("records")
    if not isinstance(records, expected_type):
        raise HighlightOverviewError(f"{name} package records are invalid")
    if package.get("record_count") is not None and package["record_count"] != len(records):
        raise HighlightOverviewError(f"{name} package record count does not match source")
    return package, records


def _record_ids(records: list, label: str) -> list[str]:
    ids = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HighlightOverviewError(f"{label} record {index} must be an object")
        material_id = _nonblank(record.get("material_id"), f"{label} record {index} material_id")
        ids.append(material_id)
    if len(ids) != len(set(ids)):
        raise HighlightOverviewError(f"{label} contains duplicate material IDs")
    return ids


def _validate_package_ids(package: dict, ids: list[str], label: str) -> None:
    declared_ids = package.get("material_ids")
    if declared_ids is not None and (not isinstance(declared_ids, list) or set(declared_ids) != set(ids)):
        raise HighlightOverviewError(f"{label} material IDs do not match source")


def _verify_bound_sources(run_root: Path, io_cache=None) -> tuple[list[dict], list[dict], list[dict]]:
    if not run_root.is_dir():
        raise HighlightOverviewError("run directory is missing")
    summary = _read_json(run_root / "run-summary.json", "run summary", dict, io_cache)
    run_id = _nonblank(summary.get("runId"), "run summary runId")
    status = summary.get("collectionStatus", summary.get("status"))
    if status is not None and status not in {"success", "succeeded", "ready_for_analysis", "analysis_completed"}:
        raise HighlightOverviewError("run is not a completed successful collection")

    facts_manifest = _read_json(run_root / "facts/manifest.json", "facts manifest", dict, io_cache)
    if facts_manifest.get("run_id") != run_id:
        raise HighlightOverviewError("facts manifest run_id does not match run summary")
    ranking_package, ranking_records = _package(run_root, facts_manifest, "material_ranking", list, io_cache)
    highlight_package, highlight_records = _package(run_root, facts_manifest, "highlight_frames", list, io_cache)
    ranking_ids = _record_ids(ranking_records, "material ranking")
    highlight_ids = _record_ids(highlight_records, "highlight frames")
    _validate_package_ids(ranking_package, ranking_ids, "material ranking package")
    _validate_package_ids(highlight_package, highlight_ids, "highlight frames package")

    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict):
        raise HighlightOverviewError("run summary artifacts are missing")
    _verify_artifact(run_root, artifacts.get("listRecords"), "list records", io_cache)
    for material_id in highlight_ids:
        _verify_artifact(
            run_root,
            artifacts.get(f"detailRecord:{material_id}"),
            f"detail record {material_id}",
            io_cache,
        )

    highlights_manifest = _read_json(run_root / "highlights-manifest.json", "highlights manifest", dict, io_cache)
    materials = highlights_manifest.get("materials")
    if not isinstance(materials, list):
        raise HighlightOverviewError("highlights manifest materials are invalid")
    manifest_ids = _record_ids(materials, "highlights manifest")
    if set(manifest_ids) != set(highlight_ids):
        raise HighlightOverviewError("highlights manifest material IDs do not match highlight facts")
    return ranking_records, highlight_records, materials


def _reason(reason: Any) -> dict | None:
    if not isinstance(reason, dict):
        return None
    reason_type = reason.get("type")
    if reason_type not in _KNOWN_REASON_TYPES:
        return None
    result = {
        "trend_name": None,
        "value": None,
        "rank": None,
        "type": reason_type,
        "within_high_point": None,
    }
    if reason_type == "cover":
        return result
    trend_name = reason.get("trend_name")
    value = reason.get("value")
    rank = reason.get("rank")
    within_high_point = reason.get("within_high_point")
    if not isinstance(trend_name, str) or not trend_name.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if type(rank) is not int or rank < 1 or type(within_high_point) is not bool:
        return None
    result.update({
        "trend_name": trend_name.strip(),
        "value": value,
        "rank": rank,
        "within_high_point": within_high_point,
    })
    return result


def _frame_gap(material_id: str, frame_index: int, code: str) -> dict:
    return {"materialId": material_id, "frameIndex": frame_index, "code": code}


def _collect(run_dir: Path, io_cache=None, asset_index=None) -> tuple[dict, list[tuple[dict, Path]]]:
    run_root = Path(run_dir).resolve()
    io_cache = io_cache or CommandFileCache(run_root)
    ranking_records, highlight_records, manifest_materials = _verify_bound_sources(run_root, io_cache)
    titles = {record["material_id"]: record.get("title") for record in ranking_records}
    facts_by_id = {record["material_id"]: record for record in highlight_records}
    manifest_by_id = {record["material_id"]: record for record in manifest_materials}
    cards = []
    verified_assets = []
    gaps = []

    for material_id, fact_record in facts_by_id.items():
        title = titles.get(material_id)
        manifest_record = manifest_by_id[material_id]
        fact_frames = fact_record.get("frames")
        manifest_frames = manifest_record.get("frames")
        if not isinstance(fact_frames, list) or not isinstance(manifest_frames, list):
            gaps.append(_frame_gap(material_id, 0, "frames_missing"))
            continue
        if len(fact_frames) != len(manifest_frames):
            gaps.append(_frame_gap(material_id, 0, "frame_count_mismatch"))
        for frame_index, fact_frame in enumerate(fact_frames, start=1):
            manifest_frame = manifest_frames[frame_index - 1] if frame_index <= len(manifest_frames) else {}
            if not isinstance(fact_frame, dict) or not isinstance(manifest_frame, dict):
                gaps.append(_frame_gap(material_id, frame_index, "frame_binding_invalid"))
                continue
            if title is None or not isinstance(title, str) or not title.strip():
                gaps.append(_frame_gap(material_id, frame_index, "material_title_missing"))
                continue
            if not fact_frame.get("available") or not manifest_frame.get("available"):
                gaps.append(_frame_gap(material_id, frame_index, "frame_unavailable"))
                continue
            raw_path = manifest_frame.get("relative_path")
            try:
                source_path, source_relative = _safe_relative_path(run_root, raw_path, "frame path")
            except (HighlightOverviewError, ValueError):
                gaps.append(_frame_gap(material_id, frame_index, "invalid_frame_path"))
                continue
            if fact_frame.get("relative_path") != manifest_frame.get("relative_path"):
                gaps.append(_frame_gap(material_id, frame_index, "frame_binding_mismatch"))
                continue
            if any(fact_frame.get(key) != manifest_frame.get(key) for key in ("second", "is_cover", "frame_type", "reasons", "available")):
                gaps.append(_frame_gap(material_id, frame_index, "frame_binding_mismatch"))
                continue
            if not source_path.is_file():
                gaps.append(_frame_gap(material_id, frame_index, "frame_missing"))
                continue
            suffix = source_path.suffix.lower()
            if suffix not in _IMAGE_EXTENSIONS:
                gaps.append(_frame_gap(material_id, frame_index, "invalid_frame_extension"))
                continue
            source_hash, source_size = io_cache.digest(source_path)
            frame_gap_codes = []
            declared_hash = manifest_frame.get("sha256")
            if declared_hash is not None and (
                not isinstance(declared_hash, str)
                or not _SHA256.fullmatch(declared_hash)
                or declared_hash.lower() != source_hash
            ):
                frame_gap_codes.append("frame_hash_mismatch")
            declared_size = manifest_frame.get("sizeBytes")
            if declared_size is not None and (type(declared_size) is not int or declared_size != source_size):
                frame_gap_codes.append("frame_size_mismatch")
            reasons = fact_frame.get("reasons")
            normalized_reasons = [_reason(item) for item in reasons] if isinstance(reasons, list) else []
            if not normalized_reasons or any(item is None for item in normalized_reasons):
                frame_gap_codes.append("unknown_reason")
            if frame_gap_codes:
                for code in frame_gap_codes:
                    gaps.append(_frame_gap(material_id, frame_index, code))
                continue
            role = "视频开场画面" if fact_frame.get("is_cover") is True or fact_frame.get("frame_type") == "cover" else "指标高光帧"
            card = {
                "id": f"highlight:{material_id}:{frame_index}:{fact_frame.get('second')}",
                "materialId": material_id,
                "materialTitle": title.strip(),
                "second": fact_frame.get("second"),
                "frameIndex": frame_index,
                "role": role,
                "reasons": normalized_reasons,
                "assetPath": f"assets/highlights/{source_hash}{suffix}",
                "assetSha256": source_hash,
                "assetSizeBytes": source_size,
            }
            cards.append(card)
            verified_assets.append((card, source_path))
            if asset_index is not None:
                asset_index[("highlight", card["id"])] = source_path

    model = {
        "schemaVersion": 1,
        "status": "available" if cards else "unavailable",
        "source": {
            "materialCount": len(highlight_records),
            "verifiedFrameCount": len(cards),
        },
        "cards": cards,
        "gaps": gaps,
    }
    return model, verified_assets


def build_highlight_overview(run_dir: Path, io_cache=None, asset_index=None) -> dict:
    """Return only verified and redacted high-frame card data."""

    model, _ = _collect(Path(run_dir), io_cache, asset_index)
    return model


def copy_verified_highlight_assets(run_dir: Path, output_dir: Path, model: dict, io_cache=None, asset_index=None) -> int:
    """Copy verified frames into the output and verify every copied byte."""

    run_root = Path(run_dir).resolve()
    io_cache = io_cache or CommandFileCache(run_root)
    if asset_index is None:
        asset_index = {}
        collected, verified_assets = _collect(run_root, io_cache, asset_index)
    else:
        collected = model
        verified_assets = [
            (card, asset_index.get(("highlight", card["id"])))
            for card in model.get("cards", [])
        ]
    expected = {
        card["id"]: (card["assetPath"], card["assetSha256"], card["assetSizeBytes"])
        for card in model.get("cards", [])
    }
    actual = {
        card["id"]: (card["assetPath"], card["assetSha256"], card["assetSizeBytes"])
        for card in collected["cards"]
    }
    if expected != actual:
        raise HighlightOverviewError("highlight overview changed before asset copy")
    asset_root = Path(output_dir) / "assets/highlights"
    asset_root.mkdir(parents=True, exist_ok=True)
    for card, source_path in verified_assets:
        if source_path is None:
            raise HighlightOverviewError(f"verified highlight source is missing: {card['id']}")
        destination = Path(output_dir) / card["assetPath"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            io_cache.copy_verified(
                source_path, destination, card["assetSha256"], card["assetSizeBytes"]
            )
        except (OSError, ValueError) as exc:
            raise HighlightOverviewError(f"copied highlight asset failed verification: {card['id']}") from exc
    return len(verified_assets)
