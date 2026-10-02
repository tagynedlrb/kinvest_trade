from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kinvest_trade.config import paper_experiment_breakers_disabled
from kinvest_trade.lab_risk import CircuitBreakerManager
from kinvest_trade.lab_runtime import LabRuntimeManager
from kinvest_trade.liquidity_lab import LiquidityLabService
from kinvest_trade.telegram_reports import ReportHelper


def config(env="vps", enabled=True, live=False):
    return SimpleNamespace(
        credentials=SimpleNamespace(env=env, live_trading_enabled=live),
        risk=SimpleNamespace(paper_experiment_disable_circuit_breakers=enabled,
                             daily_loss_limit_pct=.01, max_consecutive_losses=3,
                             circuit_breaker_cooldown_minutes=30, operating_capital_krw=100000,
                             account_risk_day_rollover_hour_kst=7, order_reject_threshold=3),
    )


@pytest.mark.parametrize("env,enabled,live,expected", [
    ("vps", True, False, True), ("prod", True, False, False),
    ("vps", True, True, False), ("vps", False, False, False),
    ("vps", "true", False, False), ("", True, False, False),
])
def test_override_never_applies_to_live_or_implicit_config(env, enabled, live, expected):
    assert paper_experiment_breakers_disabled(config(env, enabled, live)) is expected
    assert not paper_experiment_breakers_disabled(SimpleNamespace())


def test_override_survives_restored_halts_without_erasing_losses():
    cfg = config()
    cb = CircuitBreakerManager(cfg)
    now = datetime.now(timezone.utc)
    cb.load_state(consecutive_losses=10, consecutive_losses_by_market={"domestic": 10, "overseas": 10},
                  session_realised_krw=-2000, daily_loss_date=cb.current_risk_day(),
                  halted_at=now, halted_at_by_market={"domestic": now, "overseas": now},
                  daily_halted_at=now, last_cb_released_at=now, overseas_cb_active=True,
                  order_reject_history={"overseas:buy": [now] * 5},
                  order_reject_halted_at={"overseas:buy": now})
    assert not cb.is_active
    assert cb.halted_at is None
    assert cb.daily_halted_at is None
    assert not cb.overseas_cb_active
    assert cb.overseas_allowed()
    assert not cb.is_daily_halted()
    for market in ("domestic", "overseas"):
        assert not cb.is_halted(market)
        for _ in range(5):
            assert not cb.record_order_rejection(market=market, side="BUY")
        assert not cb.is_order_reject_halted(market=market, side="buy")
    assert all(not value["halted"] for value in cb.order_reject_status().values())
    cb.on_realised(market="overseas", realized_pnl_krw=-100, pnl_pct=-.01)
    assert cb.snapshot()["consecutive_losses_by_market"]["overseas"] == 11
    assert cb.snapshot()["session_realised_krw"] == -2100
    assert cb.snapshot()["daily_halted_at"] == now
    cfg.risk.paper_experiment_disable_circuit_breakers = False
    assert cb.is_halted("overseas")
    assert cb.is_daily_halted()
    assert cb.is_order_reject_halted(market="overseas", side="buy")


def test_strategy_gates_and_cooldown_are_bypassed_only_in_explicit_paper_mode():
    cfg = config()
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = cfg
    assert service._strategy_guard_blocked_keys() == set()
    for market in ("domestic", "overseas"):
        for flag in ("VWAP", "RSI", "VOL", "MOM", "VWAP+RSI"):
            assert service._entry_strategy_raw_block_reason(market=market, strategy_flag=flag) == ""
        assert service._post_cb_reentry_regime_gate(market)[0] == ""
        assert service._entry_benchmark_reversal_gate(market)[0] == ""
    runtime = LabRuntimeManager(cfg, None, None, is_effective_trade_order=lambda _: False)
    runtime.exit_cooldown["overseas:TEST"] = datetime.now(timezone.utc) + timedelta(hours=2)
    assert runtime.cooldown_remaining_minutes("overseas", "TEST") == 0
    cfg.risk.paper_experiment_disable_circuit_breakers = False
    assert runtime.cooldown_remaining_minutes("overseas", "TEST") > 100


def test_guard_report_states_scope_and_preserved_safety():
    text = ReportHelper(SimpleNamespace(config=config())).build_guard_message()
    assert "별도 지시까지" in text
    assert "중복주문" in text
    assert "일손실" in text
    assert "실계좌에는 적용되지" in text
