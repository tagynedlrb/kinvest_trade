from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_COST_PCT = 0.005


def _parse_kst_cutoff(cutoff_text: str) -> datetime:
    normalized = str(cutoff_text or "").strip().replace("_", "T")
    formats = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).replace(
                tzinfo=ZoneInfo("Asia/Seoul")
            )
        except ValueError:
            continue
    raise ValueError(
        "cutoff must be YYYY-MM-DD or YYYY-MM-DDTHH:MM in KST"
    )


def _format_kst_cutoff(cutoff_kst: datetime) -> str:
    if (
        cutoff_kst.hour == 0
        and cutoff_kst.minute == 0
        and cutoff_kst.second == 0
        and cutoff_kst.microsecond == 0
    ):
        return cutoff_kst.strftime("%Y-%m-%d")
    return cutoff_kst.strftime("%Y-%m-%d %H:%M")


def _cutoff_kst_to_utc_iso(cutoff_date: str) -> str:
    cutoff_kst = _parse_kst_cutoff(cutoff_date)
    return cutoff_kst.astimezone(timezone.utc).isoformat()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _net_pnl_pct_expr(conn: sqlite3.Connection) -> str:
    has_net_usd = _has_column(conn, "cycle_log", "net_pnl_usd")
    has_net_krw = _has_column(conn, "cycle_log", "net_pnl_krw")
    has_entry_price = _has_column(conn, "cycle_log", "entry_price")
    has_qty = _has_column(conn, "cycle_log", "qty_executed")
    if has_entry_price and has_qty and (has_net_usd or has_net_krw):
        overseas_expr = (
            "WHEN lower(market) = 'overseas' "
            "AND net_pnl_usd IS NOT NULL "
            "AND COALESCE(entry_price, 0) > 0 "
            "AND COALESCE(qty_executed, 0) > 0 "
            "THEN net_pnl_usd / (entry_price * qty_executed)"
            if has_net_usd
            else ""
        )
        domestic_expr = (
            "WHEN lower(market) = 'domestic' "
            "AND net_pnl_krw IS NOT NULL "
            "AND COALESCE(entry_price, 0) > 0 "
            "AND COALESCE(qty_executed, 0) > 0 "
            "THEN net_pnl_krw / (entry_price * qty_executed)"
            if has_net_krw
            else ""
        )
        return (
            "CASE "
            f"{overseas_expr} "
            f"{domestic_expr} "
            f"ELSE COALESCE(pnl_pct, 0) - {DEFAULT_COST_PCT} "
            "END"
        )
    return f"COALESCE(pnl_pct, 0) - {DEFAULT_COST_PCT}"


def compare_before_after(db_path: Path | str, cutoff_date: str) -> str:
    """
    Compare SELL_REAL strategy performance before and after a KST cutoff date.

    Prefer actual net PnL columns when cycle_log has enough notional data.
    Otherwise fall back to the legacy 0.5 percentage point cost adjustment.
    """
    db_path_obj = Path(db_path)
    cutoff_kst = _parse_kst_cutoff(cutoff_date)
    cutoff_label = _format_kst_cutoff(cutoff_kst)
    cutoff_utc = cutoff_kst.astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path_obj)
    conn.row_factory = sqlite3.Row
    try:
        net_expr = _net_pnl_pct_expr(conn)
        result = [f"[전략 전후 비교] 기준={cutoff_label} KST"]
        for label, operator in [("이전", "<"), ("이후", ">=")]:
            rows = conn.execute(
                f"""
                WITH evaluated AS (
                    SELECT
                        market,
                        COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy,
                        COALESCE(pnl_pct, 0) AS gross_pnl_pct,
                        {net_expr} AS net_pnl_pct
                    FROM cycle_log
                    WHERE action_bias = 'SELL_REAL'
                      AND logged_at {operator} ?
                )
                SELECT
                    market,
                    strategy,
                    COUNT(*) AS cnt,
                    AVG(gross_pnl_pct) AS avg_gross,
                    AVG(net_pnl_pct) AS avg_net,
                    SUM(CASE WHEN net_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins
                FROM evaluated
                GROUP BY market, strategy
                ORDER BY cnt DESC, strategy ASC
                """,
                (cutoff_utc,),
            ).fetchall()
            result.append(f"[{label} {cutoff_label}]")
            if not rows:
                result.append("  성과=없음")
                continue
            for row in rows:
                cnt = int(row["cnt"] or 0)
                wins = int(row["wins"] or 0)
                net = float(row["avg_net"] or 0.0)
                win_rate = (wins / cnt * 100.0) if cnt else 0.0
                market = str(row["market"] or "-")
                strategy = str(row["strategy"] or "N/A")
                result.append(
                    f"  {market:<8} {strategy:<15} {cnt:>3}건  "
                    f"net={net * 100:+.3f}%  승률={win_rate:.0f}%"
                )
        return "\n".join(result)
    finally:
        conn.close()
