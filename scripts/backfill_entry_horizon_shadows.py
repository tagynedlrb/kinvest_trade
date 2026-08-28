#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kinvest_trade.auto_trade_math import is_domestic_sell_tax_exempt
from kinvest_trade.config import load_app_config
from kinvest_trade.market_policy import get_market_auto_trade_config
from kinvest_trade.repository import (
    CONFIRMED_BUY_CYCLE_PREDICATE,
    SqliteRepository,
)
from kinvest_trade.time_utils import ensure_timezone, parse_datetime

KST = ZoneInfo("Asia/Seoul")
DEFAULT_HORIZONS = (5, 10, 15, 30, 45, 60, 90, 120)


def _online_backup(db_path: Path) -> dict[str, object]:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = (
        db_path.parent
        / f"{db_path.stem}_backup_{timestamp}_pre_entry_horizon_backfill.db"
    )
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(
            "backup verification failed: "
            f"integrity={integrity}, foreign_key_violations={len(foreign_keys)}"
        )
    return {
        "path": str(backup_path),
        "sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
    }


def _range_position(regime: dict[str, object]) -> float | None:
    try:
        recorded = regime.get("session_range_position")
        if recorded is not None:
            return min(1.0, max(0.0, float(recorded)))
        high = float(regime.get("high_price") or 0.0)
        low = float(regime.get("low_price") or 0.0)
        close = float(regime.get("close_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if high <= low or close <= 0:
        return None
    return min(1.0, max(0.0, (close - low) / (high - low)))


def _load_json_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _confirmed_entries(
    conn: sqlite3.Connection,
    *,
    since: str,
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT
            cycle_log.*,
            (
                SELECT execution.context_json
                FROM broker_order_executions AS execution
                WHERE execution.execution_group_id = cycle_log.execution_group_id
                  AND UPPER(execution.side) = 'BUY'
                  AND execution.filled_qty > 0
                ORDER BY execution.filled_qty DESC, execution.id DESC
                LIMIT 1
            ) AS execution_context_json
        FROM cycle_log
        WHERE cycle_log.market = 'domestic'
          AND cycle_log.action_bias = 'BUY_REAL'
          AND cycle_log.logged_at >= ?
          AND COALESCE(cycle_log.price, 0) > 0
          AND COALESCE(cycle_log.qty_executed, 0) > 0
          AND {CONFIRMED_BUY_CYCLE_PREDICATE}
        ORDER BY cycle_log.logged_at, cycle_log.id
        """,
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def _next_observation(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    target_at: datetime,
    session_date: str,
    max_lag_minutes: int,
) -> dict[str, object] | None:
    latest_at = target_at + timedelta(minutes=max(0, max_lag_minutes))
    row = conn.execute(
        """
        SELECT id, logged_at, price, action_bias, action_reason
        FROM cycle_log
        WHERE market = 'domestic'
          AND symbol = ?
          AND COALESCE(price, 0) > 0
          AND logged_at >= ?
          AND logged_at <= ?
        ORDER BY logged_at, id
        LIMIT 1
        """,
        (
            symbol,
            target_at.astimezone(UTC).isoformat(),
            latest_at.astimezone(UTC).isoformat(),
        ),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    observed_at = parse_datetime(result.get("logged_at"))
    if observed_at is None:
        return None
    observed = ensure_timezone(observed_at).astimezone(UTC)
    if observed.astimezone(KST).date().isoformat() != session_date:
        return None
    result["observed_at"] = observed
    result["lag_sec"] = max(0, int((observed - target_at).total_seconds()))
    return result


def build_plan(
    db_path: Path,
    *,
    since: str,
    horizons: tuple[int, ...],
    max_lag_minutes: int,
    commission_rate: float,
    stock_sell_tax_rate: float,
) -> list[dict[str, object]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        plan: list[dict[str, object]] = []
        for entry in _confirmed_entries(conn, since=since):
            opened_at = parse_datetime(entry.get("logged_at"))
            if opened_at is None:
                continue
            opened = ensure_timezone(opened_at).astimezone(UTC)
            session_date = opened.astimezone(KST).date().isoformat()
            product_type = str(entry.get("product_type") or "").strip()
            sell_tax_rate = (
                0.0
                if is_domestic_sell_tax_exempt(product_type)
                else max(0.0, stock_sell_tax_rate)
            )
            execution_context = _load_json_dict(
                entry.get("execution_context_json")
            )
            market_regime = _load_json_dict(
                execution_context.get("entry_market_regime")
            )
            observations: dict[int, dict[str, object] | None] = {}
            for horizon in horizons:
                observations[horizon] = _next_observation(
                    conn,
                    symbol=str(entry.get("symbol") or "").strip().upper(),
                    target_at=opened + timedelta(minutes=horizon),
                    session_date=session_date,
                    max_lag_minutes=max_lag_minutes,
                )
            plan.append(
                {
                    "group_id": (
                        "historical-"
                        f"{entry.get('execution_group_id') or entry.get('id')!s}"
                    ),
                    "opened_at": opened,
                    "market": "domestic",
                    "symbol": str(entry.get("symbol") or "").strip().upper(),
                    "exchange_code": str(entry.get("exchange_code") or "KRX"),
                    "entry_session_date": session_date,
                    "policy_id": "historical_as_traded",
                    "cohort": "historical_confirmed_entry",
                    "strategy_flag": str(entry.get("strategy_flag") or ""),
                    "entry_by": str(entry.get("entry_by") or ""),
                    "entry_price": float(entry.get("price") or 0.0),
                    "round_trip_cost_pct": (
                        max(0.0, commission_rate) * 2.0 + sell_tax_rate
                    ),
                    "benchmark_return_pct": market_regime.get("return_pct"),
                    "benchmark_regime_key": str(
                        market_regime.get("regime_key") or ""
                    ),
                    "benchmark_range_position": _range_position(market_regime),
                    "context": {
                        "source": "confirmed_domestic_entry_backfill_v1",
                        "cycle_log_id": int(entry.get("id") or 0),
                        "execution_group_id": str(
                            entry.get("execution_group_id") or ""
                        ),
                        "product_type": product_type,
                        "commission_rate": max(0.0, commission_rate),
                        "sell_tax_rate": sell_tax_rate,
                        "entry_market_regime": market_regime,
                    },
                    "observations": observations,
                }
            )
        return plan
    finally:
        conn.close()


def _plan_summary(plan: list[dict[str, object]]) -> dict[str, object]:
    strategies = Counter(str(row.get("strategy_flag") or "-") for row in plan)
    matured_by_horizon: Counter[int] = Counter()
    expired_by_horizon: Counter[int] = Counter()
    for row in plan:
        observations = row["observations"]
        assert isinstance(observations, dict)
        for horizon, observation in observations.items():
            if observation is None:
                expired_by_horizon[int(horizon)] += 1
            else:
                matured_by_horizon[int(horizon)] += 1
    return {
        "entry_count": len(plan),
        "session_count": len(
            {str(row.get("entry_session_date") or "") for row in plan}
        ),
        "strategy_counts": dict(sorted(strategies.items())),
        "matured_by_horizon": dict(sorted(matured_by_horizon.items())),
        "expired_by_horizon": dict(sorted(expired_by_horizon.items())),
    }


def apply_plan(
    repository: SqliteRepository,
    plan: list[dict[str, object]],
    *,
    horizons: tuple[int, ...],
) -> dict[str, int]:
    result = {"groups_opened": 0, "matured": 0, "expired": 0}
    for row in plan:
        group_id = str(row["group_id"])
        opened = repository.open_entry_horizon_shadow_group(
            opened_at=row["opened_at"],
            market=str(row["market"]),
            symbol=str(row["symbol"]),
            exchange_code=str(row["exchange_code"]),
            entry_session_date=str(row["entry_session_date"]),
            policy_id=str(row["policy_id"]),
            cohort=str(row["cohort"]),
            strategy_flag=str(row["strategy_flag"]),
            entry_by=str(row["entry_by"]),
            block_reason="",
            entry_price=float(row["entry_price"]),
            round_trip_cost_pct=float(row["round_trip_cost_pct"]),
            horizons_minutes=horizons,
            benchmark_return_pct=row.get("benchmark_return_pct"),
            benchmark_regime_key=str(row["benchmark_regime_key"]),
            benchmark_range_position=row.get("benchmark_range_position"),
            context=row["context"],
            group_id=group_id,
            allow_overlap=True,
        )
        if opened:
            result["groups_opened"] += 1
        observations = row["observations"]
        assert isinstance(observations, dict)
        for horizon in horizons:
            observation = observations.get(horizon)
            if observation is None:
                repository.finalize_entry_horizon_shadow(
                    group_id=group_id,
                    horizon_minutes=horizon,
                    status="EXPIRED",
                )
                result["expired"] += 1
                continue
            assert isinstance(observation, dict)
            repository.finalize_entry_horizon_shadow(
                group_id=group_id,
                horizon_minutes=horizon,
                status="MATURED",
                observed_at=observation["observed_at"],
                exit_price=float(observation["price"]),
                observation_lag_sec=int(observation["lag_sec"]),
            )
            result["matured"] += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="국장 확정 매수의 고정 보유시간 모의군 백필"
    )
    parser.add_argument("db_path", type=Path)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("config/fixed_config.json"),
    )
    parser.add_argument("--since", default="2026-08-12T00:00:00+00:00")
    parser.add_argument("--max-lag-minutes", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"DB 파일 없음: {args.db_path}")

    config = load_app_config(args.settings)
    domestic = get_market_auto_trade_config(config, "domestic")
    plan = build_plan(
        args.db_path,
        since=args.since,
        horizons=DEFAULT_HORIZONS,
        max_lag_minutes=max(0, args.max_lag_minutes),
        commission_rate=float(domestic.domestic_commission_rate),
        stock_sell_tax_rate=float(domestic.domestic_sell_tax_rate),
    )
    output: dict[str, object] = {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "since": args.since,
        "max_lag_minutes": max(0, args.max_lag_minutes),
        "horizons_minutes": list(DEFAULT_HORIZONS),
        "plan": _plan_summary(plan),
    }
    if args.apply:
        output["backup"] = _online_backup(args.db_path)
        repository = SqliteRepository(args.db_path)
        output["applied"] = apply_plan(
            repository,
            plan,
            horizons=DEFAULT_HORIZONS,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
