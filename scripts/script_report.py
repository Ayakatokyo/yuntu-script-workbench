"""Create a self-contained first-stage script report from verified fact packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import os

try:
    from script_report_html import _formula_overview, render_script_report_html
    from brand_dimension import BrandDimensionError, build_brand_dimension
    from script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
    from material_field_registry import default_material_field_registry
    from single_material_overview import _merge_detail_rate_metrics, _project_core, _project_formula
except ImportError:
    from .script_report_html import _formula_overview, render_script_report_html
    from .brand_dimension import BrandDimensionError, build_brand_dimension
    from .script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
    from .material_field_registry import default_material_field_registry
    from .single_material_overview import _merge_detail_rate_metrics, _project_core, _project_formula


class ScriptReportError(ValueError):
    pass


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_script_report(run_dir: Path) -> dict:
    root = Path(run_dir).resolve()
    summary_path = root / "run-summary.json"
    if not summary_path.is_file():
        raise ScriptReportError("collection_not_ready_for_script_report")
    summary = _read(summary_path)
    if summary.get("workflow", {}).get("stage") != "ready_for_analysis":
        raise ScriptReportError("collection_not_ready_for_script_report")
    facts_root = root / "facts"
    manifest_path = facts_root / "manifest.json"
    if not manifest_path.is_file():
        raise ScriptReportError("facts_manifest_missing")
    manifest = _read(manifest_path)
    for artifact in manifest.get("source_artifacts", []):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ScriptReportError("source_artifact_invalid")
        source = (root / artifact["path"]).resolve()
        if not source.is_relative_to(root) or not source.is_file() or _hash(source) != artifact.get("sha256"):
            raise ScriptReportError(f"source_artifact_hash_mismatch:{artifact['path']}")
    packages = {}
    for name in ("material_ranking", "script"):
        metadata = manifest.get("packages", {}).get(name)
        if not isinstance(metadata, dict):
            raise ScriptReportError(f"fact_package_missing:{name}")
        path = (root / metadata["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or _hash(path) != metadata.get("sha256"):
            raise ScriptReportError(f"fact_package_hash_mismatch:{name}")
        packages[name] = _read(path)["records"]
    detail_by_id = {str(item["material_id"]): item for item in packages["script"]}
    registry = default_material_field_registry()
    rows = []
    counts = {"ranking": len(packages["material_ranking"]), "detail": len(detail_by_id),
              "platform": 0, "agent_audio": 0, "unavailable": 0}
    for ranking in packages["material_ranking"]:
        material_id = str(ranking.get("material_id") or "")
        detail = detail_by_id.get(material_id, {})
        source = detail.get("script_source", "unavailable")
        if material_id in detail_by_id:
            counts[source] += 1
        core_record = _merge_detail_rate_metrics(root, material_id, ranking) if detail else ranking
        rows.append({
            "materialId": material_id,
            "rank": ranking.get("rank"),
            "title": ranking.get("title") or detail.get("title") or "",
            "publishTime": ranking.get("publish_time"),
            "videoDuration": ranking.get("video_duration"),
            "materialUri": ranking.get("material_uri"),
            "materialTags": ranking.get("material_tags"),
            "metrics": ranking.get("metrics", {}),
            "core": _project_core(core_record, registry) if detail else None,
            "formula": _project_formula(detail) if detail else None,
            "scriptSource": source,
            "scriptText": detail.get("script_text", ""),
            "platformScriptText": detail.get("platform_script_text", ""),
            "audioTranscript": detail.get("audio_transcript"),
            "contentFormula": detail.get("content_formula"),
            "contentFormulaAvailable": detail.get("data_availability", {}).get("content_formula", {}).get("available") is True,
            "creativeBreakdown": detail.get("creative_breakdown"),
            "sourcePaths": detail.get("source_paths", {}),
        })
    for material_id, detail in detail_by_id.items():
        if material_id not in {row["materialId"] for row in rows}:
            raise ScriptReportError(f"detail_not_in_ranking:{material_id}")
    try:
        brand_overview = build_brand_dimension(root)
    except BrandDimensionError:
        brand_overview = {"schemaVersion": 1, "status": "unavailable", "tabs": [],
                          "materialIdsByTab": {}, "views": {},
                          "gaps": [{"code": "brand_dimension_unavailable"}]}
    if brand_overview["status"] == "available":
        ranking_ids = [row["materialId"] for row in rows]
        classified_ids = brand_overview["materialIdsByTab"]["all"]
        if len(ranking_ids) != len(classified_ids) or set(ranking_ids) != set(classified_ids):
            raise ScriptReportError("brand_classification_ranking_ids_mismatch")
        views = {}
        for tab in brand_overview["tabs"]:
            tab_id = tab["id"]
            material_ids = set(brand_overview["materialIdsByTab"][tab_id])
            if isinstance(summary.get("artifacts"), dict):
                try:
                    scoped_formula = build_script_formula_overview(root, material_ids)
                except ScriptFormulaOverviewError as exc:
                    raise ScriptReportError(f"brand_formula_unavailable:{tab_id}") from exc
            else:
                # Older, already hash-verified runs may lack the detail artifact index.
                scoped_formula = _formula_overview([
                    row for row in rows if row["materialId"] in material_ids and row["sourcePaths"]])
            views[tab_id] = {"scriptFormulaOverview": scoped_formula}
        brand_overview["views"] = views
    contract = summary.get("collectionContract")
    contract = contract if isinstance(contract, dict) else {}
    list_params = contract.get("listParams") or {}
    brand_scope = list_params.get("brand_scope_type", "ALL_INDUSTRY")
    brands = list_params.get("brands") or []
    selection = {
        "industry": list_params.get("industry"),
        "period": contract.get("detailDateRange"),
        "rankingMetric": list_params.get("ranking_limit_type"),
        "targetTopN": contract.get("targetTopN"),
        "materialList": contract.get("materialList"),
        "listParams": {key: value for key, value in list_params.items()
                       if key not in {"industry", "date_type", "custom_start_date", "custom_end_date",
                                      "ranking_limit_type", "brand_scope_type", "brands"}
                       and not any(token in key.lower() for token in
                                   ("shop", "account", "advertiser", "token", "secret", "credential", "auth"))},
        "brandScope": brand_scope,
        "brands": brands,
        "titleBrandUnmatchedCount": (sum(not any(brand in str(row["title"]) for brand in brands)
                                         for row in rows) if brand_scope == "SPECIFIED_BRANDS" and brands else None),
    }
    payload = {"schemaVersion": 1, "runId": manifest.get("run_id"), "counts": counts,
               "selection": selection, "rows": rows,
               "detailMaterialIds": [row["materialId"] for row in rows if row["materialId"] in detail_by_id],
               "scriptFormulaOverview": (brand_overview["views"]["all"]["scriptFormulaOverview"]
                                         if brand_overview["status"] == "available"
                                         else _formula_overview([row for row in rows if row["sourcePaths"]])),
               "brandOverview": brand_overview,
               "factsManifestSha256": _hash(manifest_path)}
    output_dir = root / "script-report"
    data_path = output_dir / "report.json"
    _write(data_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    html = render_script_report_html(payload)
    html_path = output_dir / "index.html"
    _write(html_path, html)
    receipt = {"reportPath": str(html_path), "dataPath": str(data_path), "htmlSha256": _hash(html_path),
               "dataSha256": _hash(data_path), "factsManifestSha256": _hash(manifest_path), "counts": counts,
               "workflowState": "awaitingAnalysisInput"}
    _write(output_dir / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return receipt
