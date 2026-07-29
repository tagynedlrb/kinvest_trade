from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .auto_trade_math import is_domestic_sell_tax_exempt
from .market_sessions import (
    is_krx_regular_session,
    is_us_orderable_session_for_env,
)
from .repository import CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE

DEFAULT_OVERSEAS_COST_PCT = 0.005
DEFAULT_DOMESTIC_COMMISSION_PCT = 0.00015
DEFAULT_DOMESTIC_SELL_TAX_PCT = 0.002
DEFAULT_DOMESTIC_TAX_EXEMPT_COST_PCT = DEFAULT_DOMESTIC_COMMISSION_PCT * 2
DEFAULT_DOMESTIC_STOCK_COST_PCT = (
    DEFAULT_DOMESTIC_TAX_EXEMPT_COST_PCT + DEFAULT_DOMESTIC_SELL_TAX_PCT
)
DEFAULT_COST_PCT = DEFAULT_OVERSEAS_COST_PCT
MIN_REGIME_TRADES = 5
MIN_REGIME_DAYS = 3

_REGIME_LABELS = {
    "strong_down": "급락",
    "down": "하락",
    "sideways": "보합",
    "up": "상승",
    "strong_up": "급등",
    "quiet": "한산",
    "normal": "보통",
    "active": "활발",
    "very_active": "매우활발",
    "calm": "안정",
    "high": "고변동",
    "extreme": "극단변동",
    "unknown": "미확정",
}


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


def _fmt_optional(value: object, *, digits: int = 2, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.{digits}f}{suffix}"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


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
        fallback_expr = _fallback_net_pnl_pct_expr(conn)
        return (
            "CASE "
            f"{overseas_expr} "
            f"{domestic_expr} "
            f"ELSE {fallback_expr} "
            "END"
        )
    return _fallback_net_pnl_pct_expr(conn)


def _fallback_net_pnl_pct_expr(conn: sqlite3.Connection) -> str:
    domestic_cost_expr = str(DEFAULT_DOMESTIC_STOCK_COST_PCT)
    if _has_column(conn, "cycle_log", "product_type"):
        tax_exempt_conditions = " OR ".join(
            f"UPPER(COALESCE(product_type, '')) LIKE '%{marker}%'"
            for marker in ("ETF", "ETN", "ELW")
        )
        domestic_cost_expr = (
            f"CASE WHEN {tax_exempt_conditions} "
            f"THEN {DEFAULT_DOMESTIC_TAX_EXEMPT_COST_PCT} "
            f"ELSE {DEFAULT_DOMESTIC_STOCK_COST_PCT} END"
        )
    return (
        "COALESCE(pnl_pct, 0) - "
        "CASE WHEN LOWER(COALESCE(market, '')) = 'domestic' "
        f"THEN ({domestic_cost_expr}) "
        f"ELSE {DEFAULT_OVERSEAS_COST_PCT} END"
    )


def compare_before_after(db_path: Path | str, cutoff_date: str) -> str:
    """
    Compare session-owned SELL_REAL strategy performance around a KST cutoff.

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
                      AND COALESCE(qty_executed, 0) > 0
                      AND {CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE}
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


def summarize_wait_bottlenecks(
    db_path: Path | str,
    *,
    hours: int = 72,
    limit: int = 12,
) -> str:
    """Summarize WAIT rows by market, strategy, and reason."""
    db_path_obj = Path(db_path)
    lookback_hours = max(1, int(hours or 72))
    row_limit = max(1, int(limit or 12))
    cutoff_utc = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    conn = sqlite3.connect(db_path_obj)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                market,
                COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy,
                COALESCE(NULLIF(action_reason, ''), 'N/A') AS reason,
                COUNT(*) AS cnt,
                AVG(volume_ratio) AS avg_volume_ratio,
                AVG(rsi14) AS avg_rsi14,
                AVG(intraday_momentum) * 100.0 AS avg_momentum_pct
            FROM cycle_log
            WHERE action_bias = 'WAIT'
              AND logged_at >= ?
            GROUP BY market, strategy, reason
            ORDER BY cnt DESC, market ASC, strategy ASC, reason ASC
            LIMIT ?
            """,
            (cutoff_utc, row_limit),
        ).fetchall()
        result = [f"[WAIT 병목] 범위=최근 {lookback_hours}시간"]
        if not rows:
            result.append("  병목=없음")
            return "\n".join(result)
        for row in rows:
            result.append(
                "  "
                f"{str(row['market'] or '-'):<8} "
                f"{str(row['strategy'] or 'N/A'):<12} "
                f"{str(row['reason'] or 'N/A'):<24} "
                f"{int(row['cnt'] or 0):>5}건 "
                f"vr={_fmt_optional(row['avg_volume_ratio'], digits=2)} "
                f"rsi={_fmt_optional(row['avg_rsi14'], digits=1)} "
                f"mom={_fmt_optional(row['avg_momentum_pct'], digits=3, suffix='%')}"
            )
        return "\n".join(result)
    finally:
        conn.close()


def _parse_logged_at(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _regime_key_label(regime_key: str) -> str:
    return "/".join(
        _REGIME_LABELS.get(part, part)
        for part in str(regime_key or "unknown").split("|")
    )


def _minimum_round_trip_cost_pct(market: str) -> float:
    if str(market).strip().lower() == "domestic":
        return DEFAULT_DOMESTIC_TAX_EXEMPT_COST_PCT
    return DEFAULT_OVERSEAS_COST_PCT


def summarize_wait_forward_performance(
    db_path: Path | str,
    *,
    hours: int = 72,
    limit: int = 8,
    market: str = "",
    reason: str = "",
    episode_gap_minutes: int = 5,
    price_tolerance_minutes: int = 5,
    horizons: tuple[int, ...] = (15, 30, 60),
    orderable_env: str = "vps",
    now: datetime | None = None,
) -> str:
    """Measure blocked-entry opportunity cost without counting repeated scans."""
    lookback_hours = max(1, int(hours or 72))
    row_limit = max(1, int(limit or 8))
    gap_minutes = max(1, int(episode_gap_minutes or 5))
    tolerance_minutes = max(1, int(price_tolerance_minutes or 5))
    normalized_market = str(market or "").strip().lower()
    if normalized_market and normalized_market not in {"domestic", "overseas"}:
        raise ValueError("market must be domestic or overseas")
    normalized_reason = str(reason or "").strip()
    normalized_env = str(orderable_env or "vps").strip().lower()
    if normalized_env not in {"vps", "prod"}:
        raise ValueError("orderable_env must be vps or prod")
    horizon_values = tuple(
        sorted({max(1, int(value)) for value in horizons if int(value) > 0})
    )
    if not horizon_values:
        raise ValueError("at least one positive horizon is required")

    analysis_now = now or datetime.now(timezone.utc)
    if analysis_now.tzinfo is None:
        analysis_now = analysis_now.replace(tzinfo=timezone.utc)
    analysis_now = analysis_now.astimezone(timezone.utc)
    cutoff = analysis_now - timedelta(hours=lookback_hours)
    expanded_cutoff = cutoff - timedelta(minutes=gap_minutes)

    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        where = [
            "logged_at >= ?",
            "logged_at <= ?",
            "COALESCE(price, 0) > 0",
        ]
        params: list[object] = [
            expanded_cutoff.isoformat(),
            analysis_now.isoformat(),
        ]
        if normalized_market:
            where.append("LOWER(market) = ?")
            params.append(normalized_market)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    id, logged_at, market, symbol, action_bias,
                    action_reason, price, strategy_flag
                FROM cycle_log
                WHERE {" AND ".join(where)}
                ORDER BY logged_at ASC, id ASC
                """,
                params,
            ).fetchall()
        ]
        parsed_rows: list[dict[str, object]] = []
        for row in rows:
            logged_at = _parse_logged_at(row.get("logged_at"))
            if logged_at is None:
                continue
            market_name = str(row.get("market") or "").strip().lower()
            if market_name == "domestic":
                if not is_krx_regular_session(logged_at):
                    continue
            elif market_name == "overseas":
                if not is_us_orderable_session_for_env(logged_at, normalized_env):
                    continue
            else:
                continue
            row["_logged_at"] = logged_at
            row["_session_date"] = _cycle_session_date(row)
            parsed_rows.append(row)

        regime_map: dict[tuple[str, str], dict[str, object]] = {}
        if _has_table(conn, "market_regimes"):
            regime_map = {
                (
                    str(row["market"] or "").strip().lower(),
                    str(row["session_date"] or ""),
                ): dict(row)
                for row in conn.execute("SELECT * FROM market_regimes").fetchall()
            }

        sequences: dict[
            tuple[str, str, str],
            tuple[list[datetime], list[dict[str, object]]],
        ] = {}
        sequence_rows: dict[
            tuple[str, str, str],
            list[dict[str, object]],
        ] = defaultdict(list)
        session_latest: dict[tuple[str, str], datetime] = {}
        for row in parsed_rows:
            market_name = str(row.get("market") or "").strip().lower()
            symbol = str(row.get("symbol") or "").strip()
            session_date = str(row.get("_session_date") or "")
            logged_at = row.get("_logged_at")
            if not symbol or not session_date or not isinstance(logged_at, datetime):
                continue
            sequence_rows[(market_name, symbol, session_date)].append(row)
            session_key = (market_name, session_date)
            previous_latest = session_latest.get(session_key)
            if previous_latest is None or logged_at > previous_latest:
                session_latest[session_key] = logged_at
        for key, grouped_rows in sequence_rows.items():
            sequences[key] = (
                [
                    row["_logged_at"]
                    for row in grouped_rows
                    if isinstance(row.get("_logged_at"), datetime)
                ],
                grouped_rows,
            )

        buckets: dict[
            tuple[str, str, str, int],
            dict[str, object],
        ] = defaultdict(
            lambda: {
                "raw": 0,
                "episodes": [],
                "dates": set(),
                "symbols": set(),
                "strategies": defaultdict(int),
            }
        )
        last_reason_at: dict[tuple[str, str, str, str], datetime] = {}
        for row in parsed_rows:
            logged_at = row.get("_logged_at")
            if not isinstance(logged_at, datetime):
                continue
            if str(row.get("action_bias") or "").upper() != "WAIT":
                continue
            row_reason = str(row.get("action_reason") or "N/A").strip() or "N/A"
            if normalized_reason and row_reason != normalized_reason:
                continue
            market_name = str(row.get("market") or "").strip().lower()
            symbol = str(row.get("symbol") or "").strip()
            session_date = str(row.get("_session_date") or "")
            episode_key = (market_name, symbol, session_date, row_reason)
            previous = last_reason_at.get(episode_key)
            last_reason_at[episode_key] = logged_at
            if logged_at < cutoff:
                continue
            regime = regime_map.get((market_name, session_date), {})
            regime_key = str(regime.get("regime_key") or "unknown")
            is_final = int(regime.get("is_final") or 0)
            bucket_key = (market_name, row_reason, regime_key, is_final)
            bucket = buckets[bucket_key]
            bucket["raw"] = int(bucket["raw"]) + 1

            if previous is not None and (
                logged_at - previous < timedelta(minutes=gap_minutes)
            ):
                continue
            episodes = bucket["episodes"]
            if isinstance(episodes, list):
                episodes.append(row)
            dates = bucket["dates"]
            if isinstance(dates, set):
                dates.add(session_date)
            symbols = bucket["symbols"]
            if isinstance(symbols, set):
                symbols.add(symbol)
            strategies = bucket["strategies"]
            if isinstance(strategies, defaultdict):
                strategy = str(row.get("strategy_flag") or "N/A").strip() or "N/A"
                strategies[strategy] += 1

        result = [
            (
                f"[WAIT 선행성과] 범위=최근 {lookback_hours}시간 "
                f"주문가능세션={normalized_env} "
                f"에피소드간격={gap_minutes}분 가격허용오차={tolerance_minutes}분"
            ),
            (
                "  정의=동일 시장·종목·사유가 간격 이상 끊긴 뒤 첫 WAIT; "
                "동일 세션 목표시각 직전 관측가"
            ),
        ]
        if not buckets:
            result.append("  표본=없음")
            return "\n".join(result)

        sorted_buckets = sorted(
            buckets.items(),
            key=lambda item: (
                -len(item[1]["episodes"]),
                item[0][0],
                item[0][1],
                item[0][2],
            ),
        )
        for (market_name, row_reason, regime_key, is_final), bucket in sorted_buckets[
            :row_limit
        ]:
            episodes = (
                bucket["episodes"]
                if isinstance(bucket["episodes"], list)
                else []
            )
            dates = bucket["dates"] if isinstance(bucket["dates"], set) else set()
            symbols = (
                bucket["symbols"]
                if isinstance(bucket["symbols"], set)
                else set()
            )
            strategies = bucket["strategies"]
            strategy_text = "-"
            if isinstance(strategies, defaultdict):
                strategy_text = ",".join(
                    f"{name}:{count}"
                    for name, count in sorted(
                        strategies.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:3]
                )
            finality = "확정" if is_final else "임시"
            result.append(
                f"  {market_name:<8} {row_reason:<24} "
                f"{_regime_key_label(regime_key)}·{finality} "
                f"raw={int(bucket['raw'])} ep={len(episodes)} "
                f"{len(symbols)}종목/{len(dates)}일 전략={strategy_text}"
            )
            minimum_cost = _minimum_round_trip_cost_pct(market_name)
            for horizon in horizon_values:
                gross_returns: list[float] = []
                mature = 0
                for episode in episodes:
                    logged_at = episode.get("_logged_at")
                    if not isinstance(logged_at, datetime):
                        continue
                    session_date = str(episode.get("_session_date") or "")
                    session_key = (market_name, session_date)
                    target = logged_at + timedelta(minutes=horizon)
                    if target > session_latest.get(session_key, logged_at):
                        continue
                    mature += 1
                    sequence_key = (
                        market_name,
                        str(episode.get("symbol") or "").strip(),
                        session_date,
                    )
                    times, session_rows = sequences.get(sequence_key, ([], []))
                    index = bisect_right(times, target) - 1
                    if index < 0:
                        continue
                    future_row = session_rows[index]
                    future_at = future_row.get("_logged_at")
                    if not isinstance(future_at, datetime):
                        continue
                    if target - future_at > timedelta(minutes=tolerance_minutes):
                        continue
                    entry_price = float(episode.get("price") or 0.0)
                    future_price = float(future_row.get("price") or 0.0)
                    if entry_price <= 0 or future_price <= 0:
                        continue
                    gross_returns.append(future_price / entry_price - 1.0)
                if not mature:
                    result.append(f"    {horizon:>2}m 표본=성숙대기")
                    continue
                if not gross_returns:
                    result.append(f"    {horizon:>2}m 표본=0/{mature}(관측누락)")
                    continue
                average = statistics.fmean(gross_returns)
                median = statistics.median(gross_returns)
                positive_rate = (
                    sum(value > 0 for value in gross_returns)
                    / len(gross_returns)
                    * 100.0
                )
                coverage = len(gross_returns) / mature * 100.0
                result.append(
                    f"    {horizon:>2}m n={len(gross_returns)}/{mature}"
                    f"({coverage:.0f}%) Gross={average * 100:+.3f}% "
                    f"중앙={median * 100:+.3f}% 양수={positive_rate:.0f}% "
                    f"최소비용Net={(average - minimum_cost) * 100:+.3f}%"
                )
            readiness = (
                "평가가능"
                if is_final and len(dates) >= MIN_REGIME_DAYS
                else f"관찰계속({len(dates)}/{MIN_REGIME_DAYS}일)"
            )
            result.append(f"    정책표본={readiness}")
        result.append(
            "  비용하한=국장 0.03%·미장 0.50% 왕복수수료만; "
            "세금·스프레드·슬리피지 제외한 낙관치"
        )
        result.append(
            "  정책변경조건=확정 동일 레짐 3거래일 이상; "
            "반복행 수만으로 진입필터 완화 금지"
        )
        return "\n".join(result)
    finally:
        conn.close()


def _cycle_session_date(row: dict[str, object]) -> str | None:
    try:
        logged_at = datetime.fromisoformat(
            str(row.get("logged_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if logged_at.tzinfo is None:
        logged_at = logged_at.replace(tzinfo=timezone.utc)
    market = str(row.get("market") or "").strip().lower()
    local_timezone = (
        ZoneInfo("Asia/Seoul")
        if market == "domestic"
        else ZoneInfo("America/New_York")
    )
    return logged_at.astimezone(local_timezone).date().isoformat()


def _recorded_net_pct(row: dict[str, object]) -> float:
    market = str(row.get("market") or "").strip().lower()
    try:
        entry_price = float(row.get("entry_price") or 0.0)
        qty = int(row.get("qty_executed") or 0)
        if entry_price > 0 and qty > 0:
            if market == "domestic" and row.get("net_pnl_krw") is not None:
                return float(row["net_pnl_krw"]) / (entry_price * qty)
            if market == "overseas" and row.get("net_pnl_usd") is not None:
                return float(row["net_pnl_usd"]) / (entry_price * qty)
    except (TypeError, ValueError):
        pass
    try:
        gross = float(row.get("pnl_pct") or 0.0)
        if market == "domestic":
            product_type = str(row.get("product_type") or "")
            cost = (
                DEFAULT_DOMESTIC_TAX_EXEMPT_COST_PCT
                if is_domestic_sell_tax_exempt(product_type)
                else DEFAULT_DOMESTIC_STOCK_COST_PCT
            )
            return gross - cost
        return gross - DEFAULT_OVERSEAS_COST_PCT
    except (TypeError, ValueError):
        return -_minimum_round_trip_cost_pct(market)


def _regime_label(regime: dict[str, object]) -> str:
    trend = _REGIME_LABELS.get(str(regime.get("trend_regime")), "미확정")
    activity = _REGIME_LABELS.get(str(regime.get("activity_regime")), "미확정")
    volatility = _REGIME_LABELS.get(
        str(regime.get("volatility_regime")),
        "미확정",
    )
    return f"{trend}/{activity}/{volatility}"


def summarize_market_regime_performance(
    db_path: Path | str,
    *,
    days: int = 0,
    limit: int = 12,
) -> str:
    """Join final daily benchmark regimes to broker-confirmed SELL_REAL fills."""
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        result = ["[최근 시장 환경]"]
        if not _has_table(conn, "market_regimes"):
            result.append("  환경자료=미수집")
            return "\n".join(result)

        regime_rows = conn.execute(
            """
            SELECT *
            FROM market_regimes
            ORDER BY session_date DESC
            """
        ).fetchall()
        all_regimes = [dict(row) for row in regime_rows]
        final_regimes = [
            row for row in all_regimes if int(row.get("is_final") or 0) == 1
        ]
        for market in ("domestic", "overseas"):
            latest = next(
                (
                    row
                    for row in final_regimes
                    if str(row.get("market") or "").lower() == market
                ),
                None,
            )
            if latest is None:
                result.append(f"  {market:<8} 확정자료=없음")
                continue
            result.append(
                f"  {market:<8} {latest['session_date']} "
                f"{latest['benchmark_name']}={float(latest['close_price'] or 0):,.2f} "
                f"등락={float(latest['return_pct'] or 0):+.2f}% "
                f"거래량20일비={_fmt_optional(latest['volume_ratio_20'], digits=2)} "
                f"레짐={_regime_label(latest)}"
            )

        result.append("[시장 레짐별 세션소유 KIS 체결확정 손익]")
        where = [
            "action_bias = 'SELL_REAL'",
            "COALESCE(qty_executed, 0) > 0",
            CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE,
        ]
        params: list[object] = []
        if days > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
            ).isoformat()
            where.append("logged_at >= ?")
            params.append(cutoff)
        trade_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM cycle_log
                WHERE {" AND ".join(where)}
                ORDER BY logged_at ASC
                """,
                params,
            ).fetchall()
        ]
        regime_map = {
            (str(row["market"]).lower(), str(row["session_date"])): row
            for row in final_regimes
        }
        provisional_regime_keys = {
            (str(row["market"]).lower(), str(row["session_date"]))
            for row in all_regimes
            if int(row.get("is_final") or 0) == 0
        }
        grouped: dict[
            tuple[str, str, str],
            dict[str, object],
        ] = defaultdict(
            lambda: {
                "count": 0,
                "wins": 0,
                "gross_sum": 0.0,
                "net_sum": 0.0,
                "net_krw_sum": 0.0,
                "dates": set(),
            }
        )
        unmatched = 0
        pending_final = 0
        for trade in trade_rows:
            market = str(trade.get("market") or "").strip().lower()
            session_date = _cycle_session_date(trade)
            regime = regime_map.get((market, str(session_date)))
            if regime is None:
                if (market, str(session_date)) in provisional_regime_keys:
                    pending_final += 1
                else:
                    unmatched += 1
                continue
            strategy = str(trade.get("strategy_flag") or "N/A").strip() or "N/A"
            regime_key = str(regime.get("regime_key") or "unknown")
            bucket = grouped[(market, regime_key, strategy)]
            gross = float(trade.get("pnl_pct") or 0.0)
            net = _recorded_net_pct(trade)
            bucket["count"] = int(bucket["count"]) + 1
            bucket["wins"] = int(bucket["wins"]) + int(net > 0)
            bucket["gross_sum"] = float(bucket["gross_sum"]) + gross
            bucket["net_sum"] = float(bucket["net_sum"]) + net
            bucket["net_krw_sum"] = float(bucket["net_krw_sum"]) + float(
                trade.get("net_pnl_krw")
                if trade.get("net_pnl_krw") is not None
                else trade.get("realized_pnl_krw")
                or 0.0
            )
            dates = bucket["dates"]
            if isinstance(dates, set):
                dates.add(session_date)

        if not grouped:
            result.append("  성과=확정 시장환경과 연결된 청산 없음")
        else:
            sorted_groups = sorted(
                grouped.items(),
                key=lambda item: (
                    -int(item[1]["count"]),
                    item[0][0],
                    item[0][1],
                    item[0][2],
                ),
            )
            for (market, regime_key, strategy), bucket in sorted_groups[: max(1, int(limit))]:
                count = int(bucket["count"])
                wins = int(bucket["wins"])
                dates = bucket["dates"] if isinstance(bucket["dates"], set) else set()
                readiness = (
                    "평가가능"
                    if count >= MIN_REGIME_TRADES and len(dates) >= MIN_REGIME_DAYS
                    else f"표본부족({count}/{MIN_REGIME_TRADES}건,{len(dates)}/{MIN_REGIME_DAYS}일)"
                )
                regime_parts = regime_key.split("|")
                regime_text = "/".join(
                    _REGIME_LABELS.get(part, part)
                    for part in regime_parts
                )
                result.append(
                    f"  {market:<8} {strategy:<12} {regime_text:<16} "
                    f"{count:>3}건/{len(dates)}일 승률={wins / count * 100:3.0f}% "
                    f"Gross={float(bucket['gross_sum']) / count * 100:+.3f}% "
                    f"Net={float(bucket['net_sum']) / count * 100:+.3f}% "
                    f"{readiness}"
                )
        if pending_final:
            result.append(
                f"  확정대기={pending_final}건"
                "(임시 지수자료 존재; 확정 전 정책평가 제외)"
            )
        if unmatched:
            result.append(f"  미연결={unmatched}건(해당일 지수자료 자체가 없음)")
        result.append(
            f"  정책변경조건=레짐별 최소 {MIN_REGIME_TRADES}청산/{MIN_REGIME_DAYS}거래일, "
            "비용차감 Net 기준; 단일 장세 결과로 자동변경 금지"
        )
        return "\n".join(result)
    finally:
        conn.close()
