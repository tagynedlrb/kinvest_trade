#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kinvest_trade.repository import (
    CONFIRMED_BUY_CYCLE_PREDICATE,
    CONFIRMED_SELL_CYCLE_PREDICATE,
    CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE,
)
from kinvest_trade.trade_analysis import (
    _net_pnl_pct_expr,
    compare_before_after,
    summarize_market_regime_performance,
    summarize_wait_bottlenecks,
    summarize_wait_forward_performance,
)


def _where_sql(column: str, since: str) -> tuple[str, list[str]]:
    if not since:
        return "", []
    return f"WHERE {column} >= ?", [since]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def main() -> None:
    parser = argparse.ArgumentParser(description="거래 내역 분석")
    parser.add_argument("db_path", help="SQLite DB 파일 경로")
    parser.add_argument("--days", type=int, default=0, help="최근 N일 분석 (0=전체)")
    parser.add_argument(
        "--compare-date",
        help="기준일/시각 전후 SELL_REAL 전략 성과 비교 (KST, YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM)",
    )
    parser.add_argument("--wait-hours", type=int, default=0, help="최근 N시간 WAIT 병목 요약")
    parser.add_argument(
        "--wait-forward-hours",
        type=int,
        default=0,
        help="최근 N시간 WAIT 에피소드의 15/30/60분 선행성과",
    )
    parser.add_argument(
        "--wait-forward-market",
        choices=("domestic", "overseas"),
        default="",
        help="WAIT 선행성과 시장 필터",
    )
    parser.add_argument(
        "--wait-forward-reason",
        default="",
        help="WAIT 선행성과 사유 필터",
    )
    parser.add_argument(
        "--wait-forward-env",
        choices=("vps", "prod"),
        default="vps",
        help="WAIT 선행성과 주문가능 세션 프로필",
    )
    parser.add_argument("--wait-limit", type=int, default=12, help="WAIT 병목 출력 행 수")
    parser.add_argument("--regime-limit", type=int, default=12, help="시장 레짐 성과 출력 행 수")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"DB 파일 없음: {db_path}", file=sys.stderr)
        raise SystemExit(1)

    if args.compare_date:
        try:
            print(compare_before_after(db_path, args.compare_date))
            print(
                summarize_market_regime_performance(
                    db_path,
                    days=args.days,
                    limit=args.regime_limit,
                )
            )
        except ValueError as exc:
            print(f"기준일 형식 오류: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        return
    if args.wait_forward_hours > 0:
        print(
            summarize_wait_forward_performance(
                db_path,
                hours=args.wait_forward_hours,
                limit=args.wait_limit,
                market=args.wait_forward_market,
                reason=args.wait_forward_reason,
                orderable_env=args.wait_forward_env,
            )
        )
        return
    if args.wait_hours > 0:
        print(
            summarize_wait_bottlenecks(
                db_path,
                hours=args.wait_hours,
                limit=args.wait_limit,
            )
        )
        return

    since = ""
    if args.days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    has_cycle_net_krw = _has_column(conn, "cycle_log", "net_pnl_krw")
    has_cycle_net_usd = _has_column(conn, "cycle_log", "net_pnl_usd")
    has_strategy_flag = _has_column(conn, "cycle_log", "strategy_flag")
    has_exit_by = _has_column(conn, "cycle_log", "exit_by")
    has_virtual_excluded = _has_column(conn, "virtual_orders", "excluded_from_performance")
    net_pct_expr = _net_pnl_pct_expr(conn)
    krw_expr = (
        "SUM(COALESCE(net_pnl_krw, realized_pnl_krw, 0))"
        if has_cycle_net_krw
        else "SUM(realized_pnl_krw)"
    )
    usd_expr = (
        "SUM(COALESCE(net_pnl_usd, realized_pnl_usd, 0))"
        if has_cycle_net_usd
        else "SUM(realized_pnl_usd)"
    )

    cycle_where, cycle_params = _where_sql("logged_at", since)
    virtual_where, virtual_params = _where_sql("created_at", since)

    print("=" * 60)
    print(f"거래 분석 ({args.days}일 기준)" if args.days else "거래 분석 (전체)")
    print("=" * 60)
    print("주의: 실거래 성과/빈도는 KIS 체결확정 원장 중 세션소유 cycle_log만 포함")
    print("주의: 계좌 손익 통계는 외부 보유를 포함한 모든 KIS 확정 청산")
    print("주의: virtual_orders 통계는 excluded_from_performance=0 항목만 포함")

    rows = conn.execute(
        f"""
        SELECT
            action_bias,
            COUNT(*) AS recorded_count,
            SUM(
                CASE
                    WHEN action_bias = 'BUY_REAL'
                         AND {CONFIRMED_BUY_CYCLE_PREDICATE}
                    THEN 1
                    WHEN action_bias = 'SELL_REAL'
                         AND {CONFIRMED_SELL_CYCLE_PREDICATE}
                    THEN 1
                    ELSE 0
                END
            ) AS confirmed_count,
            SUM(
                CASE
                    WHEN action_bias = 'BUY_REAL'
                         AND {CONFIRMED_BUY_CYCLE_PREDICATE}
                    THEN 1
                    WHEN action_bias = 'SELL_REAL'
                         AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE}
                    THEN 1
                    ELSE 0
                END
            ) AS strategy_count
        FROM cycle_log
        {cycle_where}
        AND action_bias IN ('BUY_REAL', 'SELL_REAL')
        GROUP BY action_bias
        ORDER BY action_bias
        """
        if cycle_where
        else f"""
        SELECT
            action_bias,
            COUNT(*) AS recorded_count,
            SUM(
                CASE
                    WHEN action_bias = 'BUY_REAL'
                         AND {CONFIRMED_BUY_CYCLE_PREDICATE}
                    THEN 1
                    WHEN action_bias = 'SELL_REAL'
                         AND {CONFIRMED_SELL_CYCLE_PREDICATE}
                    THEN 1
                    ELSE 0
                END
            ) AS confirmed_count,
            SUM(
                CASE
                    WHEN action_bias = 'BUY_REAL'
                         AND {CONFIRMED_BUY_CYCLE_PREDICATE}
                    THEN 1
                    WHEN action_bias = 'SELL_REAL'
                         AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE}
                    THEN 1
                    ELSE 0
                END
            ) AS strategy_count
        FROM cycle_log
        WHERE action_bias IN ('BUY_REAL', 'SELL_REAL')
        GROUP BY action_bias
        ORDER BY action_bias
        """,
        cycle_params,
    ).fetchall()
    print("\n[실주문 측정 경계]")
    for row in rows:
        recorded_count = int(row["recorded_count"] or 0)
        confirmed_count = int(row["confirmed_count"] or 0)
        strategy_count = int(row["strategy_count"] or 0)
        print(
            f"  {row['action_bias']:12s} 체결확정={confirmed_count}건 "
            f"미체결제외={recorded_count - confirmed_count}건 "
            f"전략제외={confirmed_count - strategy_count}건"
        )

    rows = conn.execute(
        f"""
        SELECT
            market,
            SUM(CASE WHEN action_bias = 'BUY_REAL' THEN 1 ELSE 0 END)
                AS buy_count,
            SUM(CASE WHEN action_bias = 'SELL_REAL' THEN 1 ELSE 0 END)
                AS sell_count
        FROM cycle_log
        {cycle_where}
        AND (
            (action_bias = 'BUY_REAL' AND {CONFIRMED_BUY_CYCLE_PREDICATE})
            OR
            (action_bias = 'SELL_REAL' AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE})
        )
        GROUP BY market
        ORDER BY market
        """
        if cycle_where
        else f"""
        SELECT
            market,
            SUM(CASE WHEN action_bias = 'BUY_REAL' THEN 1 ELSE 0 END)
                AS buy_count,
            SUM(CASE WHEN action_bias = 'SELL_REAL' THEN 1 ELSE 0 END)
                AS sell_count
        FROM cycle_log
        WHERE
            (action_bias = 'BUY_REAL' AND {CONFIRMED_BUY_CYCLE_PREDICATE})
            OR
            (action_bias = 'SELL_REAL' AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE})
        GROUP BY market
        ORDER BY market
        """,
        cycle_params,
    ).fetchall()
    print("\n[시장별 세션소유 KIS 체결확정 거래 빈도]")
    for row in rows:
        print(
            f"  {row['market']:10s} "
            f"매수={int(row['buy_count'] or 0)}건 "
            f"청산={int(row['sell_count'] or 0)}건"
        )

    rows = conn.execute(
        f"""
        SELECT action_bias, action_reason, COUNT(*) AS cnt
        FROM cycle_log
        {cycle_where}
        AND (
            (action_bias = 'BUY_REAL' AND {CONFIRMED_BUY_CYCLE_PREDICATE})
            OR
            (action_bias = 'SELL_REAL' AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE})
        )
        GROUP BY action_bias, action_reason
        ORDER BY action_bias, cnt DESC
        """
        if cycle_where
        else f"""
        SELECT action_bias, action_reason, COUNT(*) AS cnt
        FROM cycle_log
        WHERE
            (action_bias = 'BUY_REAL' AND {CONFIRMED_BUY_CYCLE_PREDICATE})
            OR
            (action_bias = 'SELL_REAL' AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE})
        GROUP BY action_bias, action_reason
        ORDER BY action_bias, cnt DESC
        """,
        cycle_params,
    ).fetchall()
    print("\n[세션소유 KIS 체결확정 진입/청산 이유별 건수]")
    for row in rows:
        print(f"  {row['action_bias']:12s} {row['action_reason']:35s} {row['cnt']}건")

    rows = conn.execute(
        f"""
        SELECT market,
	               COUNT(*) AS trade_count,
	               SUM(CASE WHEN ({net_pct_expr}) > 0 THEN 1 ELSE 0 END) AS win_count,
	               AVG(pnl_pct) * 100 AS avg_gross_pnl_pct,
	               AVG(({net_pct_expr})) * 100 AS avg_net_pnl_pct,
	               MIN(pnl_pct) * 100 AS min_pnl_pct,
	               MAX(pnl_pct) * 100 AS max_pnl_pct,
	               {krw_expr} AS total_krw,
               {usd_expr} AS total_usd
        FROM cycle_log
        {cycle_where}
        AND action_bias = 'SELL_REAL'
        AND {CONFIRMED_SELL_CYCLE_PREDICATE}
        GROUP BY market
        """
        if cycle_where
        else f"""
        SELECT market,
	               COUNT(*) AS trade_count,
	               SUM(CASE WHEN ({net_pct_expr}) > 0 THEN 1 ELSE 0 END) AS win_count,
	               AVG(pnl_pct) * 100 AS avg_gross_pnl_pct,
	               AVG(({net_pct_expr})) * 100 AS avg_net_pnl_pct,
	               MIN(pnl_pct) * 100 AS min_pnl_pct,
	               MAX(pnl_pct) * 100 AS max_pnl_pct,
	               {krw_expr} AS total_krw,
               {usd_expr} AS total_usd
        FROM cycle_log
        WHERE action_bias = 'SELL_REAL'
        AND {CONFIRMED_SELL_CYCLE_PREDICATE}
        GROUP BY market
        """,
        cycle_params,
    ).fetchall()
    print("\n[계좌 KIS 체결확정 손익 통계: 외부 보유 포함]")
    for row in rows:
        win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
        print(
            f"  {row['market']:10s} 거래={row['trade_count']}건 승률={win_rate:.0f}% "
            f"평균Gross={row['avg_gross_pnl_pct']:.3f}% 평균Net={row['avg_net_pnl_pct']:.3f}% "
            f"범위=[{row['min_pnl_pct']:.3f}%, {row['max_pnl_pct']:.3f}%] "
            f"누적={int(row['total_krw'] or 0):,}원"
        )

    if has_strategy_flag:
        # action_reason holds momentum_policy's actual exit decision
        # (trend_filter_lost/atr_hard_stop/momentum_loss_cut/...); exit_by is
        # a separate, coarser label from the per-symbol strategy manager's own
        # preview check and is often just "VWAP"/"RSI" -- overwritten there
        # whenever that check also independently agrees a sell is due. It
        # must only be a fallback, not take precedence, or this breakdown
        # silently reclassifies e.g. a hard-stop as a generic "VWAP" exit.
        exit_expr = (
            "COALESCE(NULLIF(action_reason, ''), NULLIF(exit_by, ''), 'N/A')"
            if has_exit_by
            else "COALESCE(NULLIF(action_reason, ''), 'N/A')"
        )
        strategy_cols = (
            "market, COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy, "
            + exit_expr
            + " AS exit_reason"
        )
        rows = conn.execute(
            f"""
            SELECT {strategy_cols},
                   COUNT(*) AS trade_count,
	                   SUM(CASE WHEN ({net_pct_expr}) > 0 THEN 1 ELSE 0 END) AS win_count,
	                   AVG(pnl_pct) * 100 AS avg_gross_pnl_pct,
	                   AVG(({net_pct_expr})) * 100 AS avg_net_pnl_pct,
	                   {krw_expr} AS total_krw,
	                   {usd_expr} AS total_usd
            FROM cycle_log
            {cycle_where}
            AND action_bias = 'SELL_REAL'
            AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE}
            GROUP BY market, strategy, exit_reason
            ORDER BY total_krw ASC
            """
            if cycle_where
            else f"""
            SELECT {strategy_cols},
                   COUNT(*) AS trade_count,
	                   SUM(CASE WHEN ({net_pct_expr}) > 0 THEN 1 ELSE 0 END) AS win_count,
	                   AVG(pnl_pct) * 100 AS avg_gross_pnl_pct,
	                   AVG(({net_pct_expr})) * 100 AS avg_net_pnl_pct,
	                   {krw_expr} AS total_krw,
	                   {usd_expr} AS total_usd
            FROM cycle_log
            WHERE action_bias = 'SELL_REAL'
            AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE}
            GROUP BY market, strategy, exit_reason
            ORDER BY total_krw ASC
            """,
            cycle_params,
        ).fetchall()
        print("\n[전략별 세션소유 KIS 체결확정 손익]")
        for row in rows[:15]:
            win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
            print(
                f"  {row['market']:10s} {row['strategy']:12s} exit={row['exit_reason']:22s} "
                f"거래={row['trade_count']:3d} 승률={win_rate:3.0f}% "
                f"평균Gross={row['avg_gross_pnl_pct']:7.3f}% 평균Net={row['avg_net_pnl_pct']:7.3f}% "
                f"누적={int(row['total_krw'] or 0):,}원"
            )

    print(
        "\n"
        + summarize_market_regime_performance(
            db_path,
            days=args.days,
            limit=args.regime_limit,
        )
    )

    virtual_extra_filter = "AND COALESCE(excluded_from_performance, 0) = 0" if has_virtual_excluded else ""
    rows = conn.execute(
        f"""
        SELECT market, currency,
               COUNT(*) AS trade_count,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS win_count,
               AVG(realized_pnl_pct) * 100 AS avg_pnl_pct,
               SUM(realized_pnl) AS total_pnl
        FROM virtual_orders
        WHERE side = 'sell'
        {virtual_extra_filter}
        {'AND created_at >= ?' if since else ''}
        GROUP BY market, currency
        """,
        virtual_params,
    ).fetchall()
    print("\n[가상거래 손익 통계]")
    for row in rows:
        win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
        print(
            f"  {row['market']:10s}/{row['currency']:3s} 거래={row['trade_count']}건 승률={win_rate:.0f}% "
            f"평균={row['avg_pnl_pct']:.3f}% 누적={row['total_pnl']:.2f}{row['currency']}"
        )

    rows = conn.execute(
        f"""
        SELECT reason,
               COUNT(*) AS trade_count,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS win_count,
               AVG(realized_pnl_pct) * 100 AS avg_pnl_pct,
               SUM(realized_pnl) AS total_pnl
        FROM virtual_orders
        WHERE side = 'sell'
        {virtual_extra_filter}
        {'AND created_at >= ?' if since else ''}
        GROUP BY reason
        ORDER BY total_pnl ASC
        LIMIT 12
        """,
        virtual_params,
    ).fetchall()
    print("\n[가상거래 청산 이유별 손익]")
    for row in rows:
        win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
        print(
            f"  {row['reason']:30s} 거래={row['trade_count']:3d} 승률={win_rate:3.0f}% "
            f"평균={row['avg_pnl_pct']:7.3f}% 누적={row['total_pnl']:.2f}"
        )

    rows = conn.execute(
        f"""
        SELECT action_reason, COUNT(*) AS cnt
        FROM cycle_log
        {cycle_where}
        AND action_bias = 'WAIT'
        GROUP BY action_reason
        ORDER BY cnt DESC
        LIMIT 10
        """
        if cycle_where
        else """
        SELECT action_reason, COUNT(*) AS cnt
        FROM cycle_log
        WHERE action_bias = 'WAIT'
        GROUP BY action_reason
        ORDER BY cnt DESC
        LIMIT 10
        """,
        cycle_params,
    ).fetchall()
    print("\n[WAIT 원인 빈도 (상위 10)]")
    total_wait = sum(int(row["cnt"]) for row in rows)
    for row in rows:
        pct = (row["cnt"] / total_wait * 100) if total_wait else 0
        print(f"  {row['action_reason']:35s} {row['cnt']:5d}건 ({pct:.1f}%)")

    rows = conn.execute(
        f"""
        SELECT symbol, market, COUNT(*) AS buy_count
        FROM cycle_log
        {cycle_where}
        AND action_bias = 'BUY_REAL'
        AND {CONFIRMED_BUY_CYCLE_PREDICATE}
        GROUP BY symbol, market
        ORDER BY buy_count DESC
        LIMIT 10
        """
        if cycle_where
        else f"""
        SELECT symbol, market, COUNT(*) AS buy_count
        FROM cycle_log
        WHERE action_bias = 'BUY_REAL'
        AND {CONFIRMED_BUY_CYCLE_PREDICATE}
        GROUP BY symbol, market
        ORDER BY buy_count DESC
        LIMIT 10
        """,
        cycle_params,
    ).fetchall()
    print("\n[KIS 체결확정 종목별 진입 빈도]")
    for row in rows:
        print(f"  {row['symbol']:8s} ({row['market']:8s}) {row['buy_count']}건")

    conn.close()


if __name__ == "__main__":
    main()
