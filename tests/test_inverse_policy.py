from types import SimpleNamespace

from kinvest_trade.inverse_policy import evaluate_inverse_regime


def _config(**overrides):
    values = {
        "inverse_regime_enabled": True,
        "inverse_execution_mode": "shadow",
        "inverse_benchmark_return_threshold_pct": -1.0,
        "inverse_require_same_session": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _regime(**overrides):
    values = {
        "session_date": "2026-07-28",
        "benchmark_name": "KOSPI",
        "return_pct": -2.0,
        "trend_regime": "strong_down",
        "regime_key": "strong_down|active|high",
    }
    values.update(overrides)
    return values


def test_inverse_regime_requires_same_session() -> None:
    decision = evaluate_inverse_regime(
        _config(),
        _regime(session_date="2026-07-27"),
        expected_session_date="2026-07-28",
    )

    assert decision.eligible is False
    assert decision.reason == "inverse_benchmark_regime_stale"


def test_inverse_regime_requires_threshold_decline() -> None:
    decision = evaluate_inverse_regime(
        _config(),
        _regime(return_pct=-0.7, trend_regime="down"),
        expected_session_date="2026-07-28",
    )

    assert decision.eligible is False
    assert decision.reason == "inverse_benchmark_decline_insufficient"


def test_inverse_regime_shadow_is_eligible_but_not_live() -> None:
    decision = evaluate_inverse_regime(
        _config(inverse_execution_mode="shadow"),
        _regime(),
        expected_session_date="2026-07-28",
    )

    assert decision.eligible is True
    assert decision.execution_mode == "shadow"
    assert decision.reason == "inverse_regime_shadow"
