from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _cutoff_kst_to_utc_iso(cutoff_date: str) -> str:
    cutoff_kst = datetime.strptime(cutoff_date, "%Y-%m-%d").replace(
        tzinfo=ZoneInfo("Asia/Seoul")
    )
    return cutoff_kst.astimezone(timezone.utc).isoformat()


def compare_before_after(db_path: Path | str, cutoff_date: str) -> str:
    """
    Compare SELL_REAL strategy performance before and after a KST cutoff date.

    ``pnl_pct`` is stored as a decimal ratio in cycle_log, so a 0.005 cost
    adjustment means roughly 0.5 percentage points.
    """
    db_path_obj = Path(db_path)
    cutoff_utc = _cutoff_kst_to_utc_iso(cutoff_date)
    conn = sqlite3.connect(db_path_obj)
    conn.row_factory = sqlite3.Row
    try:
        result = [f"[전략 전후 비교] 기준일={cutoff_date} KST"]
        for label, operator in [("이전", "<"), ("이후", ">=")]:
            rows = conn.execute(
                f"""
                SELECT
                    market,
                    COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy,
                    COUNT(*) AS cnt,
                    AVG(COALESCE(pnl_pct, 0)) AS avg_gross,
                    SUM(CASE WHEN COALESCE(pnl_pct, 0) > 0 THEN 1 ELSE 0 END) AS wins
                FROM cycle_log
                WHERE action_bias = 'SELL_REAL'
                  AND logged_at {operator} ?
                GROUP BY market, strategy
                ORDER BY cnt DESC, strategy ASC
                """,
                (cutoff_utc,),
            ).fetchall()
            result.append(f"[{label} {cutoff_date}]")
            if not rows:
                result.append("  성과=없음")
                continue
            for row in rows:
                cnt = int(row["cnt"] or 0)
                wins = int(row["wins"] or 0)
                avg_gross = float(row["avg_gross"] or 0.0)
                net = avg_gross - 0.005
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
