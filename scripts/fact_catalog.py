"""Catalog of the fact packages persisted by a completed Yuntu run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

try:
    from .analysis_facts import _safe_path, _sha256
    from .fact_selection import FactPackage
except ImportError:  # pragma: no cover - direct script imports
    from analysis_facts import _safe_path, _sha256
    from fact_selection import FactPackage


_COMMON_FIELD_LABELS = {
    "material_id": "素材ID",
    "rank": "榜单排名",
    "title": "素材标题",
    "material_name": "素材名称",
    "raw_people_tags": "人群标签",
    "brand_classification.value": "品牌归属",
    "selection_group.brand_name": "分组品牌名称",
    "script_text": "视频脚本",
    "platform_script_text": "云图平台脚本原文",
    "audio_transcript.text": "Agent 音轨转写文本",
    "script_source": "脚本来源",
    "content_formula": "内容公式",
    "script_segments": "脚本分段",
    "frames": "高光画面",
    "timestamp_sec": "视频时间点（秒）",
    "data_availability": "数据可用情况",
    "source_empty": "源数据为空",
    "brand": "品牌",
    "package": "事实模块",
    "field": "业务字段",
    "reason": "缺口原因",
    "request": "请求内容",
    "operator": "筛选方式",
    "value": "阈值",
    "material_ranking": "素材榜单",
    "audience": "人群分析",
    "script": "脚本分析",
    "highlight_frames": "高光帧分析",
    "top_percentile": "高位分位",
    "bottom_percentile": "低位分位",
    "ranking_records": "素材榜单记录",
    "audience_records": "人群分析记录",
    "script_records": "脚本记录",
    "highlight_records": "高光帧记录",
}


@lru_cache(maxsize=1)
def business_field_labels() -> dict[str, str]:
    """Return lightweight report labels without changing fact contracts."""
    labels = dict(_COMMON_FIELD_LABELS)
    path = Path(__file__).resolve().parents[1] / "config" / "yuntu-material-field-registry-v1.json"
    try:
        payload = _read(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return labels
    for entry in payload.get("fields", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
            continue
        field = entry.get("id")
        source_path = entry.get("path")
        if isinstance(field, str):
            labels[field] = entry["label"]
            if field.startswith("metrics."):
                labels[field.split(".", 1)[1]] = entry["label"]
        if isinstance(source_path, list) and all(isinstance(item, str) for item in source_path):
            labels[".".join(source_path)] = entry["label"]
    return labels


def businessize_text(value: Any, labels: dict[str, str] | None = None) -> str:
    text = str(value)
    for field, label in sorted((labels or business_field_labels()).items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.escape(field)
        if field.replace("_", "").isalnum() and "." not in field:
            pattern = rf"(?<![A-Za-z0-9_.]){pattern}(?![A-Za-z0-9_.])"
        text = re.sub(pattern + r"\s*", label, text)
    return text


@dataclass(frozen=True)
class FactCatalog:
    run_id: str
    schema_version: int | str
    record_counts: dict[FactPackage, int]
    fields: dict[FactPackage, tuple[str, ...]]
    available_fields: dict[FactPackage, dict[str, int]]
    package_paths: dict[FactPackage, str]
    capabilities: dict[FactPackage, dict[str, dict[str, Any]]] | None = None

    def to_dict(self) -> dict[str, Any]:
        labels = business_field_labels()
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "packages": {
                package.value: {
                    "record_count": self.record_counts[package],
                    "fields": list(self.fields[package]),
                    "available_fields": self.available_fields[package],
                    "business_field_labels": {
                        field: labels[field]
                        for field in self.fields[package]
                        if field in labels
                    },
                    "projectable_fields": [
                        {
                            "field": field,
                            "business_label": labels.get(field),
                            "available_count": self.available_fields[package].get(field, 0),
                            "value_type": ((self.capabilities or {}).get(package, {}).get(field, {}).get("value_type") or "unknown"),
                            "aggregations": list((self.capabilities or {}).get(package, {}).get(field, {}).get("aggregations", [])),
                        }
                        for field in self.fields[package]
                    ],
                    "path": self.package_paths[package],
                    "capabilities": (self.capabilities or {}).get(package, {}),
                }
                for package in self.record_counts
            },
        }


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _walk(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result[path] = child
            result.update(_walk(child, path))
    return result


def build_fact_catalog(run_root: Path) -> FactCatalog:
    root = Path(run_root).resolve()
    manifest_path = root / "facts" / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"facts manifest is missing: {manifest_path}")
    manifest = _read(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("facts manifest must be an object")
    packages = manifest.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("facts manifest packages must be an object")

    counts: dict[FactPackage, int] = {}
    fields: dict[FactPackage, tuple[str, ...]] = {}
    availability: dict[FactPackage, dict[str, int]] = {}
    paths: dict[FactPackage, str] = {}
    capabilities: dict[FactPackage, dict[str, dict[str, Any]]] = {}
    for raw_name, metadata in packages.items():
        try:
            package = FactPackage(raw_name)
        except ValueError:
            continue
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            raise ValueError(f"package metadata is invalid: {raw_name}")
        package_path = _safe_path(root, metadata["path"])
        if not package_path.is_file():
            raise ValueError(f"fact package is missing: {metadata['path']}")
        expected_hash = metadata.get("sha256")
        if expected_hash and _sha256(package_path) != expected_hash:
            raise ValueError(f"fact package SHA-256 mismatch: {metadata['path']}")
        package_doc = _read(package_path)
        records = package_doc.get("records") if isinstance(package_doc, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"fact package records must be an array: {metadata['path']}")
        if len(records) != metadata.get("record_count"):
            raise ValueError(f"fact package record_count mismatch: {metadata['path']}")
        field_counts: dict[str, int] = {}
        field_values: dict[str, list[Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            for field, value in _walk(record).items():
                field_counts.setdefault(field, 0)
                if value not in (None, "", [], {}):
                    field_counts[field] += 1
                    field_values.setdefault(field, []).append(value)
        counts[package] = len(records)
        fields[package] = tuple(sorted(field_counts))
        availability[package] = field_counts
        paths[package] = metadata["path"]
        package_capabilities: dict[str, dict[str, Any]] = {}
        for field in field_counts:
            values = field_values.get(field, [])
            numeric = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
            operations = ["count", "distribution"]
            if numeric:
                operations.extend(["sum", "mean", "median", "percentile"])
            package_capabilities[field] = {
                "available_count": field_counts.get(field, 0),
                "value_type": "number" if numeric and len(numeric) == len(values) else "mixed",
                "aggregations": operations,
                "filterable": True,
                "projectable": True,
            }
        capabilities[package] = package_capabilities

    run_id = manifest.get("run_id") or manifest.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("facts manifest run_id is required")
    return FactCatalog(run_id, manifest.get("schema_version"), counts, fields, availability, paths, capabilities)


def describe_analysis(run_root: Path, packages: list[str] | None = None, fields: list[str] | None = None) -> dict:
    """Small, verified discovery view; does not select facts or change an analysis."""
    try:
        from .analysis_delivery import field_summary
        from .analysis_result import _field_value, _field_available
    except ImportError:
        from analysis_delivery import field_summary
        from analysis_result import _field_value, _field_available
    root = Path(run_root).resolve()
    catalog = build_fact_catalog(root)
    labels = business_field_labels()
    result = {}
    for name in packages or ["script"]:
        package = FactPackage(name)
        if package not in catalog.package_paths:
            raise ValueError(f"fact package unavailable: {name}")
        known = catalog.fields[package]
        wanted = fields or [field for field in known if "." not in field and field not in {
            "source_paths", "data_availability", "audio_transcript", "brand_classification",
            "brand_rule_version", "classification_reason", "classification_status"}]
        unknown = set(wanted) - set(known)
        if unknown:
            raise ValueError(f"unknown fields in {name}: {', '.join(sorted(unknown))}")
        records = _read(_safe_path(root, catalog.package_paths[package]))["records"]
        result[name] = [field_summary(field, [(record.get("material_id"), _field_value(record, field),
                                              _field_available(record, field)) for record in records],
                                      label=labels.get(field)) for field in wanted]
    return {"runId": catalog.run_id, "packages": result, "externalCalls": False,
            "guidance": "样例仅供字段选型，不是完整正文。按业务问题提交事实选择；字段可用性与发布校验同口径。"}


__all__ = ["FactCatalog", "build_fact_catalog", "business_field_labels", "businessize_text"]
