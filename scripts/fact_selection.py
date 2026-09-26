"""Closed data structures for selecting already-persisted fact records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FactPackage(str, Enum):
    MATERIAL_RANKING = "material_ranking"
    AUDIENCE = "audience"
    SCRIPT = "script"
    CONTENT_TAGS = "content_tags"
    INTERACTION_SERIES = "interaction_series"
    HIGHLIGHT_FRAMES = "highlight_frames"


@dataclass(frozen=True)
class FactSelectionResult:
    sections: dict[str, dict[str, Any]]
    selection_summary: dict[str, Any]
    fact_refs: tuple[str, ...]
    data_gaps: tuple[dict[str, Any], ...]
    validation_errors: tuple[str, ...] = ()
    aggregates: dict[str, Any] | None = None


class SelectionOperator(str, Enum):
    EQUALS = "equals"
    IN = "in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    TOP_N = "top_n"
    BOTTOM_N = "bottom_n"
    TOP_PERCENTILE = "top_percentile"
    BOTTOM_PERCENTILE = "bottom_percentile"
    CONTAINS = "contains"


class AggregationOperation(str, Enum):
    COUNT = "count"
    SUM = "sum"
    MEAN = "mean"
    MEDIAN = "median"
    PERCENTILE = "percentile"
    DISTRIBUTION = "distribution"


_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _enum(value: Any, enum_type: type[Enum], label: str):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported {label}: {value}") from exc


def _field(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not _SAFE_FIELD.fullmatch(value.strip()):
        raise ValueError("field must be a safe dotted identifier")
    return value.strip()


def _selector_value(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.casefold()
        if any(token in lowered for token in ("__import__", "eval(", "exec(", "open(")):
            raise ValueError("selector value must not contain executable expressions")
        if "/" in value or "\\" in value:
            raise ValueError("selector value must not contain filesystem paths")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        return [_selector_value(item) for item in value]
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("selector value must be a scalar, array, or null")


@dataclass(frozen=True)
class FactFilter:
    package: FactPackage
    field: str
    operator: SelectionOperator
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "package", _enum(self.package, FactPackage, "fact package"))
        object.__setattr__(self, "field", _field(self.field))
        object.__setattr__(self, "operator", _enum(self.operator, SelectionOperator, "selection operator"))
        object.__setattr__(self, "value", _selector_value(self.value))

        if self.operator in {SelectionOperator.TOP_PERCENTILE, SelectionOperator.BOTTOM_PERCENTILE}:
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool) or not 0 <= self.value <= 100:
                raise ValueError("percentile selector value must be between 0 and 100")
        if self.operator in {SelectionOperator.TOP_N, SelectionOperator.BOTTOM_N}:
            if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
                raise ValueError("top/bottom N selector value must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package.value,
            "field": self.field,
            "operator": self.operator.value,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactFilter":
        if not isinstance(payload, dict):
            raise ValueError("fact filter must be an object")
        return cls(
            package=payload.get("package"),
            field=payload.get("field"),
            operator=payload.get("operator"),
            value=payload.get("value"),
        )


@dataclass(frozen=True)
class SortRule:
    field: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", _field(self.field))
        if self.direction not in {"asc", "desc"}:
            raise ValueError("sort direction must be asc or desc")

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "direction": self.direction}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SortRule":
        if not isinstance(payload, dict):
            raise ValueError("sort rule must be an object")
        return cls(field=payload.get("field"), direction=payload.get("direction", "asc"))


@dataclass(frozen=True)
class FactJoin:
    package: FactPackage
    on: str = "material_id"

    def __post_init__(self) -> None:
        object.__setattr__(self, "package", _enum(self.package, FactPackage, "fact package"))
        object.__setattr__(self, "on", _field(self.on))

    def to_dict(self) -> dict[str, str]:
        return {"package": self.package.value, "on": self.on}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactJoin":
        if not isinstance(payload, dict):
            raise ValueError("fact join must be an object")
        return cls(package=payload.get("package"), on=payload.get("on", "material_id"))


@dataclass(frozen=True)
class FactAggregation:
    package: FactPackage
    field: str
    operation: AggregationOperation | str
    percentile: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "package", _enum(self.package, FactPackage, "fact package"))
        object.__setattr__(self, "field", _field(self.field))
        object.__setattr__(self, "operation", _enum(self.operation, AggregationOperation, "aggregation operation"))
        if self.operation == AggregationOperation.PERCENTILE:
            if not isinstance(self.percentile, (int, float)) or isinstance(self.percentile, bool) or not 0 <= self.percentile <= 100:
                raise ValueError("percentile must be between 0 and 100")
        elif self.percentile is not None:
            raise ValueError("percentile is only valid for percentile aggregation")

    def to_dict(self) -> dict[str, Any]:
        payload = {"package": self.package.value, "field": self.field, "operation": self.operation.value}
        if self.percentile is not None:
            payload["percentile"] = self.percentile
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactAggregation":
        if not isinstance(payload, dict):
            raise ValueError("fact aggregation must be an object")
        return cls(
            package=payload.get("package"),
            field=payload.get("field"),
            operation=payload.get("operation"),
            percentile=payload.get("percentile"),
        )


@dataclass(frozen=True)
class FactSelectionSpec:
    packages: tuple[FactPackage, ...]
    filters: tuple[FactFilter, ...]
    fields: dict[FactPackage, tuple[str, ...]]
    order_by: tuple[SortRule, ...]
    limit: int | None
    joins: tuple[FactJoin, ...]
    aggregations: tuple[FactAggregation, ...] = ()

    def __post_init__(self) -> None:
        packages = tuple(_enum(item, FactPackage, "fact package") for item in self.packages)
        if not packages:
            raise ValueError("packages must not be empty")
        if len(packages) != len(set(packages)):
            raise ValueError("packages must not contain duplicates")
        object.__setattr__(self, "packages", packages)

        filters = tuple(item if isinstance(item, FactFilter) else FactFilter.from_dict(item) for item in self.filters)
        order_by = tuple(item if isinstance(item, SortRule) else SortRule.from_dict(item) for item in self.order_by)
        joins = tuple(item if isinstance(item, FactJoin) else FactJoin.from_dict(item) for item in self.joins)
        aggregations = tuple(item if isinstance(item, FactAggregation) else FactAggregation.from_dict(item) for item in self.aggregations)
        object.__setattr__(self, "filters", filters)
        object.__setattr__(self, "order_by", order_by)
        object.__setattr__(self, "joins", joins)
        object.__setattr__(self, "aggregations", aggregations)
        if any(item.package not in packages for item in aggregations):
            raise ValueError("aggregations contains packages not selected")

        normalized_fields: dict[FactPackage, tuple[str, ...]] = {}
        for package, raw_fields in self.fields.items():
            package_enum = _enum(package, FactPackage, "fact package")
            if not isinstance(raw_fields, (list, tuple)):
                raise ValueError("fields must map packages to arrays")
            field_names = tuple(_field(item) for item in raw_fields)
            if len(field_names) != len(set(field_names)):
                raise ValueError("fields must not contain duplicates")
            normalized_fields[package_enum] = field_names
        unknown_field_packages = set(normalized_fields) - set(packages)
        if unknown_field_packages:
            raise ValueError("fields contains packages not selected")
        object.__setattr__(self, "fields", normalized_fields)

        if self.limit is not None and (not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit <= 0):
            raise ValueError("limit must be a positive integer or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [item.value for item in self.packages],
            "filters": [item.to_dict() for item in self.filters],
            "fields": {package.value: list(names) for package, names in self.fields.items()},
            "order_by": [item.to_dict() for item in self.order_by],
            "limit": self.limit,
            "joins": [item.to_dict() for item in self.joins],
            "aggregations": [item.to_dict() for item in self.aggregations],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactSelectionSpec":
        if not isinstance(payload, dict):
            raise ValueError("fact selection spec must be an object")
        return cls(
            packages=tuple(payload.get("packages", ())),
            filters=tuple(FactFilter.from_dict(item) for item in payload.get("filters", ())),
            fields=payload.get("fields", {}),
            order_by=tuple(SortRule.from_dict(item) for item in payload.get("order_by", ())),
            limit=payload.get("limit"),
            joins=tuple(FactJoin.from_dict(item) for item in payload.get("joins", ())),
            aggregations=tuple(FactAggregation.from_dict(item) for item in payload.get("aggregations", ())),
        )


__all__ = [
    "AggregationOperation",
    "FactAggregation",
    "FactFilter",
    "FactJoin",
    "FactPackage",
    "FactSelectionResult",
    "FactSelectionSpec",
    "SelectionOperator",
    "SortRule",
]
