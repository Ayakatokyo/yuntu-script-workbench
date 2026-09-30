"""Validation and normalization for model-produced commercial analysis results."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

try:
    from .fact_reference import FactReferenceError, parse_fact_ref
except ImportError:  # pragma: no cover
    from fact_reference import FactReferenceError, parse_fact_ref


ALLOWED_TYPES = {"fact", "inference", "recommendation"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_SECTIONS = {"material_ranking", "audience", "script", "highlight_frames", "content_tags", "interaction_series"}
CAUSAL_MARKERS = ("导致", "因为", "原因是", "驱动", "造成", "促成", "带来", "说明了")
BRAND_PROXY_MARKERS = ("标题识别品牌", "标签识别品牌", "品牌识别", "品牌归因", "推断品牌")
BRAND_SEMANTIC_MARKERS = ("品牌", "归因", "品牌表现", "品牌素材")
INFERENCE_MARKERS = ("推断", "推测", "可能", "或许", "倾向", "更喜欢", "偏好", "似乎", "意味着")
ASSOCIATION_MARKERS = ("关联", "相关", "共现", "交叉")


class StructuredValidationError(dict):
    """Dictionary error that keeps legacy CLI/string containment behavior."""

    def __init__(self, code: str, path: str, message: str, suggestion: str):
        super().__init__(code=code, path=path, message=message, suggestion=suggestion)

    def __str__(self) -> str:
        return self["message"]

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str) and item not in self.keys():
            return any(item in str(value) for value in self.values())
        return super().__contains__(item)


def _error(code: str, path: str, message: str, suggestion: str) -> StructuredValidationError:
    return StructuredValidationError(code, path, message, suggestion)


def format_validation_errors(errors: list[StructuredValidationError]) -> str:
    return "; ".join(str(error) for error in errors)


class AnalysisResultValidationError(ValueError):
    def __init__(self, errors: list[StructuredValidationError]):
        self.errors = errors
        super().__init__("AnalysisResult 校验失败: " + format_validation_errors(errors))


def normalize_analysis_result(result: dict) -> dict:
    if not isinstance(result, dict):
        raise ValueError("analysis result must be an object")
    normalized = deepcopy(result)
    normalized.setdefault("findings", [])
    normalized.setdefault("gaps", [])
    normalized.setdefault("next_questions", [])
    return normalized


def _has_source_empty(value: Any, *, ignore_script_fields: bool = False) -> bool:
    if isinstance(value, dict):
        availability = value.get("data_availability")
        if isinstance(availability, dict):
            if (
                availability.get("available") is False
                and availability.get("missing_reason") == "source_empty"
            ):
                return True
            for field_name, state in availability.items():
                if ignore_script_fields and field_name in {
                    "script_text", "content_formula", "script_segments", "selection_group"
                }:
                    continue
                if isinstance(state, dict) and (
                    state.get("missing_reason") == "source_empty"
                    or state.get("available") is False and state.get("missing_reason") == "source_empty"
                ):
                    return True
        return any(
            _has_source_empty(item, ignore_script_fields=ignore_script_fields)
            for key, item in value.items()
            if key != "data_availability"
        )
    if isinstance(value, list):
        return any(
            _has_source_empty(item, ignore_script_fields=ignore_script_fields)
            for item in value
        )
    return False


def _is_brand_proxy(claim: str) -> bool:
    return any(marker in claim for marker in BRAND_PROXY_MARKERS)


def _field_value(record: Any, field: str) -> Any:
    value = record
    for part in field.split("."):
        if not part or not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _field_available(record: dict, field: str) -> bool:
    value = _field_value(record, field)
    if value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}:
        return False
    availability = record.get("data_availability", {})
    if not isinstance(availability, dict):
        return False
    if availability.get("available") is False:
        return False
    for key, state in availability.items():
        if (field == key or field.startswith(key + ".")) and isinstance(state, dict):
            if state.get("available") is False or state.get("missing_reason") == "source_empty":
                return False
    return not _has_source_empty(value)


def _validate_field_evidence(finding: dict, refs: list, fact_index: dict, prefix: str) -> list:
    """Explicit field scope avoids unrelated missing fields poisoning a record."""
    errors = []
    fields = finding.get("field_refs")
    if not isinstance(fields, dict) or set(fields) != set(refs):
        return [_error("field_refs_invalid", prefix + ".field_refs", "field_refs must map every fact_ref to its used fields", "为每个 fact_ref 提供非空字段数组，例如 [script_text]。")]
    for ref, names in fields.items():
        record = fact_index.get(ref)
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
            errors.append(_error("field_refs_invalid", prefix, "field_refs requires non-empty field names", "填写实际使用的字段路径。"))
            continue
        if not isinstance(record, dict):
            continue  # The record gate reports out-of-bounds refs.
        # A script record with no readable text remains a gap, even if an ID exists.
        if "facts/script.json#" in ref and not _field_available(record, "script_text"):
            errors.append(_error("source_empty", prefix, "script_text source_empty may only enter gaps", "无可用正文的素材仅进入 gaps。"))
        for name in names:
            if name.startswith("data_availability") or not _field_available(record, name):
                errors.append(_error("field_unavailable", prefix, f"unavailable evidence field: {ref}/{name}", "只引用当前所选事实中可用的字段。"))
    quotes = finding.get("quotes", [])
    if not isinstance(quotes, list):
        return errors + [_error("quotes_invalid", prefix, "quotes must be an array", "原话引用提供 fact_ref、field、text。")]
    for quote in quotes:
        if not isinstance(quote, dict):
            errors.append(_error("quote_invalid", prefix, "quote must be an object", "提供 fact_ref、field、text。"))
            continue
        ref, field, text = quote.get("fact_ref"), quote.get("field"), quote.get("text")
        if (not isinstance(ref, str) or not isinstance(field, str)
                or not isinstance(fields.get(ref), list) or field not in fields[ref]):
            errors.append(_error("quote_reference_invalid", prefix, "quote is outside declared field_refs", "引文必须绑定本条结论的字段引用。"))
            continue
        value = _field_value(fact_index.get(ref), field)
        if not isinstance(value, str) or not isinstance(text, str) or not text.strip() or text not in value:
            errors.append(_error("quote_mismatch", prefix, "quote is not an exact substring of its source field", "保留来源正文的逐字原话。"))
    return errors


def _record_has_empty_brand(record: Any) -> bool:
    return bool(_brand_proxy_info(record))


def _brand_proxy_info(record: Any) -> dict | None:
    """Return persisted proxy evidence, including propagated fact snapshots."""
    if not isinstance(record, dict):
        return None
    selection = record.get("selection_group")
    if isinstance(selection, dict) and str(selection.get("brand_name") or "").strip():
        return None
    availability = record.get("data_availability")
    state = availability.get("selection_group") if isinstance(availability, dict) else None
    classification = record.get("brand_classification")
    if not isinstance(classification, dict):
        return None
    if classification.get("source") != "runtime_title_rule":
        return None
    if isinstance(state, dict) and state.get("available") is not False:
        return None
    matched = classification.get("matchedBrands", [])
    if not isinstance(matched, list):
        matched = []
    return {
        "source": "runtime_title_rule",
        "matched_brands": [str(item) for item in matched if str(item).strip()],
    }


def _finding_brand_proxy(finding: dict, referenced_records: list[Any], claim: str) -> dict | None:
    proxy_records = [info for info in (_brand_proxy_info(record) for record in referenced_records) if info]
    if not proxy_records:
        return {"source": "claim_marker", "matched_brands": []} if _is_brand_proxy(claim) else None
    matched_brands = {
        brand
        for info in proxy_records
        for brand in info["matched_brands"]
    }
    brand_is_named = any(brand and brand in claim for brand in matched_brands)
    brand_semantics = any(marker in claim for marker in BRAND_SEMANTIC_MARKERS)
    if brand_is_named or brand_semantics or _is_brand_proxy(claim):
        return {
            "source": "runtime_title_rule",
            "matched_brands": sorted(matched_brands),
        }
    return None


def _brand_proxy_explanation(finding: dict) -> str:
    explanation = finding.get("brand_proxy_explanation")
    if isinstance(explanation, str) and explanation.strip():
        return explanation.strip()
    proxy = finding.get("brand_proxy")
    if isinstance(proxy, dict):
        explanation = proxy.get("explanation")
        if isinstance(explanation, str) and explanation.strip():
            return explanation.strip()
    return ""


def validate_analysis_result(
    result: dict, fact_index: dict, allowed_sections: set[str] | None = None
) -> list[StructuredValidationError]:
    """Return gate failures; a non-empty list must prevent report rendering."""
    errors: list[StructuredValidationError] = []
    try:
        normalized = normalize_analysis_result(result)
    except ValueError as exc:
        return [_error("result_not_object", "$", str(exc), "返回 JSON 对象。")]
    section = normalized.get("section")
    if not isinstance(section, str) or not section.strip():
        errors.append(_error("section_required", "$.section", "section is required", "填写一个已选择的分析模块。"))
    elif section not in (allowed_sections or ALLOWED_SECTIONS):
        errors.append(_error("section_invalid", "$.section", f"section is not an allowed analysis module: {section}", "仅使用 material_ranking、audience、script 或 highlight_frames。"))
    findings = normalized.get("findings")
    if not isinstance(findings, list):
        return errors + [_error("findings_type", "$.findings", "findings must be an array", "返回 findings 数组。")]
    if not isinstance(fact_index, dict):
        return errors + [_error("fact_index_type", "$.fact_index", "fact_index must be an object", "使用本地事实索引校验 fact_refs。")]
    for position, finding in enumerate(findings):
        prefix = f"$.findings[{position}]"
        if not isinstance(finding, dict):
            errors.append(_error("finding_type", prefix, f"{prefix} must be an object", "每条 finding 返回对象。"))
            continue
        claim = finding.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            errors.append(_error("claim_required", f"{prefix}.claim", f"{prefix}.claim is required", "填写可由 fact_refs 支撑的结论文本。"))
        finding_type = finding.get("type")
        if finding_type not in ALLOWED_TYPES:
            errors.append(_error("finding_type_invalid", f"{prefix}.type", f"{prefix}.type must be one of: {', '.join(sorted(ALLOWED_TYPES))}", "使用 fact、inference 或 recommendation。"))
        confidence = finding.get("confidence")
        if confidence not in ALLOWED_CONFIDENCE:
            errors.append(_error("confidence_invalid", f"{prefix}.confidence", f"{prefix}.confidence must be one of: {', '.join(sorted(ALLOWED_CONFIDENCE))}", "使用 high、medium 或 low。"))
        refs = finding.get("fact_refs")
        if not isinstance(refs, list) or not refs:
            errors.append(_error("fact_refs_required", f"{prefix}.fact_refs", f"{prefix}.fact_refs must contain at least one reference", "至少引用一条已选择事实记录。"))
            continue
        if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            errors.append(_error("fact_ref_type", f"{prefix}.fact_refs", f"{prefix}.fact_refs must contain strings", "每个 fact_ref 使用事实包路径和 records 下标。"))
            continue
        referenced_records = []
        for ref_position, ref in enumerate(refs):
            ref_path = f"{prefix}.fact_refs[{ref_position}]"
            try:
                parse_fact_ref(ref)
            except FactReferenceError as exc:
                errors.append(_error(exc.code, ref_path, exc.message, exc.suggestion))
                continue
            if ref not in fact_index:
                errors.append(_error("fact_ref_out_of_bounds", ref_path, f"fact_ref does not resolve: {ref}", "只引用本次选择且存在于 fact_index 的记录。"))
            else:
                referenced_records.append(fact_index[ref])
        if "field_refs" in finding:
            errors.extend(_validate_field_evidence(finding, refs, fact_index, prefix))
        elif any(
            _has_source_empty(
                fact_index[ref],
                ignore_script_fields="highlight-frames.json" in ref,
            )
            for ref in refs
            if ref in fact_index
        ):
            errors.append(_error("source_empty", prefix, f"{prefix} references source_empty data; source_empty may only enter gaps", "移入 gaps 或改为引用可用字段。"))
        if "quotes" in finding and "field_refs" not in finding:
            errors.append(_error("field_refs_required", prefix, "quotes require field_refs", "为原话声明实际引用字段。"))
        claim_text = claim if isinstance(claim, str) else ""
        if any(marker in claim_text for marker in CAUSAL_MARKERS):
            errors.append(_error("causal_language", f"{prefix}.claim", f"{prefix} 包含不受支持的因果表述；相关不等于因果", "改为描述观察或关联，并标记为 inference。"))
        if any(marker in claim_text for marker in ASSOCIATION_MARKERS) and finding_type != "inference":
            errors.append(_error("association_requires_inference", f"{prefix}.type", "标签关联必须标记为 inference", "将 type 改为 inference。"))
        proxy_info = _finding_brand_proxy(finding, referenced_records, claim_text)
        if proxy_info:
            if finding_type != "inference":
                errors.append(_error("brand_proxy_requires_inference", f"{prefix}.type", "品牌代理识别必须标记为 inference", "将 type 改为 inference，并说明品牌来自运行时标题规则。"))
            if confidence == "high":
                errors.append(_error("brand_proxy_high_confidence", f"{prefix}.confidence", "品牌代理识别不能使用 high confidence", "将 confidence 降为 medium 或 low。"))
            if not _brand_proxy_explanation(finding):
                errors.append(_error("brand_proxy_explanation_required", f"{prefix}.brand_proxy_explanation", "品牌代理 finding 必须提供品牌代理说明", "说明 selection_group.brand_name 为空，品牌名来自 brand_classification.source=runtime_title_rule。"))
        if finding_type == "fact" and any(marker in claim_text for marker in INFERENCE_MARKERS):
            errors.append(_error("finding_type_semantics", f"{prefix}.type", "fact finding 包含推断性措辞", "将 type 改为 inference，或改写为可直接由记录证明的事实。"))
        if section == "highlight_frames" and any(
            marker in claim_text for marker in ("高光", "画面", "节奏", "流失")
        ) and finding_type != "inference" and not any(
            marker in claim_text for marker in ("事实", "记录", "第", "秒")
        ):
            errors.append(_error("highlight_interpretation_requires_inference", f"{prefix}.type", f"{prefix} 高光解释必须标记为 inference", "将画面/节奏解释标记为 inference，并保留事实引用。"))
    for field_name in ("gaps", "next_questions"):
        if not isinstance(normalized.get(field_name), list):
            errors.append(_error("result_field_type", f"$.{field_name}", f"{field_name} must be an array", f"返回 {field_name} 数组。"))
    return errors


__all__ = [
    "ALLOWED_CONFIDENCE",
    "ALLOWED_SECTIONS",
    "ALLOWED_TYPES",
    "AnalysisResultValidationError",
    "StructuredValidationError",
    "format_validation_errors",
    "normalize_analysis_result",
    "validate_analysis_result",
]
