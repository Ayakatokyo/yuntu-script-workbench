"""Build a script formula overview from the source formula hierarchy."""

from __future__ import annotations

from pathlib import Path

try:
    from audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file
    from content_formula_labels import formula_detail_labels, resolve_formula_ref
except ImportError:
    from .audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file
    from .content_formula_labels import formula_detail_labels, resolve_formula_ref


class ScriptFormulaOverviewError(ValueError):
    """Raised when script formula facts cannot be verified safely."""


def _gap(gaps: dict[str, int], code: str) -> None:
    gaps[code] = gaps.get(code, 0) + 1


def _verify_detail_sources(root: Path, summary: dict, records: list[dict], io_cache=None) -> None:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ScriptFormulaOverviewError("detail source declarations are missing")
    for record in records:
        material_id = str(record.get("material_id", ""))
        source_paths = record.get("source_paths")
        normalized = source_paths.get("normalized") if isinstance(source_paths, dict) else None
        expected_path = f"normalized/detail-records/{material_id}.json"
        if normalized != expected_path:
            raise ScriptFormulaOverviewError("detail normalized source path is invalid")
        artifact = artifacts.get(f"detailRecord:{material_id}")
        if not isinstance(artifact, dict) or artifact.get("path") != expected_path:
            raise ScriptFormulaOverviewError("detail source declaration is missing")
        source_path = _safe_path(root, expected_path, "detail source path")
        if io_cache is not None:
            try:
                io_cache.verify_json(source_path, artifact)
            except (OSError, UnicodeError, ValueError) as exc:
                raise ScriptFormulaOverviewError("detail source sha256 or size does not match source") from exc
        else:
            _verify_file(source_path, artifact, "detail source")


def build_script_formula_overview(run_dir: Path, material_ids: set[str] | None = None, io_cache=None) -> dict:
    root = Path(run_dir).resolve()
    try:
        _, summary, package = _load_manifest_package(root, "script", io_cache)
        records = _read_json(
            _safe_path(root, package["path"], "script package path"),
            "script facts",
            dict,
            io_cache,
        ).get("records")
    except AudienceOverviewError as exc:
        raise ScriptFormulaOverviewError(str(exc)) from exc
    expected_count = package.get("record_count")
    if type(expected_count) is not int or not isinstance(records, list) or len(records) != expected_count:
        raise ScriptFormulaOverviewError("script record_count does not match facts")
    package_sources = package.get("source_paths")
    if not isinstance(package_sources, list) or "normalized/detail-records/" not in package_sources:
        raise ScriptFormulaOverviewError("script normalized source declaration is missing")
    declared_ids = package.get("material_ids")
    actual_ids = [record.get("material_id") for record in records if isinstance(record, dict)]
    if not isinstance(declared_ids, list) or set(actual_ids) != set(declared_ids) or len(set(actual_ids)) != len(actual_ids):
        raise ScriptFormulaOverviewError("script material_ids do not match facts")
    try:
        _verify_detail_sources(root, summary, records, io_cache)
    except AudienceOverviewError as exc:
        raise ScriptFormulaOverviewError(str(exc)) from exc
    if material_ids is not None:
        records = [record for record in records if str(record.get("material_id")) in material_ids]
    primary_ids: dict[str, set[str]] = {}
    secondary_ids: dict[str, dict[str, set[str]]] = {}
    gaps: dict[str, int] = {}
    entries: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for record in records:
        material_id = str(record["material_id"])
        availability = record.get("data_availability", {}).get("content_formula") if isinstance(record.get("data_availability"), dict) else None
        formula = record.get("content_formula")
        if not isinstance(availability, dict) or availability.get("available") is not True or not isinstance(formula, dict):
            _gap(gaps, "formula_source_empty")
            continue
        labels = formula.get("labels")
        if labels == []:
            _gap(gaps, "formula_source_empty")
            continue
        if not isinstance(labels, list):
            _gap(gaps, "formula_hierarchy_unavailable")
            continue
        detail_labels = formula_detail_labels(formula)
        for item in labels:
            if not isinstance(item, dict) or not isinstance(item.get("level_2_labels"), list):
                _gap(gaps, "formula_hierarchy_unavailable")
                continue
            primary_id, primary_label = resolve_formula_ref(item.get("level_1_label"), "primary", detail_labels)
            if primary_id is None or primary_label is None:
                _gap(gaps, "formula_hierarchy_unavailable")
                continue
            secondary_entries: list[tuple[str, str]] = []
            for secondary_id in item["level_2_labels"]:
                secondary_ref_id, secondary_label = resolve_formula_ref(secondary_id, "secondary", detail_labels)
                if secondary_ref_id is None or secondary_label is None:
                    _gap(gaps, "formula_hierarchy_unavailable")
                    continue
                secondary_entries.append((secondary_ref_id, secondary_label))
            entries.append((material_id, primary_id, primary_label, secondary_entries))
    for material_id, primary_id, primary_label, secondary_entries in entries:
        primary_ids.setdefault(primary_label, set()).add(material_id)
        for secondary_id, secondary_label in secondary_entries:
            secondary_ids.setdefault(primary_label, {}).setdefault(secondary_label, set()).add(material_id)
    formula_materials = set().union(*primary_ids.values()) if primary_ids else set()
    primary_total = len(formula_materials)
    primary_buckets = []
    for label, materials in sorted(primary_ids.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        primary_buckets.append({"label": label, "value": len(materials), "share": len(materials) / primary_total if primary_total else 0})
    secondary_buckets: dict[str, list[dict]] = {}
    for primary, buckets in secondary_ids.items():
        total = len(primary_ids[primary])
        secondary_buckets[primary] = [
            {"label": label, "value": len(materials), "share": len(materials) / total if total else 0}
            for label, materials in sorted(buckets.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        ]
    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {"path": "facts/script.json", "recordCount": len(records), "mappingMode": "dynamic-content"},
        "coverage": {"scriptMaterialCount": len(records), "formulaMaterialCount": len(formula_materials)},
        "primaryBuckets": primary_buckets,
        "secondaryBucketsByPrimary": secondary_buckets,
        "gaps": [{"code": code, "count": count} for code, count in sorted(gaps.items())],
    }
