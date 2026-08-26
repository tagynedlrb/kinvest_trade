from types import SimpleNamespace

from kinvest_trade.market_policy import get_market_strategy_guard_policy


def _auto_trade(**overrides):
    values = {
        "commission_rate": 0.0025,
        "domestic_commission_rate": 0.00015,
        "overseas_commission_rate": 0.0025,
        "domestic_sell_tax_rate": 0.002,
        "sec_fee_rate": 0.0000206,
        "strategy_guard_lookback_hours": 48,
        "strategy_guard_min_trades": 3,
        "strategy_guard_max_avg_net_pnl_pct": -0.003,
        "strategy_guard_max_capital_weighted_net_pnl_pct": -0.003,
        "strategy_guard_strategy_flags": ["VWAP", "RSI", "VOL"],
        "strategy_guard_min_final_sessions": 3,
        "strategy_guard_release_requires_recovery": False,
        "strategy_guard_release_min_trades": 3,
        "strategy_guard_release_min_avg_net_pnl_pct": 0.0,
        "strategy_guard_release_min_capital_weighted_net_pnl_pct": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_strategy_guard_policy_is_resolved_from_each_market_definition() -> None:
    domestic = _auto_trade(
        strategy_guard_lookback_hours=24,
        strategy_guard_min_trades=4,
        strategy_guard_max_avg_net_pnl_pct=-0.004,
        strategy_guard_max_capital_weighted_net_pnl_pct=-0.002,
        strategy_guard_strategy_flags=["VWAP"],
        strategy_guard_min_final_sessions=2,
    )
    overseas = _auto_trade(
        strategy_guard_lookback_hours=72,
        strategy_guard_min_trades=5,
        strategy_guard_max_avg_net_pnl_pct=-0.006,
        strategy_guard_max_capital_weighted_net_pnl_pct=-0.001,
        strategy_guard_strategy_flags=["VWAP+RSI"],
        strategy_guard_min_final_sessions=4,
    )
    config = SimpleNamespace(
        market_policies=SimpleNamespace(
            domestic=SimpleNamespace(auto_trade=domestic),
            overseas=SimpleNamespace(auto_trade=overseas),
        ),
        liquidity_lab=SimpleNamespace(
            strategy_guard_lookback_hours=999,
            strategy_guard_min_trades=99,
            strategy_guard_max_avg_net_pnl_pct=-0.99,
            strategy_guard_strategy_flags=["SHARED"],
        ),
        auto_trade=_auto_trade(),
    )

    domestic_guard = get_market_strategy_guard_policy(config, "domestic")
    overseas_guard = get_market_strategy_guard_policy(config, "overseas")

    assert domestic_guard.lookback_hours == 24
    assert domestic_guard.min_trades == 4
    assert domestic_guard.max_avg_net_pnl_pct == -0.004
    assert domestic_guard.max_capital_weighted_net_pnl_pct == -0.002
    assert domestic_guard.strategy_flags == frozenset({"VWAP"})
    assert domestic_guard.min_final_sessions == 2
    assert domestic_guard.release_requires_recovery is False
    assert domestic_guard.release_min_trades == 3
    assert domestic_guard.release_min_avg_net_pnl_pct == 0.0
    assert domestic_guard.release_min_capital_weighted_net_pnl_pct == 0.0
    assert domestic_guard.fallback_cost_pct == 0.0023

    assert overseas_guard.lookback_hours == 72
    assert overseas_guard.min_trades == 5
    assert overseas_guard.max_avg_net_pnl_pct == -0.006
    assert overseas_guard.max_capital_weighted_net_pnl_pct == -0.001
    assert overseas_guard.strategy_flags == frozenset({"VWAP+RSI"})
    assert overseas_guard.min_final_sessions == 4
    assert overseas_guard.release_requires_recovery is False
    assert overseas_guard.fallback_cost_pct == 0.0050206


def test_strategy_guard_policy_keeps_legacy_config_fallback() -> None:
    config = SimpleNamespace(
        liquidity_lab=SimpleNamespace(
            strategy_guard_lookback_hours=36,
            strategy_guard_min_trades=7,
            strategy_guard_max_avg_net_pnl_pct=-0.009,
            strategy_guard_strategy_flags=["RSI"],
        ),
        auto_trade=_auto_trade(strategy_guard_min_final_sessions=6),
    )

    guard = get_market_strategy_guard_policy(config, "overseas")

    assert guard.lookback_hours == 36
    assert guard.min_trades == 7
    assert guard.max_avg_net_pnl_pct == -0.009
    assert guard.max_capital_weighted_net_pnl_pct == -0.009
    assert guard.strategy_flags == frozenset({"RSI"})
    assert guard.min_final_sessions == 6
    assert guard.release_requires_recovery is False
