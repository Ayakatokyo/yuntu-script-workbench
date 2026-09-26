import pytest

from scripts.fact_selection import (
    FactFilter,
    FactJoin,
    FactPackage,
    FactSelectionSpec,
    SelectionOperator,
    SortRule,
)


def test_fact_selection_spec_round_trips_without_collector_parameters():
    spec = FactSelectionSpec(
        packages=(FactPackage.MATERIAL_RANKING, FactPackage.SCRIPT),
        filters=(
            FactFilter(
                package=FactPackage.MATERIAL_RANKING,
                field="metrics.show_cnt",
                operator=SelectionOperator.TOP_PERCENTILE,
                value=75,
            ),
        ),
        fields={FactPackage.MATERIAL_RANKING: ("material_id", "metrics")},
        order_by=(SortRule("rank", "asc"),),
        limit=30,
        joins=(FactJoin(FactPackage.SCRIPT, "material_id"),),
    )

    payload = spec.to_dict()

    assert FactSelectionSpec.from_dict(payload) == spec
    assert "shop_id" not in repr(payload)
    assert "custom_start_date" not in repr(payload)


def test_fact_selection_spec_rejects_unsupported_package_and_duplicate_fields():
    with pytest.raises(ValueError, match="unsupported fact package"):
        FactSelectionSpec(
            packages=("../../secrets",),
            filters=(),
            fields={},
            order_by=(),
            limit=None,
            joins=(),
        )

    with pytest.raises(ValueError, match="fields must not contain duplicates"):
        FactSelectionSpec(
            packages=(FactPackage.MATERIAL_RANKING,),
            filters=(),
            fields={FactPackage.MATERIAL_RANKING: ("metrics", "metrics")},
            order_by=(),
            limit=None,
            joins=(),
        )


def test_fact_filter_rejects_executable_or_path_like_values():
    with pytest.raises(ValueError, match="selector value"):
        FactFilter(
            package=FactPackage.MATERIAL_RANKING,
            field="metrics.ctr",
            operator=SelectionOperator.GTE,
            value="__import__('os').system('id')",
        )

    with pytest.raises(ValueError, match="field"):
        FactFilter(
            package=FactPackage.MATERIAL_RANKING,
            field="../../run-summary.json",
            operator=SelectionOperator.EQUALS,
            value="x",
        )
