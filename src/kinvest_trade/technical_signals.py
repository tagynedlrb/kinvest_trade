from __future__ import annotations

from dataclasses import dataclass

from .indicators import compute_momentum, compute_rsi, compute_sma, compute_volatility


@dataclass(slots=True)
class MovingAverageSnapshot:
    price: float
    spread_pct: float
    daily_ma_fast: float | None
    daily_ma_slow: float | None
    minute_ma_fast: float | None
    minute_ma_slow: float | None
    prev_minute_ma_fast: float | None
    prev_minute_ma_slow: float | None
    rsi14: float | None
    intraday_volatility: float
    intraday_momentum: float
    daily_gap_fast_pct: float
    daily_gap_slow_pct: float
    minute_gap_slow_pct: float
    fast_above_slow: bool
    crossed_up: bool
    crossed_down: bool
    regime: str

    @property
    def has_required_context(self) -> bool:
        return (
            self.daily_ma_fast is not None
            and self.daily_ma_slow is not None
            and self.minute_ma_fast is not None
            and self.minute_ma_slow is not None
        )


def build_moving_average_snapshot(
    *,
    price: float,
    bid: float,
    ask: float,
    daily_closes: list[float],
    minute_closes: list[float],
    daily_fast_window: int,
    daily_slow_window: int,
    intraday_fast_window: int,
    intraday_slow_window: int,
    volatility_window: int,
    momentum_window: int,
) -> MovingAverageSnapshot:
    daily_ma_fast = compute_sma(daily_closes, daily_fast_window)
    daily_ma_slow = compute_sma(daily_closes, daily_slow_window)
    minute_ma_fast = compute_sma(minute_closes, intraday_fast_window)
    minute_ma_slow = compute_sma(minute_closes, intraday_slow_window)
    prev_minute_ma_fast = (
        compute_sma(minute_closes[1:], intraday_fast_window)
        if len(minute_closes) >= intraday_fast_window + 1
        else None
    )
    prev_minute_ma_slow = (
        compute_sma(minute_closes[1:], intraday_slow_window)
        if len(minute_closes) >= intraday_slow_window + 1
        else None
    )
    rsi14 = compute_rsi(minute_closes, 14) if len(minute_closes) >= 15 else None

    minute_chrono = list(reversed(minute_closes))
    intraday_volatility = compute_volatility(
        minute_chrono,
        min(volatility_window, max(len(minute_chrono) - 1, 1)),
    ) or 0.0
    intraday_momentum = compute_momentum(
        minute_chrono,
        min(momentum_window, max(len(minute_chrono) - 1, 1)),
    ) or 0.0

    spread_pct = 0.0
    if bid > 0 and ask > 0:
        mid_price = (bid + ask) / 2
        if mid_price > 0:
            spread_pct = (ask - bid) / mid_price

    fast_above_slow = bool(
        minute_ma_fast is not None
        and minute_ma_slow is not None
        and minute_ma_fast > minute_ma_slow
    )
    crossed_up = bool(
        prev_minute_ma_fast is not None
        and prev_minute_ma_slow is not None
        and minute_ma_fast is not None
        and minute_ma_slow is not None
        and prev_minute_ma_fast <= prev_minute_ma_slow
        and minute_ma_fast > minute_ma_slow
    )
    crossed_down = bool(
        prev_minute_ma_fast is not None
        and prev_minute_ma_slow is not None
        and minute_ma_fast is not None
        and minute_ma_slow is not None
        and prev_minute_ma_fast >= prev_minute_ma_slow
        and minute_ma_fast < minute_ma_slow
    )

    daily_gap_fast_pct = _gap_pct(price, daily_ma_fast)
    daily_gap_slow_pct = _gap_pct(price, daily_ma_slow)
    minute_gap_slow_pct = _gap_pct(price, minute_ma_slow)
    regime = _classify_regime(
        price=price,
        daily_ma_fast=daily_ma_fast,
        daily_ma_slow=daily_ma_slow,
        daily_gap_fast_pct=daily_gap_fast_pct,
        daily_gap_slow_pct=daily_gap_slow_pct,
        fast_above_slow=fast_above_slow,
        crossed_up=crossed_up,
        crossed_down=crossed_down,
    )

    return MovingAverageSnapshot(
        price=price,
        spread_pct=spread_pct,
        daily_ma_fast=daily_ma_fast,
        daily_ma_slow=daily_ma_slow,
        minute_ma_fast=minute_ma_fast,
        minute_ma_slow=minute_ma_slow,
        prev_minute_ma_fast=prev_minute_ma_fast,
        prev_minute_ma_slow=prev_minute_ma_slow,
        rsi14=rsi14,
        intraday_volatility=intraday_volatility,
        intraday_momentum=intraday_momentum,
        daily_gap_fast_pct=daily_gap_fast_pct,
        daily_gap_slow_pct=daily_gap_slow_pct,
        minute_gap_slow_pct=minute_gap_slow_pct,
        fast_above_slow=fast_above_slow,
        crossed_up=crossed_up,
        crossed_down=crossed_down,
        regime=regime,
    )


def format_snapshot_indicator(
    snapshot: MovingAverageSnapshot,
    *,
    daily_fast_label: str,
    daily_slow_label: str,
) -> str:
    parts = [
        "rsi=-" if snapshot.rsi14 is None else f"rsi={snapshot.rsi14:.1f}",
        daily_fast_label + "=-"
        if snapshot.daily_ma_fast is None
        else f"{daily_fast_label}={snapshot.daily_gap_fast_pct * 100:+.2f}%",
        daily_slow_label + "=-"
        if snapshot.daily_ma_slow is None
        else f"{daily_slow_label}={snapshot.daily_gap_slow_pct * 100:+.2f}%",
    ]
    return ", ".join(parts)


def _gap_pct(price: float, moving_average: float | None) -> float:
    if moving_average is None or moving_average <= 0 or price <= 0:
        return 0.0
    return (price - moving_average) / moving_average


def _classify_regime(
    *,
    price: float,
    daily_ma_fast: float | None,
    daily_ma_slow: float | None,
    daily_gap_fast_pct: float,
    daily_gap_slow_pct: float,
    fast_above_slow: bool,
    crossed_up: bool,
    crossed_down: bool,
) -> str:
    if price <= 0 or daily_ma_fast is None or daily_ma_slow is None:
        return "warmup"
    if price >= daily_ma_fast and daily_ma_fast >= daily_ma_slow and fast_above_slow:
        return "trend_up"
    if abs(daily_gap_slow_pct) <= 0.015 and crossed_up:
        return "ma_slow_reclaim"
    if daily_gap_slow_pct <= -0.012:
        return "breakdown"
    if daily_gap_fast_pct <= -0.004 and (crossed_down or not fast_above_slow):
        return "trend_down"
    if crossed_up:
        return "recovery"
    if crossed_down:
        return "pullback"
    return "range"
