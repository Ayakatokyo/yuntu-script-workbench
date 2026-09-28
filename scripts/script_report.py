"""Create a self-contained first-stage script report from verified fact packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import os

try:
    from script_report_html import render_script_report_html
except ImportError:
    from .script_report_html import render_script_report_html


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
    rows = []
    counts = {"ranking": len(packages["material_ranking"]), "detail": len(detail_by_id),
              "platform": 0, "agent_audio": 0, "unavailable": 0}
    for ranking in packages["material_ranking"]:
        material_id = str(ranking.get("material_id") or "")
        detail = detail_by_id.get(material_id, {})
        source = detail.get("script_source", "unavailable")
        if material_id in detail_by_id:
            counts[source] += 1
        rows.append({
            "materialId": material_id,
            "rank": ranking.get("rank"),
            "title": ranking.get("title") or detail.get("title") or "",
            "metrics": ranking.get("metrics", {}),
            "scriptSource": source,
            "scriptText": detail.get("script_text", ""),
            "platformScriptText": detail.get("platform_script_text", ""),
            "audioTranscript": detail.get("audio_transcript"),
            "contentFormula": detail.get("content_formula"),
            "creativeBreakdown": detail.get("creative_breakdown"),
            "sourcePaths": detail.get("source_paths", {}),
        })
    for material_id, detail in detail_by_id.items():
        if material_id not in {row["materialId"] for row in rows}:
            raise ScriptReportError(f"detail_not_in_ranking:{material_id}")
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
        "brandScope": brand_scope,
        "brands": brands,
        "titleBrandUnmatchedCount": (sum(not any(brand in str(row["title"]) for brand in brands)
                                         for row in rows) if brand_scope == "SPECIFIED_BRANDS" and brands else None),
    }
    payload = {"schemaVersion": 1, "runId": manifest.get("run_id"), "counts": counts,
               "selection": selection, "rows": rows, "factsManifestSha256": _hash(manifest_path)}
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
