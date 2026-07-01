from __future__ import annotations

from dataclasses import dataclass

from .indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_momentum,
    compute_rsi,
    compute_sma,
    compute_volatility,
)


@dataclass(slots=True)
class PriceSeries:
    closes: list[float]
    highs: list[float]
    lows: list[float]
    volumes: list[float]


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
    intraday_bar_return: float
    volume_last: float
    volume_avg: float
    volume_ratio: float
    breakout_level: float | None
    breakdown_level: float | None
    breakout_distance_pct: float
    atr: float
    atr_pct: float
    bollinger_basis: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
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
            and self.minute_ma_fast is not None
        )

    @property
    def daily_trend_up(self) -> bool:
        return bool(
            self.daily_ma_fast is not None
            and self.daily_ma_slow is not None
            and self.daily_ma_fast >= self.daily_ma_slow
            and self.price >= self.daily_ma_slow
        )

    @property
    def intraday_trend_up(self) -> bool:
        return bool(
            self.minute_ma_fast is not None
            and self.minute_ma_slow is not None
            and self.minute_ma_fast >= self.minute_ma_slow
            and self.price >= self.minute_ma_slow
        )


def build_moving_average_snapshot(
    *,
    price: float,
    bid: float,
    ask: float,
    daily_closes: list[float],
    minute_closes: list[float],
    minute_highs: list[float],
    minute_lows: list[float],
    minute_volumes: list[float],
    daily_fast_window: int,
    daily_slow_window: int,
    intraday_fast_window: int,
    intraday_slow_window: int,
    volatility_window: int,
    momentum_window: int,
    volume_window: int,
    breakout_lookback_bars: int,
    bollinger_window: int,
    bollinger_stddev: float,
    atr_window: int,
    bar_duration_sec: int = 300,
    chart_elapsed_sec: int = 0,
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
    highs_chrono = list(reversed(minute_highs))
    lows_chrono = list(reversed(minute_lows))
    intraday_volatility = compute_volatility(
        minute_chrono,
        min(volatility_window, max(len(minute_chrono) - 1, 1)),
    ) or 0.0
    intraday_momentum = compute_momentum(
        minute_chrono,
        min(momentum_window, max(len(minute_chrono) - 1, 1)),
    ) or 0.0
    intraday_bar_return = compute_momentum(minute_chrono, 1) or 0.0

    volume_last = minute_volumes[0] if minute_volumes else 0.0
    volume_avg = 0.0
    if len(minute_volumes) > 1:
        baseline_window = max(5, min(max(volume_window, 5), len(minute_volumes) - 1))
        baseline = minute_volumes[1 : baseline_window + 1]
        if baseline:
            volume_avg = sum(baseline) / len(baseline)
    volume_ratio = volume_last / volume_avg if volume_avg > 0 else 0.0
    if chart_elapsed_sec > 0 and bar_duration_sec > 0:
        elapsed_ratio = min(chart_elapsed_sec / bar_duration_sec, 1.0)
        if elapsed_ratio > 0.1:
            adjustment = min(1.0 / elapsed_ratio, 3.0)
            volume_ratio *= adjustment

    breakout_level = None
    breakdown_level = None
    lookback = min(max(int(breakout_lookback_bars), 1), max(len(minute_highs) - 1, 1))
    prior_highs = minute_highs[1 : lookback + 1]
    prior_lows = minute_lows[1 : lookback + 1]
    if prior_highs:
        breakout_level = max(prior_highs)
    if prior_lows:
        breakdown_level = min(prior_lows)
    breakout_distance_pct = _gap_pct(price, breakout_level)

    bollinger_basis, bollinger_upper, bollinger_lower = compute_bollinger_bands(
        minute_chrono,
        window=max(2, min(bollinger_window, len(minute_chrono))),
        num_stddev=bollinger_stddev,
    )
    atr = compute_atr(
        highs_chrono,
        lows_chrono,
        minute_chrono,
        window=max(2, min(atr_window, max(len(minute_chrono) - 1, 1))),
    ) or 0.0
    atr_pct = (atr / price) if atr > 0 and price > 0 else 0.0

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
        volume_ratio=volume_ratio,
        intraday_momentum=intraday_momentum,
        breakout_distance_pct=breakout_distance_pct,
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
        intraday_bar_return=intraday_bar_return,
        volume_last=volume_last,
        volume_avg=volume_avg,
        volume_ratio=volume_ratio,
        breakout_level=breakout_level,
        breakdown_level=breakdown_level,
        breakout_distance_pct=breakout_distance_pct,
        atr=atr,
        atr_pct=atr_pct,
        bollinger_basis=bollinger_basis,
        bollinger_upper=bollinger_upper,
        bollinger_lower=bollinger_lower,
        daily_gap_fast_pct=daily_gap_fast_pct,
        daily_gap_slow_pct=daily_gap_slow_pct,
        minute_gap_slow_pct=minute_gap_slow_pct,
        fast_above_slow=fast_above_slow,
        crossed_up=crossed_up,
        crossed_down=crossed_down,
        regime=regime,
    )

def extract_price_series(
    rows: list[dict],
    *,
    close_fields: tuple[str, ...],
    high_fields: tuple[str, ...] = (),
    low_fields: tuple[str, ...] = (),
    volume_fields: tuple[str, ...] = (),
) -> PriceSeries:
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    for row in rows:
        close = _first_positive_value(row, close_fields)
        if close <= 0:
            continue
        high = _first_positive_value(row, high_fields) if high_fields else close
        low = _first_positive_value(row, low_fields) if low_fields else close
        volume = _first_non_negative_value(row, volume_fields) if volume_fields else 0.0
        closes.append(close)
        highs.append(high if high > 0 else close)
        lows.append(low if low > 0 else close)
        volumes.append(volume)
    return PriceSeries(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
    )


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
    volume_ratio: float,
    intraday_momentum: float,
    breakout_distance_pct: float,
    fast_above_slow: bool,
    crossed_up: bool,
    crossed_down: bool,
) -> str:
    if price <= 0 or daily_ma_fast is None or daily_ma_slow is None:
        return "warmup"
    if (
        price >= daily_ma_fast
        and daily_ma_fast >= daily_ma_slow
        and fast_above_slow
        and volume_ratio >= 1.5
        and breakout_distance_pct >= 0
    ):
        return "momentum_breakout"
    if price >= daily_ma_fast and daily_ma_fast >= daily_ma_slow and fast_above_slow:
        return "trend_up"
    if volume_ratio >= 1.5 and intraday_momentum > 0 and breakout_distance_pct > -0.002:
        return "momentum_setup"
    if daily_gap_slow_pct <= -0.012:
        return "breakdown"
    if daily_gap_fast_pct <= -0.004 and (crossed_down or not fast_above_slow):
        return "trend_down"
    if breakout_distance_pct >= 0:
        return "breakout_test"
    if crossed_up:
        return "recovery"
    if crossed_down:
        return "pullback"
    return "range"


def _first_positive_value(row: dict, field_names: tuple[str, ...]) -> float:
    for field_name in field_names:
        value = _coerce_float(row.get(field_name))
        if value > 0:
            return value
    return 0.0


def _first_non_negative_value(row: dict, field_names: tuple[str, ...]) -> float:
    for field_name in field_names:
        raw = row.get(field_name)
        if raw is None:
            continue
        value = _coerce_float(raw)
        if value >= 0:
            return value
    return 0.0


def _coerce_float(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)
