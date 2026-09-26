"""Structured interpretation of the user's phase-two business question."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

try:
    from .fact_selection import FactPackage
except ImportError:  # pragma: no cover - direct script imports
    from fact_selection import FactPackage


class AnalysisObjective(str, Enum):
    COMPARE_BRAND = "compare_brand"
    FIND_HIGH_EXPOSURE_LOW_CONVERSION = "find_high_exposure_low_conversion"
    FIND_HIGH_EFFICIENCY_LOW_EXPOSURE = "find_high_efficiency_low_exposure"
    ANALYZE_AUDIENCE = "analyze_audience"
    ANALYZE_SCRIPT = "analyze_script"
    ANALYZE_HIGHLIGHT = "analyze_highlight"
    ANALYZE_CREATIVE_DIRECTION = "analyze_creative_direction"


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    result = tuple(item.strip() if isinstance(item, str) else item for item in value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field_name} must contain nonblank strings")
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _enum_tuple(value: Any, enum_type: type[Enum], label: str) -> tuple[Enum, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    result = []
    for item in value:
        try:
            result.append(item if isinstance(item, enum_type) else enum_type(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported {label}: {item}") from exc
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(result)


@dataclass(frozen=True)
class AnalysisIntent:
    business_question: str
    objectives: tuple[AnalysisObjective, ...]
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    required_packages: tuple[FactPackage, ...]
    material_scope: dict[str, Any] | None
    creative_analysis_required: bool
    original_question: str | None = None
    research_object: str = ""
    operation: str = ""
    scope: dict[str, Any] | None = None
    expected_output: str = ""
    unsupported_requests: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.business_question, str) or not self.business_question.strip():
            raise ValueError("business_question must be a nonblank string")
        object.__setattr__(self, "business_question", self.business_question.strip())
        original_question = self.original_question if self.original_question is not None else self.business_question
        if not isinstance(original_question, str) or not original_question.strip():
            raise ValueError("original_question must be a nonblank string")
        object.__setattr__(self, "original_question", original_question.strip())
        object.__setattr__(self, "objectives", _enum_tuple(self.objectives, AnalysisObjective, "objective"))
        object.__setattr__(self, "metrics", _strings(self.metrics, "metrics"))
        object.__setattr__(self, "dimensions", _strings(self.dimensions, "dimensions"))
        object.__setattr__(self, "required_packages", _enum_tuple(self.required_packages, FactPackage, "fact package"))
        if not isinstance(self.creative_analysis_required, bool):
            raise ValueError("creative_analysis_required must be a boolean")
        if self.material_scope is not None and not isinstance(self.material_scope, dict):
            raise ValueError("material_scope must be an object or null")
        for name in ("research_object", "operation", "expected_output"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            object.__setattr__(self, name, value.strip())
        if self.scope is not None and not isinstance(self.scope, dict):
            raise ValueError("scope must be an object or null")
        normalized_unsupported = []
        for item in self.unsupported_requests:
            if not isinstance(item, dict):
                raise ValueError("unsupported_requests must contain objects")
            request = item.get("request")
            reason = item.get("reason")
            if not isinstance(request, str) or not request.strip() or not isinstance(reason, str) or not reason.strip():
                raise ValueError("unsupported_requests items require request and reason")
            normalized_unsupported.append({"request": request.strip(), "reason": reason.strip()})
        object.__setattr__(self, "unsupported_requests", tuple(normalized_unsupported))

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_question": self.business_question,
            "original_question": self.original_question,
            "research_object": self.research_object,
            "operation": self.operation,
            "objectives": [item.value for item in self.objectives],
            "metrics": list(self.metrics),
            "dimensions": list(self.dimensions),
            "required_packages": [item.value for item in self.required_packages],
            "material_scope": self.material_scope,
            "scope": self.scope,
            "expected_output": self.expected_output,
            "unsupported_requests": [dict(item) for item in self.unsupported_requests],
            "creative_analysis_required": self.creative_analysis_required,
        }


def parse_analysis_intent(payload: dict[str, Any]) -> AnalysisIntent:
    if not isinstance(payload, dict):
        raise ValueError("analysis intent must be an object")
    return AnalysisIntent(
        business_question=payload.get("business_question"),
        original_question=payload.get("original_question", payload.get("business_question")),
        research_object=payload.get("research_object", ""),
        operation=payload.get("operation", ""),
        objectives=tuple(payload.get("objectives", ())),
        metrics=tuple(payload.get("metrics", ())),
        dimensions=tuple(payload.get("dimensions", ())),
        required_packages=tuple(payload.get("required_packages", ())),
        material_scope=payload.get("material_scope"),
        scope=payload.get("scope", payload.get("material_scope")),
        expected_output=payload.get("expected_output", ""),
        unsupported_requests=tuple(payload.get("unsupported_requests", ())),
        creative_analysis_required=payload.get("creative_analysis_required", False),
    )


__all__ = [
    "AnalysisIntent",
    "AnalysisObjective",
    "FactPackage",
    "parse_analysis_intent",
]
