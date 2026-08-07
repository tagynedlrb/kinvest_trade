from __future__ import annotations

from statistics import median
from typing import Iterable


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_domestic_sector_context(sector_name: str) -> dict[str, object]:
    sector = str(sector_name or "").strip()
    return {
        "available": bool(sector),
        "evaluable": False,
        "market": "domestic",
        "source": "kis_current_price_identity",
        "sector_name": sector,
        "supportive_for_long": None,
        "reason": "sector_breadth_source_unavailable" if sector else "sector_identity_missing",
    }


def build_overseas_sector_context(
    symbol: str,
    pool_rows: Iterable[dict[str, object]],
) -> dict[str, object]:
    symbol_key = str(symbol or "").strip().upper()
    rows = [row for row in pool_rows if isinstance(row, dict)]
    target = next(
        (
            row
            for row in rows
            if str(row.get("symbol") or "").strip().upper() == symbol_key
        ),
        None,
    )
    if target is None:
        return {
            "available": False,
            "evaluable": False,
            "market": "overseas",
            "source": "tradingview_top_relative_volume_pool",
            "sector_name": "",
            "industry_name": "",
            "supportive_for_long": None,
            "reason": "symbol_not_in_sector_pool",
        }

    sector = str(target.get("sector_name") or "").strip()
    industry = str(target.get("industry_name") or "").strip()
    if not sector:
        return {
            "available": False,
            "evaluable": False,
            "market": "overseas",
            "source": "tradingview_top_relative_volume_pool",
            "sector_name": "",
            "industry_name": industry,
            "supportive_for_long": None,
            "reason": "sector_identity_missing",
        }

    changes = [
        value
        for row in rows
        if str(row.get("sector_name") or "").strip() == sector
        if (value := _optional_float(row.get("scanner_change_pct"))) is not None
    ]
    target_change = _optional_float(target.get("scanner_change_pct"))
    cohort_count = len(changes)
    evaluable = cohort_count >= 2
    average_change = sum(changes) / cohort_count if changes else None
    positive_rate = (
        sum(change > 0 for change in changes) / cohort_count
        if changes
        else None
    )
    supportive = (
        average_change > 0 and positive_rate is not None and positive_rate >= 0.5
        if evaluable and average_change is not None
        else None
    )
    return {
        "available": True,
        "evaluable": evaluable,
        "market": "overseas",
        "source": "tradingview_top_relative_volume_pool",
        "sector_name": sector,
        "industry_name": industry,
        "cohort_count": cohort_count,
        "sector_average_change_pct": (
            round(average_change, 6) if average_change is not None else None
        ),
        "sector_median_change_pct": (
            round(median(changes), 6) if changes else None
        ),
        "sector_positive_rate": (
            round(positive_rate, 6) if positive_rate is not None else None
        ),
        "target_change_pct": target_change,
        "target_minus_sector_average_pct": (
            round(target_change - average_change, 6)
            if target_change is not None and average_change is not None
            else None
        ),
        "supportive_for_long": supportive,
        "reason": "" if evaluable else "sector_cohort_too_small",
        "limitation": "selected_relative_volume_pool_not_broad_sector_benchmark",
    }
