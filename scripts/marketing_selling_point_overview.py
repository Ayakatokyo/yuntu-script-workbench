"""Build a manifest-bound marketing selling-point coverage overview."""

from __future__ import annotations

from pathlib import Path

try:
    from audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file
except ImportError:
    from .audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file


class MarketingSellingPointOverviewError(ValueError):
    """Raised when marketing selling-point facts cannot be verified safely."""


def _gap(gaps: dict[str, int], code: str) -> None:
    gaps[code] = gaps.get(code, 0) + 1


def _verify_list_source(root: Path, summary: dict, expected_count: int, io_cache=None) -> None:
    artifacts = summary.get("artifacts")
    artifact = artifacts.get("listRecords") if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict) or artifact.get("path") != "normalized/list-records.json":
        raise MarketingSellingPointOverviewError("normalized list source declaration is missing")
    source = _safe_path(root, artifact["path"], "list source path")
    if io_cache is not None:
        try:
            rows = io_cache.verify_json(source, artifact)
        except (OSError, UnicodeError, ValueError) as exc:
            raise MarketingSellingPointOverviewError("list source sha256 or size does not match source") from exc
        if not isinstance(rows, list):
            raise MarketingSellingPointOverviewError("list source has invalid shape")
    else:
        _verify_file(source, artifact, "list source")
        rows = _read_json(source, "list source", list)
    if len(rows) != expected_count:
        raise MarketingSellingPointOverviewError("list source record_count does not match facts")


def _bucket_sort(item: tuple[str, set[str]]) -> tuple[int, str]:
    label, materials = item
    return (-len(materials), label)


def build_marketing_selling_point_overview(run_dir: Path, material_ids: set[str] | None = None, io_cache=None) -> dict:
    """Aggregate platform content tags into material-coverage buckets.

    Tags with an underscore use the text before the first underscore as the
    primary selling point and the remaining text as its secondary selling
    point. Unscoped tags are retained only as a gap because they do not carry
    the hierarchy required by this view.
    """

    root = Path(run_dir).resolve()
    try:
        _, summary, package = _load_manifest_package(root, "content_tags", io_cache)
        expected_count = package.get("record_count")
        if type(expected_count) is not int or expected_count < 0:
            raise MarketingSellingPointOverviewError("content tags record_count is invalid")
        source_paths = package.get("source_paths")
        if not isinstance(source_paths, list) or "normalized/list-records.json" not in source_paths:
            raise MarketingSellingPointOverviewError("content tags normalized source declaration is missing")
        package_path = _safe_path(root, package.get("path"), "content tags package path")
        payload = _read_json(package_path, "content tags facts", dict, io_cache)
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != expected_count:
            raise MarketingSellingPointOverviewError("content tags record_count does not match facts")
        declared_ids = package.get("material_ids")
        actual_ids = []
        for record in records:
            if not isinstance(record, dict):
                raise MarketingSellingPointOverviewError("content tags records must contain objects")
            material_id = record.get("material_id")
            if material_id is None or isinstance(material_id, bool) or str(material_id).strip() == "":
                raise MarketingSellingPointOverviewError("content tags record has no material_id")
            actual_ids.append(str(material_id))
            declared_source = record.get("source_paths", {}).get("normalized") if isinstance(record.get("source_paths"), dict) else None
            if declared_source != "normalized/list-records.json":
                raise MarketingSellingPointOverviewError("content tags record normalized source path is invalid")
        if (
            not isinstance(declared_ids, list)
            or len(declared_ids) != expected_count
            or {str(item) for item in declared_ids} != set(actual_ids)
            or len(set(actual_ids)) != len(actual_ids)
        ):
            raise MarketingSellingPointOverviewError("content tags material_ids do not match facts")
        _verify_list_source(root, summary, expected_count, io_cache)
    except AudienceOverviewError as exc:
        raise MarketingSellingPointOverviewError(str(exc)) from exc

    if material_ids is not None:
        records = [record for record in records if str(record.get("material_id")) in material_ids]

    primary_materials: dict[str, set[str]] = {}
    secondary_materials: dict[str, dict[str, set[str]]] = {}
    gaps: dict[str, int] = {}

    for record in records:
        material_id = str(record["material_id"])
        availability = record.get("data_availability", {}).get("material_tags") if isinstance(record.get("data_availability"), dict) else None
        tags = record.get("platform_content_tags")
        if not isinstance(availability, dict) or availability.get("available") is not True or not isinstance(tags, list):
            _gap(gaps, "selling_point_source_empty")
            continue
        if not tags:
            _gap(gaps, "selling_point_source_empty")
            continue
        for raw_tag in tags:
            if not isinstance(raw_tag, str) or not raw_tag.strip():
                _gap(gaps, "selling_point_label_unavailable")
                continue
            tag = raw_tag.strip()
            primary, separator, secondary = tag.partition("_")
            primary = primary.strip()
            secondary = secondary.strip() if separator else ""
            if not separator or not primary or not secondary:
                _gap(gaps, "selling_point_unscoped_label")
                continue
            primary_materials.setdefault(primary, set()).add(material_id)
            secondary_materials.setdefault(primary, {}).setdefault(secondary, set()).add(material_id)

    primary_buckets = []
    for label, materials in sorted(primary_materials.items(), key=_bucket_sort):
        primary_buckets.append({
            "label": label,
            "value": len(materials),
            "share": len(materials) / len(set().union(*primary_materials.values())) if primary_materials else 0,
        })

    secondary_buckets_by_primary: dict[str, list[dict]] = {}
    for primary, buckets in sorted(secondary_materials.items(), key=lambda item: _bucket_sort((item[0], primary_materials.get(item[0], set())))):
        total = len(primary_materials.get(primary, set()))
        secondary_buckets_by_primary[primary] = [
            {"label": label, "value": len(materials), "share": len(materials) / total if total else 0}
            for label, materials in sorted(buckets.items(), key=_bucket_sort)
        ]

    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {"path": "facts/content-tags.json", "recordCount": len(records)},
        "coverage": {
            "contentTagMaterialCount": len(records),
            "marketingSellingPointMaterialCount": len(set().union(*primary_materials.values())) if primary_materials else 0,
        },
        "primaryBuckets": primary_buckets,
        "secondaryBucketsByPrimary": secondary_buckets_by_primary,
        "gaps": [{"code": code, "count": count} for code, count in sorted(gaps.items())],
    }
