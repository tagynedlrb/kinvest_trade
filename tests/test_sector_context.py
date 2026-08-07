from kinvest_trade.sector_context import (
    build_domestic_sector_context,
    build_overseas_sector_context,
)


def test_domestic_sector_context_records_identity_without_breadth_claim() -> None:
    context = build_domestic_sector_context("전기전자")

    assert context["available"] is True
    assert context["evaluable"] is False
    assert context["sector_name"] == "전기전자"
    assert context["supportive_for_long"] is None


def test_overseas_sector_context_measures_selected_pool_alignment() -> None:
    context = build_overseas_sector_context(
        "AAA",
        [
            {
                "symbol": "AAA",
                "sector_name": "Technology Services",
                "industry_name": "Packaged Software",
                "scanner_change_pct": 3.0,
            },
            {
                "symbol": "BBB",
                "sector_name": "Technology Services",
                "industry_name": "Internet Software/Services",
                "scanner_change_pct": 1.0,
            },
            {
                "symbol": "CCC",
                "sector_name": "Finance",
                "scanner_change_pct": -2.0,
            },
        ],
    )

    assert context["available"] is True
    assert context["evaluable"] is True
    assert context["cohort_count"] == 2
    assert context["sector_average_change_pct"] == 2.0
    assert context["sector_positive_rate"] == 1.0
    assert context["target_minus_sector_average_pct"] == 1.0
    assert context["supportive_for_long"] is True
    assert (
        context["limitation"]
        == "selected_relative_volume_pool_not_broad_sector_benchmark"
    )


def test_overseas_sector_context_does_not_score_single_member_cohort() -> None:
    context = build_overseas_sector_context(
        "AAA",
        [
            {
                "symbol": "AAA",
                "sector_name": "Energy Minerals",
                "scanner_change_pct": 5.0,
            }
        ],
    )

    assert context["available"] is True
    assert context["evaluable"] is False
    assert context["supportive_for_long"] is None
    assert context["reason"] == "sector_cohort_too_small"
