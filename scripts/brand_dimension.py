"""Build a manifest-bound brand dimension for the offline overview."""

from __future__ import annotations

from pathlib import Path

try:
    from audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file
except ImportError:
    from .audience_overview import AudienceOverviewError, _load_manifest_package, _read_json, _safe_path, _verify_file


ALL_BRANDS_ID = "all"
UNKNOWN_BRAND_ID = "unknown"


class BrandDimensionError(ValueError):
    """Raised when the persisted brand classification cannot be trusted."""


def _brand_id(brand: str) -> str:
    return f"brand:{brand}"


def build_brand_dimension(run_dir: Path, io_cache=None) -> dict:
    """Project the persisted runtime brand classification without re-matching titles."""

    root = Path(run_dir).resolve()
    try:
        manifest, _, package = _load_manifest_package(root, "content_tags", io_cache)
        expected_count = package.get("record_count")
        if type(expected_count) is not int or expected_count < 0:
            raise BrandDimensionError("content tags record_count is invalid")
        package_path = _safe_path(root, package.get("path"), "content tags package path")
        _verify_file(package_path, package, "content tags package", io_cache)
        payload = _read_json(package_path, "content tags facts", dict, io_cache)
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != expected_count:
            raise BrandDimensionError("content tags record_count does not match facts")
    except AudienceOverviewError as exc:
        raise BrandDimensionError(str(exc)) from exc

    classification_meta = manifest.get("brand_classification")
    candidates = classification_meta.get("candidate_brands") if isinstance(classification_meta, dict) else None
    rule_version = classification_meta.get("rule_version") if isinstance(classification_meta, dict) else None
    if not isinstance(candidates, list) or any(not isinstance(item, str) or not item.strip() for item in candidates):
        raise BrandDimensionError("brand classification candidate brands are missing")
    candidates = [item.strip() for item in candidates]
    if len(set(candidates)) != len(candidates) or not isinstance(rule_version, str) or not rule_version.strip():
        raise BrandDimensionError("brand classification metadata is invalid")

    material_ids: list[str] = []
    material_brands: dict[str, list[str]] = {}
    ids_by_tab: dict[str, list[str]] = {ALL_BRANDS_ID: []}
    for candidate in candidates:
        ids_by_tab[_brand_id(candidate)] = []
    ids_by_tab[UNKNOWN_BRAND_ID] = []

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BrandDimensionError(f"content tags record {index} must be an object")
        material_id = record.get("material_id")
        if material_id is None or isinstance(material_id, bool) or not str(material_id).strip():
            raise BrandDimensionError(f"content tags record {index} has no material_id")
        material_id = str(material_id).strip()
        if material_id in material_brands:
            raise BrandDimensionError(f"duplicate material_id: {material_id}")
        classification = record.get("brand_classification")
        matched = classification.get("matchedBrands") if isinstance(classification, dict) else None
        if not isinstance(matched, list) or any(not isinstance(item, str) or item not in candidates for item in matched):
            raise BrandDimensionError(f"brand classification is invalid for material: {material_id}")
        matched = list(dict.fromkeys(matched))
        material_ids.append(material_id)
        material_brands[material_id] = matched
        ids_by_tab[ALL_BRANDS_ID].append(material_id)
        if not matched:
            ids_by_tab[UNKNOWN_BRAND_ID].append(material_id)
        for brand in matched:
            ids_by_tab[_brand_id(brand)].append(material_id)

    tabs = [{"id": ALL_BRANDS_ID, "label": "全部素材", "kind": "all", "count": len(material_ids)}]
    tabs.extend(
        {"id": _brand_id(brand), "label": brand, "kind": "brand", "brand": brand, "count": len(ids_by_tab[_brand_id(brand)])}
        for brand in candidates
    )
    if ids_by_tab[UNKNOWN_BRAND_ID]:
        tabs.append({"id": UNKNOWN_BRAND_ID, "label": "未分类品牌", "kind": "unknown", "count": len(ids_by_tab[UNKNOWN_BRAND_ID])})

    declared_counts = classification_meta.get("counts") if isinstance(classification_meta, dict) else None
    if isinstance(declared_counts, dict):
        for brand in candidates:
            if declared_counts.get(brand) != len(ids_by_tab[_brand_id(brand)]):
                raise BrandDimensionError(f"brand classification count does not match facts: {brand}")
    declared_unknown = classification_meta.get("unknown_count") if isinstance(classification_meta, dict) else None
    if declared_unknown is not None and declared_unknown != len(ids_by_tab[UNKNOWN_BRAND_ID]):
        raise BrandDimensionError("unknown brand count does not match facts")

    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {"path": "facts/content-tags.json", "recordCount": len(material_ids)},
        "ruleVersion": rule_version,
        "tabs": tabs,
        "materialIdsByTab": ids_by_tab,
        "materialBrands": material_brands,
    }
