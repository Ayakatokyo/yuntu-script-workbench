"""Create a self-contained first-stage script report from verified fact packages."""

from __future__ import annotations

from html import escape
import hashlib
import json
from pathlib import Path
import tempfile
import os


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
    if not summary_path.is_file() or _read(summary_path).get("workflow", {}).get("stage") != "ready_for_analysis":
        raise ScriptReportError("collection_not_ready_for_script_report")
    facts_root = root / "facts"
    manifest_path = facts_root / "manifest.json"
    if not manifest_path.is_file():
        raise ScriptReportError("facts_manifest_missing")
    manifest = _read(manifest_path)
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
              "platform": 0, "asr": 0, "unavailable": 0}
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
            "asr": detail.get("asr_transcript"),
            "sourcePaths": detail.get("source_paths", {}),
        })
    for material_id, detail in detail_by_id.items():
        if material_id not in {row["materialId"] for row in rows}:
            raise ScriptReportError(f"detail_not_in_ranking:{material_id}")
    payload = {"schemaVersion": 1, "runId": manifest.get("run_id"), "counts": counts,
               "rows": rows, "factsManifestSha256": _hash(manifest_path)}
    output_dir = root / "script-report"
    data_path = output_dir / "report.json"
    _write(data_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    details_html = []
    for row in rows:
        if row["materialId"] not in detail_by_id:
            continue
        asr = row["asr"] if isinstance(row["asr"], dict) else {}
        status = asr.get("status", "not_needed" if row["scriptSource"] == "platform" else "not_processed")
        segments = asr.get("segments", []) if status == "available" else []
        segment_html = "".join(f"<li>{escape(str(item['start']))}–{escape(str(item['end']))} 秒：{escape(item['text'])}</li>" for item in segments)
        details_html.append(
            f"<article id='m-{escape(row['materialId'])}'><h2>{escape(str(row['rank']))}. {escape(str(row['title']))}</h2>"
            f"<p>素材 ID：{escape(row['materialId'])} · 脚本来源：{escape(row['scriptSource'])} · 转写状态：{escape(str(status))}</p>"
            f"<pre>{escape(row['scriptText']) if row['scriptText'] else '无可用脚本文本'}</pre>"
            f"<details><summary>平台原文与转写时间码</summary><p>平台原文</p><pre>{escape(row['platformScriptText'])}</pre>"
            f"<ol>{segment_html}</ol></details></article>"
        )
    leaderboard = "".join(
        f"<tr><td>{escape(str(row['rank']))}</td><td><a href='#m-{escape(row['materialId'])}'>{escape(str(row['title']))}</a>"
        f"<small>{escape(row['materialId'])}</small></td><td>{escape(row['scriptSource'])}</td></tr>"
        if row["materialId"] in detail_by_id else
        f"<tr><td>{escape(str(row['rank']))}</td><td>{escape(str(row['title']))}<small>{escape(row['materialId'])}</small></td><td>未获取详情</td></tr>"
        for row in rows
    )
    html = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>云图素材脚本工作台</title><style>body{{font:16px system-ui,sans-serif;color:#173252;background:#f5f9ff;margin:0 auto;max-width:1100px;padding:24px}}h1{{color:#1459a8}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;border-bottom:1px solid #dce6f1;text-align:left}}small{{display:block;color:#60758b}}article{{background:white;margin:18px 0;padding:20px;border-radius:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}}a{{color:#1259aa}}.summary{{display:flex;gap:12px;flex-wrap:wrap}}.summary span{{background:white;padding:12px;border-radius:9px}}</style>
<main><h1>云图素材脚本工作台</h1><p>第一阶段事实报告。平台脚本与音轨转写分开保存；转写仅覆盖可识别语音，不代表画面文字。</p>
<div class='summary'><span>榜单 {counts['ranking']}</span><span>详情 {counts['detail']}</span><span>平台脚本 {counts['platform']}</span><span>ASR {counts['asr']}</span><span>无脚本 {counts['unavailable']}</span></div>
<h2>素材榜单</h2><table><thead><tr><th>排名</th><th>素材</th><th>脚本来源</th></tr></thead><tbody>{leaderboard}</tbody></table>
<h2>已获详情与脚本</h2>{''.join(details_html)}<p>数据与来源路径见同目录 report.json；用户提出分析问题后再进入第二阶段。</p></main></html>"""
    html_path = output_dir / "index.html"
    _write(html_path, html)
    receipt = {"reportPath": str(html_path), "dataPath": str(data_path), "htmlSha256": _hash(html_path),
               "dataSha256": _hash(data_path), "factsManifestSha256": _hash(manifest_path), "counts": counts,
               "workflowState": "awaitingAnalysisInput"}
    _write(output_dir / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return receipt
