"""Build and validate the request handed to the current conversation model."""

from __future__ import annotations

try:
    from .analysis_prompt import build_prompt_packet
    from .analysis_spec import AnalysisSpec
    from .fact_catalog import FactCatalog
except ImportError:  # pragma: no cover - direct script imports
    from analysis_prompt import build_prompt_packet
    from analysis_spec import AnalysisSpec
    from fact_catalog import FactCatalog


REQUEST_TYPE = "yuntu_commercial_analysis"
REQUEST_VERSION = 2
HANDOFF_PROTOCOL = "yuntu-current-conversation-model-v2"
RESPONSE_SCHEMA = "AnalysisResult"
FACT_SELECTION_REQUEST_TYPE = "yuntu_fact_selection"
FACT_SELECTION_REQUEST_VERSION = 1
FACT_SELECTION_RESPONSE_SCHEMA = "FactSelectionSpec"

BASE_SYSTEM_RULES = (
    "只使用给定事实，不补充外部资料或未提供的记录。",
    "每条结论必须带 fact_refs 及 field_refs；field_refs 将每个记录引用映射到实际使用字段数组。原话 quotes 提供 fact_ref、field、text 并逐字命中来源。",
    "相关不等于因果；不得把相关、共现或高光解释表述为因果。",
    "品牌代理识别必须标记为推断并降低置信度，且只能使用运行时品牌候选范围。",
    "脚本字段为 source_empty 时只能进入 gaps，不得用标题或标签替代脚本。",
)

REPORT_PRESENTATION_GUIDANCE = {
    "layout_policy": "flexible",
    "validation_mode": "advisory",
    "evidence_index": [
        "证据索引优先展示素材名称或榜单位置、业务字段名称和真实数据值，不以事实路径、原始字段名或只有编号的表格作为主要内容。",
        "有两条及以上可比较数据时，至少用一张合适的可视化呈现关键指标；图表类型由报告设计自行选择。",
    ],
    "conclusion_audit": [
        "每条结论除依据编号外，展示一组可直接核对的真实数据参考；不可比较时明确说明数据缺口。",
        "优先用真实业务指标对比图；数据不适合横向比较时，可用结论类型、可信度或证据覆盖的可视化辅助阅读。",
    ],
    "delivery_workflow": [
        "先一次性确定报告信息架构、数据字段和文件清单，再集中生成 HTML 报告组件；只在明确校验失败或用户反馈后局部修正。",
        "不固定页面布局、图表数量或组件实现，不因展示偏好增加模型轮次或阻断式校验。",
    ],
}


def build_fact_selection_request(spec: AnalysisSpec, catalog: FactCatalog) -> dict:
    """Create a handoff asking the current model to select persisted facts."""
    if not isinstance(spec, AnalysisSpec):
        raise ValueError("spec must be an AnalysisSpec")
    if not isinstance(catalog, FactCatalog):
        raise ValueError("catalog must be a FactCatalog")
    return {
        "request_type": FACT_SELECTION_REQUEST_TYPE,
        "request_version": FACT_SELECTION_REQUEST_VERSION,
        "host_context": {
            "analysis_id": spec.analysis_id,
            "run_id": spec.run_id,
            "business_question": spec.business_question,
            "analysis_spec": spec.to_dict(),
            "fact_catalog": catalog.to_dict(),
            "response_schema": FACT_SELECTION_RESPONSE_SCHEMA,
            "system_rules": [
                "只允许选择事实目录中已落盘的事实包和字段。",
                "不得生成 QuerySpec、HighlightSpec、shop_id 或任何采集参数。",
                "不得调用 RPA，不得读取运行目录之外的文件。",
                "无法支持的问题必须通过 data_gaps 表达，不得虚构字段或事实。",
            ],
        },
        "local_audit": {
            "analysis_id": spec.analysis_id,
            "run_id": spec.run_id,
            "facts_version": catalog.schema_version,
        },
    }


def validate_fact_selection_request(request: dict) -> list[str]:
    """Validate the first handoff before it reaches the current model."""
    errors: list[str] = []
    if not isinstance(request, dict):
        return ["request must be an object"]
    if request.get("request_type") != FACT_SELECTION_REQUEST_TYPE:
        errors.append(f"request_type must be {FACT_SELECTION_REQUEST_TYPE}")
    if request.get("request_version") != FACT_SELECTION_REQUEST_VERSION:
        errors.append(f"request_version must be {FACT_SELECTION_REQUEST_VERSION}")
    if "collector_params" in request:
        errors.append("collector parameters are forbidden in fact selection request")
    host_context = request.get("host_context")
    if not isinstance(host_context, dict):
        errors.append("host_context must be an object")
        host_context = {}
    for field in ("analysis_id", "run_id", "business_question"):
        if not isinstance(host_context.get(field), str) or not host_context[field].strip():
            errors.append(f"host_context.{field} is required")
    if not isinstance(host_context.get("analysis_spec"), dict):
        errors.append("host_context.analysis_spec must be an object")
    if not isinstance(host_context.get("fact_catalog"), dict):
        errors.append("host_context.fact_catalog must be an object")
    if host_context.get("response_schema") != FACT_SELECTION_RESPONSE_SCHEMA:
        errors.append(f"host_context.response_schema must be {FACT_SELECTION_RESPONSE_SCHEMA}")
    rules = host_context.get("system_rules")
    if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule.strip() for rule in rules):
        errors.append("host_context.system_rules must be a non-empty string array")
    audit = request.get("local_audit")
    if not isinstance(audit, dict):
        errors.append("local_audit must be an object")
        audit = {}
    for field in ("analysis_id", "run_id"):
        if not isinstance(audit.get(field), str) or not audit[field].strip():
            errors.append(f"local_audit.{field} is required")
    if host_context.get("analysis_id") != audit.get("analysis_id"):
        errors.append("local_audit.analysis_id must match host_context.analysis_id")
    if host_context.get("run_id") != audit.get("run_id"):
        errors.append("local_audit.run_id must match host_context.run_id")
    return errors


def build_model_analysis_request(
    spec: AnalysisSpec,
    selected_facts: dict,
    fact_index: dict[str, object],
    standardized_intent: dict | None = None,
    capability_snapshot: dict | None = None,
) -> dict:
    """Create a host-ready, JSON-serializable model handoff request."""
    if not isinstance(spec, AnalysisSpec):
        raise ValueError("spec must be an AnalysisSpec")
    packet = build_prompt_packet(spec, selected_facts, fact_index)
    rules = list(BASE_SYSTEM_RULES)
    for rule in packet.get("rules", {}).get("quality_gates", []):
        if rule not in rules:
            rules.append(rule)
    selected_paths = sorted(packet.get("facts", {}))
    selected_refs = sorted(
        ref for ref in fact_index
        if any(ref.startswith(f"{path}#") for path in selected_paths)
    )
    intent = standardized_intent if isinstance(standardized_intent, dict) else {
        "original_question": spec.business_question,
        "research_object": "",
        "operation": "",
        "metrics": list(spec.metric_focus),
        "dimensions": [],
        "scope": spec.filters,
        "expected_output": "",
        "unsupported_requests": [],
    }
    manifest = selected_facts.get("manifest", {})
    if not isinstance(manifest, dict):
        manifest = {}
    packages = manifest.get("packages", {})
    if not isinstance(packages, dict):
        packages = {}
    package_hashes = {
        package["path"]: package.get("sha256")
        for package in packages.values()
        if isinstance(package, dict)
        and isinstance(package.get("path"), str)
        and package["path"] in selected_paths
        and isinstance(package.get("sha256"), str)
    }
    source_hashes = {
        artifact.get("path"): artifact.get("sha256")
        for artifact in manifest.get("source_artifacts", [])
        if isinstance(artifact, dict)
        and isinstance(artifact.get("path"), str)
        and isinstance(artifact.get("sha256"), str)
    }
    return {
        "request_type": REQUEST_TYPE,
        "request_version": REQUEST_VERSION,
        "host_context": {
            "analysis_id": spec.analysis_id,
            "run_id": spec.run_id,
            "analysis_spec": spec.to_dict(),
            "original_question": intent.get("original_question", spec.business_question),
            "standardized_intent": intent,
            "capability_snapshot": capability_snapshot or {},
            "facts": packet.get("facts", {}),
            "selected_records": packet.get("facts", {}),
            "local_aggregates": selected_facts.get("aggregates", {}),
            "allowed_fact_refs": selected_refs,
            "data_gaps": selected_facts.get("data_gaps", []),
            "fact_refs": selected_refs,
            "system_rules": rules,
            "response_schema": RESPONSE_SCHEMA,
            "report_presentation_guidance": REPORT_PRESENTATION_GUIDANCE,
        },
        "local_audit": {
            "analysis_id": spec.analysis_id,
            "run_id": spec.run_id,
            "facts_version": manifest.get("schema_version"),
            "prompt_version": packet.get("prompt_version"),
            "fact_package_hashes": package_hashes,
            "source_artifact_hashes": source_hashes,
            "selected_fact_paths": selected_paths,
        },
    }


def validate_model_analysis_request(request: dict) -> list[str]:
    """Return protocol errors without making network or model calls."""
    errors: list[str] = []
    if not isinstance(request, dict):
        return ["request must be an object"]
    if request.get("request_type") != REQUEST_TYPE:
        errors.append(f"request_type must be {REQUEST_TYPE}")
    version = request.get("request_version")
    if isinstance(version, bool) or version != REQUEST_VERSION:
        errors.append(f"request_version must be {REQUEST_VERSION}")
    host_context = request.get("host_context")
    if not isinstance(host_context, dict):
        errors.append("host_context must be an object")
        host_context = {}
    audit = request.get("local_audit")
    if not isinstance(audit, dict):
        errors.append("local_audit must be an object")
        audit = {}
    for field in ("analysis_id", "run_id"):
        value = host_context.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"host_context.{field} is required")
    rules = host_context.get("system_rules")
    if not isinstance(rules, list) or not rules or any(
        not isinstance(rule, str) or not rule.strip() for rule in rules
    ):
        errors.append("host_context.system_rules must be a non-empty string array")
    if not isinstance(host_context.get("analysis_spec"), dict):
        errors.append("host_context.analysis_spec must be an object")
    facts = host_context.get("facts")
    if not isinstance(facts, dict):
        errors.append("host_context.facts must be an object")
    refs = host_context.get("fact_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        errors.append("host_context.fact_refs must be a string array")
        refs = []
    allowed_refs = host_context.get("allowed_fact_refs", refs)
    if not isinstance(allowed_refs, list) or any(not isinstance(ref, str) for ref in allowed_refs):
        errors.append("host_context.allowed_fact_refs must be a string array")
        allowed_refs = []
    if set(refs) - set(allowed_refs):
        errors.append("host_context.fact_refs must be a subset of allowed_fact_refs")
    if isinstance(facts, dict):
        fact_paths = set(facts)
        for path, records in facts.items():
            if not isinstance(path, str) or not isinstance(records, list):
                errors.append("host_context.facts must map package paths to record arrays")
        for ref in refs:
            if "#" not in ref:
                errors.append("host_context.fact_refs must be fact references")
                continue
            if ref.split("#", 1)[0] not in fact_paths:
                errors.append(f"host_context.fact_refs reference is outside selected facts: {ref}")
    try:
        from .fact_reference import FactReferenceError, parse_fact_ref
    except ImportError:  # pragma: no cover
        from fact_reference import FactReferenceError, parse_fact_ref
    for position, ref in enumerate(allowed_refs):
        try:
            parse_fact_ref(ref)
        except FactReferenceError as exc:
            errors.append(f"host_context.allowed_fact_refs[{position}] {exc.message}")
    if host_context.get("response_schema") != RESPONSE_SCHEMA:
        errors.append(f"host_context.response_schema must be {RESPONSE_SCHEMA}")
    for field in ("analysis_id", "run_id", "facts_version", "prompt_version"):
        value = audit.get(field)
        if field in {"analysis_id", "run_id", "prompt_version"} and (not isinstance(value, str) or not value.strip()):
            errors.append(f"local_audit.{field} is required")
        if field == "facts_version" and (isinstance(value, bool) or not isinstance(value, (int, str))):
            errors.append("local_audit.facts_version is required")
    if not isinstance(audit.get("fact_package_hashes"), dict):
        errors.append("local_audit.fact_package_hashes must be an object")
    if not isinstance(audit.get("source_artifact_hashes"), dict):
        errors.append("local_audit.source_artifact_hashes must be an object")
    if not isinstance(audit.get("selected_fact_paths"), list):
        errors.append("local_audit.selected_fact_paths must be an array")
    if host_context.get("analysis_id") != audit.get("analysis_id"):
        errors.append("local_audit.analysis_id must match host_context.analysis_id")
    if host_context.get("run_id") != audit.get("run_id"):
        errors.append("local_audit.run_id must match host_context.run_id")
    return errors


__all__ = [
    "FACT_SELECTION_REQUEST_TYPE",
    "FACT_SELECTION_REQUEST_VERSION",
    "FACT_SELECTION_RESPONSE_SCHEMA",
    "REQUEST_TYPE",
    "REQUEST_VERSION",
    "HANDOFF_PROTOCOL",
    "RESPONSE_SCHEMA",
    "build_model_analysis_request",
    "build_fact_selection_request",
    "validate_fact_selection_request",
    "validate_model_analysis_request",
]
