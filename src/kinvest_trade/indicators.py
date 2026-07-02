from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IndicatorSummary:
    rsi14: float | None
    sma5: float | None
    sma20: float | None
    last_close: int | None
    change_pct_from_oldest: float | None
    volume_sum: int
    bar_count: int


def compute_pct_returns(values: list[float]) -> list[float]:
    if len(values) < 2:
        return []

    returns: list[float] = []
    for previous, current in zip(values[:-1], values[1:]):
        if previous == 0:
            continue
        returns.append((current - previous) / previous)
    return returns


def compute_sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[:window]) / window


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for prev, curr in zip(closes[1 : period + 1], closes[:period]):
        delta = curr - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for prev, curr in zip(closes[period + 1 :], closes[period:-1]):
        delta = curr - prev
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_momentum(values: list[float], window: int) -> float | None:
    if len(values) < window + 1:
        return None
    baseline = values[-window - 1]
    latest = values[-1]
    if baseline == 0:
        return None
    return (latest - baseline) / baseline


def compute_volatility(values: list[float], window: int) -> float | None:
    returns = compute_pct_returns(values)
    if len(returns) < window:
        return None

    sample = returns[-window:]
    mean = sum(sample) / len(sample)
    variance = sum((value - mean) ** 2 for value in sample) / len(sample)
    return variance ** 0.5


def compute_stddev(values: list[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return variance ** 0.5


def compute_bollinger_bands(
    values: list[float],
    window: int,
    num_stddev: float = 2.0,
) -> tuple[float | None, float | None, float | None]:
    if len(values) < window:
        return None, None, None
    sample = values[-window:]
    basis = sum(sample) / len(sample)
    stddev = compute_stddev(sample)
    if stddev is None:
        return basis, basis, basis
    upper = basis + (stddev * num_stddev)
    lower = basis - (stddev * num_stddev)
    return basis, upper, lower


def compute_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    window: int,
) -> float | None:
    if len(highs) != len(lows) or len(lows) != len(closes):
        return None
    if len(closes) < window + 1:
        return None

    true_ranges: list[float] = []
    for index in range(1, len(closes)):
        high = highs[index]
        low = lows[index]
        prev_close = closes[index - 1]
        true_ranges.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )

    if len(true_ranges) < window:
        return None
    sample = true_ranges[-window:]
    return sum(sample) / len(sample)


def compute_drawdown(current_price: float, peak_price: float) -> float:
    if peak_price <= 0:
        return 0.0
    return (current_price - peak_price) / peak_price


def compute_ema(values: list[float], period: int) -> list[float]:
    """Exponentially weighted moving average for chronological values."""
    if len(values) < 1:
        return []
    k = 2.0 / (period + 1)
    ema: list[float] = [values[0]]
    for value in values[1:]:
        ema.append(value * k + ema[-1] * (1 - k))
    return ema


def compute_macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, bool, bool]:
    """
    Calculate MACD from chronological values.

    Returns:
        (macd_line, signal_line, golden_cross, dead_cross)
    """
    min_len = slow + signal
    if len(values) < min_len + 1:
        return None, None, False, False

    ema_fast = compute_ema(values, fast)
    ema_slow = compute_ema(values, slow)
    macd_line_series = [fast_value - slow_value for fast_value, slow_value in zip(ema_fast, ema_slow)]
    signal_series = compute_ema(macd_line_series, signal)

    if len(macd_line_series) < 2 or len(signal_series) < 2:
        return None, None, False, False

    macd_cur = macd_line_series[-1]
    macd_prev = macd_line_series[-2]
    sig_cur = signal_series[-1]
    sig_prev = signal_series[-2]

    golden_cross = macd_prev <= sig_prev and macd_cur > sig_cur
    dead_cross = macd_prev >= sig_prev and macd_cur < sig_cur

    return macd_cur, sig_cur, golden_cross, dead_cross


def summarize_indicators(closes: list[int], volumes: list[int]) -> IndicatorSummary:
    closes_float = [float(value) for value in closes]
    last_close = closes[0] if closes else None
    oldest_close = closes[-1] if closes else None
    change_pct = None
    if last_close is not None and oldest_close not in {None, 0}:
        change_pct = (last_close - oldest_close) / oldest_close

    return IndicatorSummary(
        rsi14=compute_rsi(closes_float, 14),
        sma5=compute_sma(closes_float, 5),
        sma20=compute_sma(closes_float, 20),
        last_close=last_close,
        change_pct_from_oldest=change_pct,
        volume_sum=sum(volumes),
        bar_count=len(closes),
    )
