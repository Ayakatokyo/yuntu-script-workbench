"""Build a manifest-bound, redacted audience coverage overview."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from command_io import CommandFileCache
except ImportError:  # pragma: no cover
    from .command_io import CommandFileCache


class AudienceOverviewError(ValueError):
    """Raised when audience facts cannot be verified safely."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_DIMENSIONS = {
    "crowd_group": ("八大人群分布", "bar"),
    "gender": ("性别分布", "donut"),
    "age": ("年龄分布", "donut"),
}


def _read_json(path: Path, label: str, expected: type, io_cache=None) -> Any:
    try:
        value = io_cache.read_json(path) if io_cache is not None else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudienceOverviewError(f"{label} is missing or unreadable") from exc
    if not isinstance(value, expected):
        raise AudienceOverviewError(f"{label} has invalid shape")
    return value


def _safe_path(root: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\\" in raw or "\x00" in raw:
        raise AudienceOverviewError(f"{label} must be a safe relative path")
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AudienceOverviewError(f"{label} must be a safe relative path")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AudienceOverviewError(f"{label} escapes run") from exc
    return resolved


def _verify_file(path: Path, metadata: dict, label: str, io_cache=None) -> None:
    if not path.is_file():
        raise AudienceOverviewError(f"{label} is missing")
    expected_hash = metadata.get("sha256")
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise AudienceOverviewError(f"{label} sha256 is invalid")
    try:
        if io_cache is not None:
            actual, actual_size = io_cache.verify(path, metadata)
        else:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            actual_size = path.stat().st_size
    except (OSError, ValueError) as exc:
        raise AudienceOverviewError(f"{label} sha256 or size does not match source") from exc
    if actual != expected_hash.lower():
        raise AudienceOverviewError(f"{label} sha256 does not match source")
    expected_size = metadata.get("sizeBytes")
    if expected_size is not None and (type(expected_size) is not int or expected_size != actual_size):
        raise AudienceOverviewError(f"{label} size does not match source")


def _load_manifest_package(root: Path, package_name: str, io_cache=None) -> tuple[dict, dict, dict]:
    manifest = _read_json(root / "facts/manifest.json", "facts manifest", dict, io_cache)
    summary = _read_json(root / "run-summary.json", "run summary", dict, io_cache)
    packages = manifest.get("packages")
    package = packages.get(package_name) if isinstance(packages, dict) else None
    if not isinstance(package, dict):
        raise AudienceOverviewError(f"facts manifest {package_name} package is missing")
    package_path = _safe_path(root, package.get("path"), f"{package_name} package path")
    if io_cache is not None:
        try:
            io_cache.verify_json(package_path, package)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AudienceOverviewError(f"{package_name} package sha256 or size does not match source") from exc
    else:
        _verify_file(package_path, package, f"{package_name} package")
    return manifest, summary, package


def _verify_list_source(root: Path, summary: dict, expected_count: int, io_cache=None) -> None:
    artifacts = summary.get("artifacts")
    artifact = artifacts.get("listRecords") if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict) or artifact.get("path") != "normalized/list-records.json":
        raise AudienceOverviewError("normalized list source declaration is missing")
    source = _safe_path(root, artifact["path"], "list source path")
    if io_cache is not None:
        try:
            rows = io_cache.verify_json(source, artifact)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AudienceOverviewError("list source sha256 or size does not match source") from exc
        if not isinstance(rows, list):
            raise AudienceOverviewError("list source has invalid shape")
    else:
        _verify_file(source, artifact, "list source")
        rows = _read_json(source, "list source", list)
    if len(rows) != expected_count:
        raise AudienceOverviewError("list source record_count does not match facts")


def _package_records(root: Path, package_name: str, io_cache=None) -> tuple[dict, dict, list[dict]]:
    manifest, summary, package = _load_manifest_package(root, package_name, io_cache)
    expected_count = package.get("record_count")
    if type(expected_count) is not int or expected_count < 0:
        raise AudienceOverviewError(f"{package_name} record_count is invalid")
    records = _read_json(
        _safe_path(root, package["path"], f"{package_name} package path"),
        f"{package_name} facts",
        dict,
        io_cache,
    ).get("records")
    if not isinstance(records, list) or len(records) != expected_count:
        raise AudienceOverviewError(f"{package_name} record_count does not match facts")
    ids = [item.get("material_id") for item in records if isinstance(item, dict)]
    declared_ids = package.get("material_ids")
    if not isinstance(declared_ids, list) or len(declared_ids) != expected_count or set(ids) != set(declared_ids) or len(set(ids)) != len(ids):
        raise AudienceOverviewError(f"{package_name} material_ids do not match facts")
    coverage = manifest.get("coverage")
    if isinstance(coverage, dict) and coverage.get("ranking_records") not in (None, expected_count if package_name == "audience" else coverage.get("ranking_records")):
        raise AudienceOverviewError("facts coverage ranking_records is invalid")
    if package_name == "audience":
        source_paths = package.get("source_paths")
        if not isinstance(source_paths, list) or "normalized/list-records.json" not in source_paths:
            raise AudienceOverviewError("audience normalized source declaration is missing")
        for record in records:
            declared_source = record.get("source_paths", {}).get("normalized") if isinstance(record.get("source_paths"), dict) else None
            if declared_source != "normalized/list-records.json":
                raise AudienceOverviewError("audience record normalized source path is invalid")
        _verify_list_source(root, summary, expected_count, io_cache)
    return manifest, summary, records


def _bucket_sort(item: tuple[str, int]) -> tuple[Any, ...]:
    label, value = item
    if re.fullmatch(r"\d{2}-\d{2}|\d{2}\+", label):
        return (-value, 0, int(label[:2]), label)
    return (-value, 1, label)


def build_audience_overview(run_dir: Path, material_ids: set[str] | None = None, io_cache=None) -> dict:
    root = Path(run_dir).resolve()
    io_cache = io_cache or CommandFileCache(root)
    _, _, records = _package_records(root, "audience", io_cache)
    if material_ids is not None:
        records = [record for record in records if str(record.get("material_id")) in material_ids]
    counts: dict[str, dict[str, set[str]]] = {dimension: {} for dimension in _DIMENSIONS}
    gaps: dict[str, int] = {}
    for record in records:
        material_id = record.get("material_id") if isinstance(record, dict) else None
        observations = record.get("observations") if isinstance(record, dict) else None
        if not isinstance(material_id, (str, int)) or not isinstance(observations, list):
            gaps["audience_record_unavailable"] = gaps.get("audience_record_unavailable", 0) + 1
            continue
        for observation in observations:
            if not isinstance(observation, dict):
                gaps["audience_observation_unavailable"] = gaps.get("audience_observation_unavailable", 0) + 1
                continue
            dimension = observation.get("dimension")
            tag = observation.get("tag")
            if dimension not in _DIMENSIONS:
                gaps["audience_unknown_dimension"] = gaps.get("audience_unknown_dimension", 0) + 1
                continue
            if not isinstance(tag, str) or not tag.strip():
                gaps["audience_empty_tag"] = gaps.get("audience_empty_tag", 0) + 1
                continue
            counts[dimension].setdefault(tag.strip(), set()).add(str(material_id))

    dimensions: dict[str, dict] = {}
    for dimension, (label, chart) in _DIMENSIONS.items():
        values = sorted(((tag, len(materials)) for tag, materials in counts[dimension].items()), key=_bucket_sort)
        total = sum(value for _, value in values)
        dimensions[dimension] = {
            "label": label,
            "chart": chart,
            "total": total,
            "buckets": [
                {"label": tag, "value": value, "share": value / total if total else 0}
                for tag, value in values
            ],
        }
    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {"path": "facts/audience.json", "recordCount": len(records)},
        "materialScope": {"count": len(records)},
        "metric": {"id": "MATERIAL_COVERAGE", "label": "覆盖素材数", "unit": "count"},
        "dimensions": dimensions,
        "gaps": [{"code": code, "count": count} for code, count in sorted(gaps.items())],
    }
