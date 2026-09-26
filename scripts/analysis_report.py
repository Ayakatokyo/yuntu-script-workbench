"""Render the fixed, auditable Markdown report for validated analysis results."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterator

try:
    from .analysis_result import (
        ALLOWED_SECTIONS,
        format_validation_errors,
        normalize_analysis_result,
        validate_analysis_result,
    )
    from .analysis_spec import AnalysisSpec
    from .fact_catalog import business_field_labels, businessize_text
except ImportError:  # pragma: no cover - direct collector execution
    from analysis_result import (
        ALLOWED_SECTIONS,
        format_validation_errors,
        normalize_analysis_result,
        validate_analysis_result,
    )
    from analysis_spec import AnalysisSpec
    from fact_catalog import business_field_labels, businessize_text


FIXED_SECTIONS = (
    ("material_ranking", "三、素材榜单分析"),
    ("audience", "四、人群分析"),
    ("script", "五、脚本分析"),
    ("highlight_frames", "六、高光帧分析"),
)

SECTION_LABELS = {
    "material_ranking": "素材榜单",
    "audience": "人群分析",
    "script": "脚本分析",
    "highlight_frames": "高光帧分析",
}
FINDING_TYPE_LABELS = {"fact": "数据事实", "inference": "商业解读", "recommendation": "行动建议"}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
FACT_SOURCE_LABELS = {
    "facts/material-ranking.json": "素材榜单事实",
    "facts/audience.json": "人群分析事实",
    "facts/script.json": "脚本内容事实",
    "facts/highlight-frames.json": "高光画面事实",
}


def _package_label(value: Any) -> str:
    raw = str(value)
    path = raw.split("#/", 1)[0]
    if path in FACT_SOURCE_LABELS:
        return FACT_SOURCE_LABELS[path]
    return SECTION_LABELS.get(raw, businessize_text(raw))


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(_text(item) for item in value) or "无"
    if isinstance(value, dict):
        if "claim" in value:
            return businessize_text(value["claim"])
        return "; ".join(f"{businessize_text(key)}：{businessize_text(val)}" for key, val in value.items())
    return businessize_text(value)


def _md_cell(value: Any) -> str:
    return businessize_text(value).replace("|", "\\|").replace("\n", " ")


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        return "、".join(str(item) for item in value[:5])
    return str(value)


def _business_values(record: Any, *, limit: int = 6) -> list[tuple[str, str]]:
    if not isinstance(record, dict):
        return []
    labels = business_field_labels()
    flattened: list[tuple[str, Any]] = []

    def visit(value: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                visit(child, path)
        elif isinstance(value, list):
            if value and all(not isinstance(item, (dict, list)) for item in value):
                flattened.append((prefix, value))
        elif value not in (None, ""):
            flattened.append((prefix, value))

    visit(record)
    excluded = {
        "material_id", "title", "material_name", "source_ref", "sourceRef",
        "brand_classification.source", "brand_classification.rule_version",
        "brand_classification.match_tokens", "brand_classification.matchedBrands",
    }
    priority = ("rank", "brand_classification.value", "metrics.", "raw_people_tags", "timestamp_sec")
    candidates = []
    for path, value in flattened:
        if path in excluded or path.startswith(("source_", "artifact.", "data_availability.")):
            continue
        label = labels.get(path)
        if not label:
            continue
        order = next((index for index, prefix in enumerate(priority) if path == prefix or path.startswith(prefix)), len(priority))
        candidates.append((order, path, label, _display_value(value)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(label, display) for _order, _path, label, display in candidates[:limit]]


def _evidence_display(ref: str, metadata: dict) -> tuple[str, str]:
    record = metadata.get("fact_index", {}).get(ref)
    if not isinstance(record, dict):
        return "事实记录", "暂无可展示的结构化数值"
    object_label = next(
        (str(record.get(key)).strip() for key in ("title", "material_name") if record.get(key)),
        "",
    )
    if not object_label:
        rank = record.get("rank")
        material_id = record.get("material_id")
        parts = []
        if rank not in (None, ""):
            parts.append(f"榜单第 {rank} 名")
        if material_id not in (None, ""):
            parts.append(f"素材 {material_id}")
        object_label = " · ".join(parts) or "事实记录"
    values = _business_values(record)
    summary = "；".join(f"{label}：{value}" for label, value in values)
    return object_label, summary or "暂无可展示的结构化数值"


def _finding_explanation(finding: dict) -> str | None:
    explanation = finding.get("brand_proxy_explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        proxy = finding.get("brand_proxy")
        explanation = proxy.get("explanation") if isinstance(proxy, dict) else None
    if isinstance(explanation, str) and explanation.strip():
        return businessize_text(explanation.strip())
    return None


def _finding_ref_ids(finding: dict, evidence_ids: dict[str, str]) -> list[str]:
    return [evidence_ids[ref] for ref in finding.get("fact_refs", []) if ref in evidence_ids]


def _format_evidence_ids(finding: dict, evidence_ids: dict[str, str]) -> str:
    return " ".join(f"[依据{int(identifier[1:])}]" for identifier in _finding_ref_ids(finding, evidence_ids))


def _findings_for_section(grouped: dict[str, list[dict]], section: str) -> list[dict]:
    return [
        finding
        for result in grouped.get(section, [])
        for finding in result.get("findings", [])
    ]


def _iter_findings_in_report_order(
    grouped: dict[str, list[dict]], custom_names: list[str]
) -> Iterator[tuple[str, dict]]:
    for section, _heading in FIXED_SECTIONS:
        for finding in _findings_for_section(grouped, section):
            yield section, finding
    for custom_name in custom_names:
        for finding in _findings_for_section(grouped, custom_name):
            yield custom_name, finding


def _build_evidence_audit(
    grouped: dict[str, list[dict]], custom_names: list[str]
) -> tuple[dict[str, str], list[dict]]:
    """Assign stable evidence and conclusion IDs in rendered section order."""
    evidence_ids: dict[str, str] = {}
    audits: list[dict] = []
    for section, finding in _iter_findings_in_report_order(grouped, custom_names):
        ref_ids: list[str] = []
        for ref in finding.get("fact_refs", []):
            if ref not in evidence_ids:
                evidence_ids[ref] = f"E{len(evidence_ids) + 1}"
            ref_ids.append(evidence_ids[ref])
        audits.append({
            "id": f"C{len(audits) + 1}",
            "section": section,
            "finding": finding,
            "evidence_ids": ref_ids,
        })
    return evidence_ids, audits


def _render_finding_line(finding: dict, evidence_ids: dict[str, str]) -> str:
    refs = _format_evidence_ids(finding, evidence_ids)
    return f"- {businessize_text(finding['claim'])}" + (f" {refs}" if refs else "")


def _render_brand_explanations(findings: list[dict]) -> list[str]:
    explanations: list[str] = []
    for finding in findings:
        explanation = _finding_explanation(finding)
        if explanation and explanation not in explanations:
            explanations.append(explanation)
    if not explanations:
        return []
    lines = ["### 品牌代理说明", ""]
    lines.extend(f"> {explanation}" for explanation in explanations)
    lines.append("")
    return lines


def _render_section_body(
    section: str,
    grouped: dict[str, list[dict]],
    evidence_ids: dict[str, str],
    excluded: set[int],
) -> list[str]:
    findings = _findings_for_section(grouped, section)
    lines = _render_brand_explanations(findings)
    visible = [
        finding for finding in findings
        if id(finding) not in excluded and finding.get("type") != "recommendation"
    ]

    lines.extend(["### 核心结论", ""])
    core_refs = sorted(
        {
            identifier
            for finding in findings
            if id(finding) in excluded and finding.get("type") in {"fact", "inference"}
            for identifier in _finding_ref_ids(finding, evidence_ids)
        },
        key=lambda value: int(value[1:]),
    )
    if core_refs:
        lines.append(f"- 本章节核心结论已在上文汇总（{' '.join(f'[依据{int(ref[1:])}]' for ref in core_refs)}）。")
    else:
        lines.append("- 本次没有独立核心结论。")
    lines.append("")

    facts = [finding for finding in visible if finding.get("type") == "fact"]
    lines.extend(["### 数据表现", ""])
    lines.extend(
        [_render_finding_line(finding, evidence_ids) for finding in facts]
        or ["- 本次没有新增数据表现。"]
    )
    lines.append("")

    inferences = [finding for finding in visible if finding.get("type") == "inference"]
    lines.extend(["### 商业解读", ""])
    lines.extend(
        [_render_finding_line(finding, evidence_ids) for finding in inferences]
        or ["- 本次没有新增商业解读。"]
    )
    lines.append("")

    gaps = [gap for result in grouped.get(section, []) for gap in result.get("gaps", [])]
    questions = [
        question
        for result in grouped.get(section, [])
        for question in result.get("next_questions", [])
    ]
    lines.extend(["### 数据缺口与待验证问题", ""])
    if gaps or questions:
        lines.extend(f"- 数据缺口：{_text(gap)}" for gap in gaps)
        lines.extend(f"- 待验证问题：{_text(question)}" for question in questions)
    else:
        lines.append("- 当前模块没有数据缺口或待验证问题。")
    lines.append("")
    return lines


def _render_brand_audit(metadata: dict) -> list[str]:
    brand_metadata = metadata.get("brand_classification")
    if not isinstance(brand_metadata, dict):
        return ["- 当前事实包没有品牌分类 manifest 元数据；未使用默认品牌表。"]
    candidates = brand_metadata.get("candidate_brands", [])
    if not isinstance(candidates, list):
        candidates = []
    counts = brand_metadata.get("counts", {})
    if not isinstance(counts, dict):
        counts = {}
    lines = [
        "- 品牌候选范围：" + ("、".join(str(item) for item in candidates) or "未指定"),
        f"- 品牌规则版本：`{brand_metadata.get('rule_version', 'unknown')}`",
        "- 品牌分类计数：" + ("；".join(f"{key}={value}" for key, value in counts.items()) or "无"),
        f"- 未知品牌数量：{brand_metadata.get('unknown_count', 'unknown')}",
    ]
    records = metadata.get("brand_audit_records", [])
    if not isinstance(records, list):
        records = []
    lines.append("- 分类审计（命中品牌 / 命中依据）：")
    if not records:
        lines.append("  - 当前选择没有可展示的榜单分类记录。")
        return lines
    for record in records[:20]:
        if not isinstance(record, dict):
            continue
        classification = record.get("brand_classification", {})
        if not isinstance(classification, dict):
            classification = {}
        matched = classification.get("matchedBrands", [])
        tokens = classification.get("match_tokens", {})
        token_text = "；".join(
            f"{brand}：{'、'.join(str(item) for item in values)}"
            for brand, values in tokens.items()
            if isinstance(values, list)
        ) if isinstance(tokens, dict) else ""
        lines.append(
            f"  - 素材ID：{record.get('material_id', '未知')}；"
            f"命中品牌：{'、'.join(str(item) for item in matched) or '无'}；"
            f"命中依据：{token_text or '无'}"
        )
    if len(records) > 20:
        lines.append(f"  - 其余 {len(records) - 20} 条分类记录保留在事实包中。")
    return lines


def _summary_findings(spec: AnalysisSpec, grouped: dict[str, list[dict]]) -> list[dict]:
    """Promote at most one factual/inferential conclusion per selected package."""
    selected = set(spec.sections)
    promoted: list[dict] = []
    for section, _heading in FIXED_SECTIONS:
        if section not in selected:
            continue
        candidates = [
            finding for finding in _findings_for_section(grouped, section)
            if finding.get("type") in {"fact", "inference"}
        ]
        if candidates:
            promoted.append(candidates[0])
    return promoted


def _is_cross_module_finding(finding: dict) -> bool:
    if finding.get("type") == "recommendation":
        return False
    if finding.get("cross_module") is True:
        return True
    packages = {ref.split("#/", 1)[0] for ref in finding.get("fact_refs", [])}
    return len(packages) > 1


def _recommendation_findings(
    spec: AnalysisSpec, grouped: dict[str, list[dict]], custom_names: list[str]
) -> list[dict]:
    sections = list(spec.sections) + custom_names
    return [
        finding
        for section in sections
        for finding in _findings_for_section(grouped, section)
        if finding.get("type") == "recommendation"
        and not _is_cross_module_finding(finding)
    ]


def _render_audit_appendices(
    evidence_ids: dict[str, str], audits: list[dict], metadata: dict
) -> list[str]:
    support_counts = {ref: 0 for ref in evidence_ids}
    for audit in audits:
        for ref in set(audit["finding"].get("fact_refs", [])):
            if ref in support_counts:
                support_counts[ref] += 1
    lines = [
        "## 附录 A：证据索引",
        "",
        "下表直接展示每条依据对应的数据对象、真实业务数值，以及支持多少条结论；技术路径仅放在折叠追溯区。",
        "",
        "| 依据 | 业务来源 | 数据对象 | 真实数据参考 | 支持结论 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    if evidence_ids:
        for ref, identifier in evidence_ids.items():
            path, _, pointer = ref.partition("#")
            object_label, data_summary = _evidence_display(ref, metadata)
            lines.append(
                f"| 依据{int(identifier[1:])} | {FACT_SOURCE_LABELS.get(path, '分析事实来源')} | "
                f"{_md_cell(object_label)} | {_md_cell(data_summary)} | {support_counts.get(ref, 0)} 条 |"
            )
    else:
        lines.append("| — | 本次没有事实结论引用 | — | — | 0 条 |")

    lines.extend([
        "",
        "## 附录 B：结论审计",
        "",
        "| 结论编号 | 结论摘要 | 业务类型 | 分析板块 | 可信度 | 主要依据 | 证据数据参考 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    if audits:
        proxy_explanations: list[str] = []
        for audit in audits:
            finding = audit["finding"]
            refs = "、".join(f"依据{int(identifier[1:])}" for identifier in audit["evidence_ids"])
            data_reference = "；".join(
                _evidence_display(ref, metadata)[1]
                for ref in finding.get("fact_refs", [])[:2]
            ) or "暂无可展示的结构化数值"
            lines.append(
                f"| {audit['id']} | {_md_cell(finding['claim'])} | "
                f"{FINDING_TYPE_LABELS.get(finding['type'], '分析结论')} | "
                f"{SECTION_LABELS.get(audit['section'], audit['section'])} | "
                f"{CONFIDENCE_LABELS.get(finding['confidence'], finding['confidence'])} | {refs or '无'} | "
                f"{_md_cell(data_reference)} |"
            )
            explanation = _finding_explanation(finding)
            if explanation and explanation not in proxy_explanations:
                proxy_explanations.append(explanation)
        lines.extend(f"- 品牌代理说明：{item}" for item in proxy_explanations)
    else:
        lines.append("| — | 本次没有可审计结论 | — | — | — | — | — |")

    lines.extend(["", "### 品牌分类审计", ""])
    lines.extend(_render_brand_audit(metadata))
    lines.extend(["", "<details>", "<summary>技术追溯详情（复核用）</summary>", ""])
    if evidence_ids:
        lines.extend(f"- {identifier}：`{ref}`" for ref, identifier in evidence_ids.items())
    else:
        lines.append("- 无")
    lines.extend(["", "</details>"])
    return lines


def _render_phase_two_chain(metadata: dict) -> list[str]:
    phase_two = metadata.get("phase_two")
    if not isinstance(phase_two, dict):
        return []
    intent = phase_two.get("intent", {})
    if not isinstance(intent, dict):
        intent = {}
    selection = phase_two.get("selection_spec", {})
    if not isinstance(selection, dict):
        selection = {}
    summary = phase_two.get("selection_summary", {})
    if not isinstance(summary, dict):
        summary = {}
    source_counts = summary.get("source_record_counts", {})
    selected_counts = summary.get("selected_record_counts", {})
    gaps = phase_two.get("data_gaps", [])
    if not isinstance(gaps, list):
        gaps = []
    original_question = phase_two.get("business_question", intent.get("original_question", "未记录"))
    metrics = [businessize_text(item) for item in intent.get("metrics", []) if isinstance(item, str)]
    dimensions = [businessize_text(item) for item in intent.get("dimensions", []) if isinstance(item, str)]
    package_order = list(dict.fromkeys([*source_counts, *selected_counts, *selection.get("packages", [])]))
    lines = [
        "## 分析链路（透明度记录）",
        "",
        f"- 用户商业问题：{original_question}",
        "- 关注指标：" + ("、".join(metrics) or "按用户原问题分析"),
        "- 对比维度：" + ("、".join(dimensions) or "未指定"),
        "- 使用事实：" + ("、".join(_package_label(item) for item in selection.get("packages", [])) or "未记录"),
        "",
        "| 事实来源 | 原始记录 | 本次选中 |",
        "| --- | ---: | ---: |",
    ]
    if package_order:
        lines.extend(
            f"| {_package_label(package)} | {source_counts.get(package, '—')} | {selected_counts.get(package, '—')} |"
            for package in package_order
        )
    else:
        lines.append("| 未记录 | — | — |")
    selection_rules = summary.get("selection_rules") or selection.get("filters", [])
    aggregates = phase_two.get("aggregates", {})
    truncation = summary.get("truncation", {})
    if selection_rules:
        lines.extend(["", "- 取数条件：" + _text(selection_rules)])
    if aggregates:
        lines.append("- 汇总结果：" + _text(aggregates))
    if truncation:
        lines.append("- 样本截断说明：" + _text(truncation))
    unsupported = intent.get("unsupported_requests", [])
    if unsupported:
        lines.append("- 本次不支持的请求：" + _text(unsupported))
    lines.append("- 事实选择缺口：")
    if gaps:
        lines.extend(f"  - {_text(gap)}" for gap in gaps)
    else:
        lines.append("  - 无")
    lines.append("")
    return lines


def render_analysis_report(spec: AnalysisSpec, results: list[dict], metadata: dict) -> str:
    if not isinstance(spec, AnalysisSpec):
        raise ValueError("spec must be an AnalysisSpec")
    if not isinstance(results, list):
        raise ValueError("results must be an array")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict")
    required_metadata = ("run_id", "analysis_time", "facts_version", "prompt_version", "fact_index")
    missing = [name for name in required_metadata if name not in metadata]
    if missing:
        raise ValueError("报告元数据缺失: " + ", ".join(missing))

    fact_index = metadata["fact_index"]
    selected = set(spec.sections)
    custom_names = [item if isinstance(item, str) else item["name"] for item in spec.custom_sections]
    custom_names = [name for name in custom_names if isinstance(name, str) and name.strip()]
    allowed = selected | set(custom_names)
    result_modules = ALLOWED_SECTIONS | set(custom_names)
    errors: list[str] = []
    normalized_results: list[dict] = []
    for result in results:
        normalized = normalize_analysis_result(result)
        errors.extend(validate_analysis_result(normalized, fact_index, result_modules))
        normalized_results.append(normalized)
    if errors:
        raise ValueError("校验失败，禁止渲染报告: " + format_validation_errors(errors))

    unknown = sorted({result["section"] for result in normalized_results if result.get("section") not in allowed})
    if unknown:
        raise ValueError("报告包含未选择的结果章节: " + ", ".join(unknown))
    grouped: dict[str, list[dict]] = {}
    for result in normalized_results:
        section = result["section"]
        if section in allowed:
            grouped.setdefault(section, []).append(result)
    missing_sections = sorted(selected - set(grouped))
    if missing_sections:
        raise ValueError("报告缺少所选章节的结果或数据缺口: " + ", ".join(missing_sections))

    generated_at = metadata.get("analysis_time") or datetime.now(timezone.utc).isoformat()
    coverage = metadata.get("coverage", {})
    package_paths = metadata.get("package_paths", [])
    if not package_paths:
        package_paths = sorted({ref.split("#/", 1)[0] for ref in fact_index})
    coverage_text = "；".join(f"{businessize_text(key)}：{value}" for key, value in coverage.items()) or "未记录"
    selected_section_labels = "、".join(SECTION_LABELS.get(section, section) for section in spec.sections)
    source_labels = "、".join(dict.fromkeys(_package_label(path) for path in package_paths)) or "未记录"
    evidence_ids, audits = _build_evidence_audit(grouped, custom_names)
    summary = _summary_findings(spec, grouped)
    promoted = {id(finding) for finding in summary}
    recommendations = _recommendation_findings(spec, grouped, custom_names)
    cross_findings = [
        audit["finding"] for audit in audits if _is_cross_module_finding(audit["finding"])
    ]
    cross_ids = {id(finding) for finding in cross_findings}

    lines = ["# 云图商业分析报告", "", "## 一、核心结论", ""]
    if summary:
        lines.extend(_render_finding_line(finding, evidence_ids) for finding in summary)
    else:
        lines.append("- 本次没有可进入核心结论的事实或推断；行动建议见下一节。")

    lines.extend(["", "## 二、行动建议", ""])
    if recommendations:
        lines.extend(_render_finding_line(finding, evidence_ids) for finding in recommendations)
    else:
        lines.append("- 未生成行动建议。")

    for section, heading in FIXED_SECTIONS:
        lines.extend(["", f"## {heading}", ""])
        if section not in selected:
            lines.append("- 本次未选择该模块。")
        else:
            lines.extend(_render_section_body(section, grouped, evidence_ids, promoted | cross_ids))

    for custom_name in custom_names:
        lines.extend(["", f"## {custom_name}", ""])
        lines.extend(_render_section_body(custom_name, grouped, evidence_ids, promoted | cross_ids))

    if len(selected) >= 2 and cross_findings:
        lines.extend(["", "## 七、跨模块观察", ""])
        lines.append("- 标签共现和指标关联仅表示观察，相关不等于因果。")
        lines.extend(_render_finding_line(finding, evidence_ids) for finding in cross_findings)

    lines.extend([
        "",
        *_render_phase_two_chain(metadata),
        "",
        "## 八、数据限制与下一步",
        "",
        "### 本次分析范围与方法",
        "",
        f"- 分析任务编号：`{spec.analysis_id}`",
        f"- 运行编号：`{metadata['run_id']}`",
        f"- 分析时间：`{generated_at}`",
        f"- 事实包版本：`{metadata['facts_version']}`；提示词版本：`{metadata['prompt_version']}`；模型请求版本：`{metadata.get('request_version', 2)}`",
        f"- 选择模块：{selected_section_labels}；商业问题：{spec.business_question}",
        f"- 样本覆盖：{coverage_text}",
        f"- 品牌筛选：{', '.join(spec.filters.get('brand_filter', ())) or '未指定'}",
        "- 事实来源：" + source_labels,
        "- 方法：仅向当前对话模型提供已选择、已校验的事实包，并要求每条结论保留可追溯证据编号。",
        "",
        "### 数据限制",
        "",
        "- 脚本源数据为空时只能记录为数据缺口；不得用标题或标签替代脚本。",
        "- 素材分组未提供品牌名称时，标题/标签品牌识别只能作为品牌代理推断，并降低可信度。",
        "- 相关线索不等于因果关系；高光画面不得直接推导用户心理或转化原因。",
    ])
    lines.extend(["", *_render_audit_appendices(evidence_ids, audits, metadata)])
    return "\n".join(lines).rstrip() + "\n"
