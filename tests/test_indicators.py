from kinvest_trade.indicators import (
    compute_drawdown,
    compute_momentum,
    compute_rsi,
    compute_sma,
    compute_volatility,
    summarize_indicators,
)


def test_compute_sma() -> None:
    assert compute_sma([10, 8, 6, 4, 2], 3) == 8


def test_compute_rsi_returns_value() -> None:
    closes = [110, 108, 106, 104, 103, 101, 100, 99, 98, 97, 96, 95, 94, 93, 92]
    value = compute_rsi(closes, 14)
    assert value is not None
    assert 0 <= value <= 100


def test_summarize_indicators() -> None:
    closes = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 99, 98, 97, 96, 95, 94, 93, 92, 91]
    volumes = [1000] * len(closes)
    summary = summarize_indicators(closes, volumes)
    assert summary.last_close == 110
    assert summary.sma5 is not None
    assert summary.sma20 is not None
    assert summary.volume_sum == 20000


def test_compute_momentum_and_volatility() -> None:
    closes = [100.0, 101.0, 102.0, 103.0, 102.5, 104.0]
    momentum = compute_momentum(closes, 3)
    volatility = compute_volatility(closes, 3)
    assert momentum is not None
    assert momentum > 0
    assert volatility is not None
    assert volatility > 0


def test_compute_drawdown() -> None:
    assert compute_drawdown(95.0, 100.0) == -0.05
