#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"DB 파일 없음: {db_path}", file=sys.stderr)
        raise SystemExit(1)

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
    print("주의: cycle_log의 실거래 통계는 주문 접수 기록 기준이며, 체결확정은 MTS/잔고 기준 확인 필요")
    print("주의: virtual_orders 통계는 excluded_from_performance=0 항목만 포함")

    rows = conn.execute(
        f"""
        SELECT action_bias, action_reason, COUNT(*) AS cnt
        FROM cycle_log
        {cycle_where}
        AND action_bias IN ('BUY_REAL', 'SELL_REAL')
        GROUP BY action_bias, action_reason
        ORDER BY action_bias, cnt DESC
        """
        if cycle_where
        else """
        SELECT action_bias, action_reason, COUNT(*) AS cnt
        FROM cycle_log
        WHERE action_bias IN ('BUY_REAL', 'SELL_REAL')
        GROUP BY action_bias, action_reason
        ORDER BY action_bias, cnt DESC
        """,
        cycle_params,
    ).fetchall()
    print("\n[진입/청산 이유별 건수]")
    for row in rows:
        print(f"  {row['action_bias']:12s} {row['action_reason']:35s} {row['cnt']}건")

    rows = conn.execute(
        f"""
        SELECT market,
               COUNT(*) AS trade_count,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS win_count,
               AVG(pnl_pct) * 100 AS avg_pnl_pct,
               MIN(pnl_pct) * 100 AS min_pnl_pct,
               MAX(pnl_pct) * 100 AS max_pnl_pct,
               {krw_expr} AS total_krw,
               {usd_expr} AS total_usd
        FROM cycle_log
        {cycle_where}
        AND action_bias = 'SELL_REAL'
        GROUP BY market
        """
        if cycle_where
        else """
        SELECT market,
               COUNT(*) AS trade_count,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS win_count,
               AVG(pnl_pct) * 100 AS avg_pnl_pct,
               MIN(pnl_pct) * 100 AS min_pnl_pct,
               MAX(pnl_pct) * 100 AS max_pnl_pct,
               {krw_expr} AS total_krw,
               {usd_expr} AS total_usd
        FROM cycle_log
        WHERE action_bias = 'SELL_REAL'
        GROUP BY market
        """,
        cycle_params,
    ).fetchall()
    print("\n[실거래 손익 통계]")
    for row in rows:
        win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
        print(
            f"  {row['market']:10s} 거래={row['trade_count']}건 승률={win_rate:.0f}% "
            f"평균={row['avg_pnl_pct']:.3f}% 범위=[{row['min_pnl_pct']:.3f}%, {row['max_pnl_pct']:.3f}%] "
            f"누적={int(row['total_krw'] or 0):,}원"
        )

    if has_strategy_flag:
        strategy_cols = (
            "market, COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy, "
            + ("COALESCE(NULLIF(exit_by, ''), 'N/A')" if has_exit_by else "'N/A'")
            + " AS exit_by"
        )
        rows = conn.execute(
            f"""
            SELECT {strategy_cols},
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS win_count,
                   AVG(pnl_pct) * 100 AS avg_pnl_pct,
                   {krw_expr} AS total_krw,
                   {usd_expr} AS total_usd
            FROM cycle_log
            {cycle_where}
            AND action_bias = 'SELL_REAL'
            GROUP BY market, strategy, exit_by
            ORDER BY total_krw ASC
            """
            if cycle_where
            else f"""
            SELECT {strategy_cols},
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS win_count,
                   AVG(pnl_pct) * 100 AS avg_pnl_pct,
                   {krw_expr} AS total_krw,
                   {usd_expr} AS total_usd
            FROM cycle_log
            WHERE action_bias = 'SELL_REAL'
            GROUP BY market, strategy, exit_by
            ORDER BY total_krw ASC
            """,
            cycle_params,
        ).fetchall()
        print("\n[전략별 실주문접수 손익]")
        for row in rows[:15]:
            win_rate = (row["win_count"] / row["trade_count"] * 100) if row["trade_count"] else 0
            print(
                f"  {row['market']:10s} {row['strategy']:12s} exit={row['exit_by']:8s} "
                f"거래={row['trade_count']:3d} 승률={win_rate:3.0f}% 평균={row['avg_pnl_pct']:7.3f}% "
                f"누적={int(row['total_krw'] or 0):,}원"
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
        GROUP BY symbol, market
        ORDER BY buy_count DESC
        LIMIT 10
        """
        if cycle_where
        else """
        SELECT symbol, market, COUNT(*) AS buy_count
        FROM cycle_log
        WHERE action_bias = 'BUY_REAL'
        GROUP BY symbol, market
        ORDER BY buy_count DESC
        LIMIT 10
        """,
        cycle_params,
    ).fetchall()
    print("\n[종목별 진입 빈도]")
    for row in rows:
        print(f"  {row['symbol']:8s} ({row['market']:8s}) {row['buy_count']}건")

    conn.close()


if __name__ == "__main__":
    main()
