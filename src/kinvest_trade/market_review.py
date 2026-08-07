from __future__ import annotations

import json
from collections import defaultdict
from typing import Iterable


MARKET_SESSION_REVIEW_VERSION = "market_session_review_v1"


def _as_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _net_pnl_pct(row: dict) -> float:
    entry_price = _as_float(row.get("entry_price"))
    quantity = _as_int(row.get("qty_executed"))
    if entry_price <= 0 or quantity <= 0:
        return 0.0
    notional = entry_price * quantity
    market = str(row.get("market") or "").strip().lower()
    if market == "domestic" and row.get("net_pnl_krw") is not None:
        return _as_float(row.get("net_pnl_krw")) / notional
    if market == "overseas" and row.get("net_pnl_usd") is not None:
        return _as_float(row.get("net_pnl_usd")) / notional
    return 0.0


def _performance_summary(rows: Iterable[dict], key_name: str) -> dict[str, dict]:
    grouped: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {
            "count": 0,
            "wins": 0,
            "gross_pnl_pct_sum": 0.0,
            "net_pnl_pct_sum": 0.0,
            "net_pnl_usd": 0.0,
            "net_pnl_krw": 0.0,
        }
    )
    for row in rows:
        key = str(row.get(key_name) or "N/A").strip() or "N/A"
        bucket = grouped[key]
        net_pct = _net_pnl_pct(row)
        bucket["count"] = int(bucket["count"]) + 1
        bucket["wins"] = int(bucket["wins"]) + int(net_pct > 0)
        bucket["gross_pnl_pct_sum"] = float(bucket["gross_pnl_pct_sum"]) + _as_float(
            row.get("pnl_pct")
        )
        bucket["net_pnl_pct_sum"] = float(bucket["net_pnl_pct_sum"]) + net_pct
        bucket["net_pnl_usd"] = float(bucket["net_pnl_usd"]) + _as_float(
            row.get("net_pnl_usd")
        )
        bucket["net_pnl_krw"] = float(bucket["net_pnl_krw"]) + _as_float(
            row.get("net_pnl_krw")
        )
    return {key: dict(grouped[key]) for key in sorted(grouped)}


def _load_entry_context(context_text: object) -> dict:
    try:
        context = json.loads(str(context_text or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(context, dict):
        return {}
    return context


def _load_entry_regime(context_text: object) -> dict:
    context = _load_entry_context(context_text)
    regime = context.get("entry_market_regime")
    return regime if isinstance(regime, dict) else {}


def build_market_session_review(
    *,
    regime: dict,
    entries: list[dict],
    exits: list[dict],
    entry_context_by_group: dict[str, object],
    reviewed_at: str,
) -> dict:
    market = str(regime.get("market") or "").strip().lower()
    session_date = str(regime.get("session_date") or "")
    context_count = 0
    session_match_count = 0
    sector_context_count = 0
    sector_evaluable_count = 0
    sector_supportive_count = 0
    for entry in entries:
        group_id = str(entry.get("execution_group_id") or "")
        entry_context = _load_entry_context(
            entry_context_by_group.get(group_id)
        )
        entry_regime = entry_context.get("entry_market_regime")
        if not isinstance(entry_regime, dict):
            entry_regime = {}
        if bool(entry_regime.get("available")):
            context_count += 1
            if (
                str(entry_regime.get("market") or "").strip().lower() == market
                and str(entry_regime.get("session_date") or "") == session_date
            ):
                session_match_count += 1

        entry_sector = entry_context.get("entry_sector_context")
        if not isinstance(entry_sector, dict) or not bool(
            entry_sector.get("available")
        ):
            continue
        sector_context_count += 1
        if bool(entry_sector.get("evaluable")):
            sector_evaluable_count += 1
            sector_supportive_count += int(
                entry_sector.get("supportive_for_long") is True
            )

    exit_count = len(exits)
    net_pnl_pct_sum = sum(_net_pnl_pct(row) for row in exits)
    net_pnl_usd = sum(_as_float(row.get("net_pnl_usd")) for row in exits)
    net_pnl_krw = sum(_as_float(row.get("net_pnl_krw")) for row in exits)
    quality = {
        "final_regime_available": bool(regime.get("is_final")),
        "volume_available": _as_float(regime.get("volume")) > 0,
        "activity_metric_available": regime.get("volume_ratio_20") is not None,
        "entry_regime_context_count": context_count,
        "entry_regime_context_missing_count": max(0, len(entries) - context_count),
        "entry_regime_context_session_match_count": session_match_count,
        "entry_regime_context_session_mismatch_count": max(
            0, context_count - session_match_count
        ),
        "entry_regime_coverage_pct": (
            context_count / len(entries) if entries else 1.0
        ),
        "entry_regime_session_match_pct": (
            session_match_count / context_count if context_count else 1.0
        ),
        "entry_sector_context_count": sector_context_count,
        "entry_sector_context_missing_count": max(
            0, len(entries) - sector_context_count
        ),
        "entry_sector_evaluable_count": sector_evaluable_count,
        "entry_sector_supportive_count": sector_supportive_count,
        "entry_sector_coverage_pct": (
            sector_context_count / len(entries) if entries else 1.0
        ),
        "trade_source": "confirmed_session_owned_cycle_log",
    }
    return {
        "market": market,
        "session_date": session_date,
        "benchmark_code": str(regime.get("benchmark_code") or ""),
        "benchmark_name": str(regime.get("benchmark_name") or ""),
        "source": str(regime.get("source") or ""),
        "regime_captured_at": str(regime.get("captured_at") or ""),
        "reviewed_at": reviewed_at,
        "close_price": regime.get("close_price"),
        "return_pct": regime.get("return_pct"),
        "volume": regime.get("volume"),
        "turnover": regime.get("turnover"),
        "volume_ratio_20": regime.get("volume_ratio_20"),
        "range_pct": regime.get("range_pct"),
        "range_ratio_20": regime.get("range_ratio_20"),
        "regime_key": str(regime.get("regime_key") or "unknown|unknown|unknown"),
        "confirmed_entry_count": len(entries),
        "entry_regime_context_count": context_count,
        "entry_regime_session_match_count": session_match_count,
        "confirmed_exit_count": exit_count,
        "win_count": sum(int(_net_pnl_pct(row) > 0) for row in exits),
        "gross_pnl_pct_sum": sum(_as_float(row.get("pnl_pct")) for row in exits),
        "net_pnl_pct_sum": net_pnl_pct_sum,
        "net_pnl_usd": net_pnl_usd,
        "net_pnl_krw": net_pnl_krw,
        "strategy_summary_json": _performance_summary(exits, "strategy_flag"),
        "exit_reason_summary_json": _performance_summary(exits, "action_reason"),
        "quality_json": quality,
        "calculation_version": MARKET_SESSION_REVIEW_VERSION,
    }
