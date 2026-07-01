import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from kinvest_trade.auto_trader import FixedSymbolAutoTrader, RealizedBreakdown, StrategySnapshot
from kinvest_trade.config import load_app_config


class DummyRepository:
    def __init__(self) -> None:
        self.heartbeats: list[tuple[str, str]] = []

    def save_heartbeat(self, status: str, message: str) -> None:
        self.heartbeats.append((status, message))


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _build_trader() -> FixedSymbolAutoTrader:
    project_root = Path(__file__).resolve().parents[1]
    base_auto = load_app_config(project_root / "config" / "fixed_config.json").auto_trade
    trader = FixedSymbolAutoTrader.__new__(FixedSymbolAutoTrader)
    trader.config = SimpleNamespace(
        auto_trade=replace(
            base_auto,
            quantity=1,
            max_position_qty=4,
            commission_rate=0.0005,
            sec_fee_rate=0.0000206,
            stop_loss_pct=0.003,
            hard_stop_loss_pct=0.006,
            atr_soft_stop_multiplier=1.2,
            atr_hard_stop_multiplier=1.8,
            atr_trailing_stop_multiplier=1.4,
            soft_stop_volatility_multiplier=1.25,
            hard_stop_volatility_multiplier=2.2,
            take_profit_pct=0.004,
            full_take_profit_pct=0.008,
            trailing_stop_pct=0.002,
            trailing_volatility_multiplier=1.5,
            min_expected_reward_cost_ratio=0.5,
            min_expected_reward_risk_ratio=1.2,
            allow_partial_exit=True,
            max_spread_pct=0.003,
            breakout_entry_pct=0.0,
            bollinger_breakout_buffer_pct=0.0,
            volume_spike_ratio=1.5,
            scale_in_volume_ratio=1.3,
            volume_fade_ratio=0.85,
            min_intraday_momentum_pct=0.001,
            min_bar_return_pct=0.0005,
            max_breakout_extension_pct=0.005,
            partial_exit_rsi14=70.0,
            scale_in_profit_trigger_pct=0.003,
            ma60_entry_buffer_pct=0.012,
            ma20_breakdown_buffer_pct=0.004,
            ma60_hard_stop_buffer_pct=0.01,
            ma20_partial_exit_buffer_pct=0.0025,
            trend_chase_limit_pct=0.02,
            max_entry_rsi14=68.0,
            allow_scale_in=True,
            scale_in_cooldown_cycles=4,
            max_hold_cycles=30,
            force_reentry_after_cycles=3,
        )
    )
    trader.position = SimpleNamespace(
        qty=0,
        avg_price=0.0,
        peak_price=0.0,
        hold_cycles=0,
        partial_exit_count=0,
        last_buy_cycle=0,
    )
    trader.loop_count = 10
    trader.flat_cycles = 0
    trader.last_exit_cycle = 0
    trader.last_available_usd = 0.0
    trader.last_fx_rate_krw = trader.config.auto_trade.usd_krw_fallback_rate
    trader._last_adaptive_override = SimpleNamespace()
    trader.repository = DummyRepository()
    trader.notifier = DummyNotifier()
    return trader


def _snapshot(**overrides) -> StrategySnapshot:
    payload = dict(
        price=225.0,
        spread_pct=0.001,
        daily_ma_fast=224.0,
        daily_ma_slow=220.5,
        minute_ma_fast=225.2,
        minute_ma_slow=224.6,
        prev_minute_ma_fast=224.7,
        prev_minute_ma_slow=224.5,
        rsi14=57.0,
        intraday_volatility=0.001,
        intraday_momentum=0.004,
        intraday_bar_return=0.0021,
        volume_last=250000.0,
        volume_avg=100000.0,
        volume_ratio=2.5,
        breakout_level=224.5,
        breakdown_level=223.2,
        breakout_distance_pct=0.0022,
        atr=0.45,
        atr_pct=0.002,
        bollinger_basis=223.8,
        bollinger_upper=224.7,
        bollinger_lower=222.9,
        daily_gap_fast_pct=0.00446,
        daily_gap_slow_pct=0.02041,
        minute_gap_slow_pct=0.00178,
        fast_above_slow=True,
        crossed_up=False,
        crossed_down=False,
        regime="momentum_breakout",
    )
    payload.update(overrides)
    return StrategySnapshot(**payload)


def test_entry_edge_filter_blocks_trade_when_roundtrip_cost_is_too_large() -> None:
    trader = _build_trader()
    trader.config.auto_trade.commission_rate = 0.0065
    trader.config.auto_trade.min_expected_reward_cost_ratio = 0.5

    allowed = trader._entry_has_sufficient_edge(
        auto=trader.config.auto_trade,
        snapshot=_snapshot(
            price=220.0,
            daily_ma_fast=220.6,
            daily_ma_slow=220.2,
            daily_gap_fast_pct=-0.0027,
            daily_gap_slow_pct=-0.0009,
            atr_pct=0.0018,
            breakout_distance_pct=0.0006,
        ),
        qty=1,
        target_reason="volume_breakout_entry",
    )

    assert not allowed
    assert trader.repository.heartbeats[-1][0] == "EDGE_FAIL_COST"


def test_entry_edge_filter_allows_ma_slow_reclaim_trade_when_reward_is_large_enough() -> None:
    trader = _build_trader()
    trader.config.auto_trade.commission_rate = 0.0005
    trader.config.auto_trade.min_expected_reward_cost_ratio = 0.5

    allowed = trader._entry_has_sufficient_edge(
        auto=trader.config.auto_trade,
        snapshot=_snapshot(
            daily_gap_fast_pct=0.006,
            daily_gap_slow_pct=0.021,
            atr_pct=0.002,
            breakout_distance_pct=0.004,
        ),
        qty=2,
        target_reason="volume_breakout_entry",
    )

    assert allowed


def test_entry_edge_filter_blocks_trade_when_reward_risk_is_too_small() -> None:
    trader = _build_trader()
    trader.config.auto_trade.commission_rate = 0.0005
    trader.config.auto_trade.min_expected_reward_cost_ratio = 0.5
    trader.config.auto_trade.min_expected_reward_risk_ratio = 2.0

    allowed = trader._entry_has_sufficient_edge(
        auto=trader.config.auto_trade,
        snapshot=_snapshot(
            atr_pct=0.0045,
            breakout_distance_pct=0.0002,
        ),
        qty=1,
        target_reason="volume_breakout_entry",
    )

    assert not allowed
    assert trader.repository.heartbeats[-1][0] == "EDGE_FAIL_RISK"


def test_decide_action_prefers_pullback_entry() -> None:
    trader = _build_trader()

    decision = trader._decide_action(
        _snapshot(
            volume_ratio=2.2,
            intraday_bar_return=0.0008,
        )
    )

    assert decision.side == "buy"
    assert decision.reason == "pullback_entry"
    assert decision.qty >= 1


def test_decide_action_sells_on_momentum_loss_cut() -> None:
    trader = _build_trader()
    trader.position = SimpleNamespace(
        qty=3,
        avg_price=224.5,
        peak_price=226.0,
        hold_cycles=12,
        partial_exit_count=0,
        last_buy_cycle=1,
    )

    decision = trader._decide_action(
        _snapshot(
            price=223.0,
            daily_gap_fast_pct=-0.004,
            daily_gap_slow_pct=0.011,
            fast_above_slow=False,
            crossed_up=False,
            crossed_down=True,
            rsi14=41.0,
            intraday_momentum=-0.004,
            intraday_bar_return=-0.002,
            minute_ma_fast=223.5,
            minute_ma_slow=224.0,
            atr_pct=0.004,
            regime="trend_down",
        )
    )

    assert decision.side == "sell"
    assert decision.qty == 3
    assert decision.reason == "momentum_loss_cut"


def test_determine_buy_qty_uses_slot_sizing_when_available_balance_exists() -> None:
    trader = _build_trader()
    trader.last_available_usd = 10_000.0
    trader.config.auto_trade.use_slot_sizing = True
    trader.config.auto_trade.slot_entry_pct = 0.10
    trader.config.auto_trade.slot_scale_in_pct = 0.05
    trader.config.auto_trade.slot_max_pct = 0.20
    trader.config.auto_trade.max_position_qty = 20

    qty = trader._determine_buy_qty(
        auto=trader.config.auto_trade,
        snapshot=_snapshot(price=18.0, volume_ratio=1.2, intraday_momentum=0.001),
        scale_in=False,
        urgent=False,
    )

    assert qty == 11


def test_determine_buy_qty_falls_back_to_fixed_quantity_when_slot_balance_missing() -> None:
    trader = _build_trader()
    trader.last_available_usd = 0.0
    trader.config.auto_trade.use_slot_sizing = True
    trader.config.auto_trade.quantity = 2
    trader.config.auto_trade.max_position_qty = 6

    qty = trader._determine_buy_qty(
        auto=trader.config.auto_trade,
        snapshot=_snapshot(
            price=225.0,
            volume_ratio=trader.config.auto_trade.volume_spike_ratio * 1.6,
            intraday_momentum=trader.config.auto_trade.min_intraday_momentum_pct * 2.2,
        ),
        scale_in=False,
        urgent=True,
    )

    assert qty == 5


def test_sell_fill_message_includes_pnl_usd_and_pct() -> None:
    trader = _build_trader()
    trader.position = SimpleNamespace(avg_price=280.12, hold_cycles=20)
    trader.config.auto_trade.poll_interval_sec = 10

    asyncio.run(
        trader._send_fill_message(
            run_id=42,
            action_count=3,
            side="SELL",
            qty=4,
            price=282.45,
            reason="time_exit_profit",
            realized=RealizedBreakdown(gross_pnl_usd=9.32, net_pnl_usd=9.32, net_pnl_krw=12850.0),
            cumulative_pnl_net_krw=18200.0,
            snapshot=_snapshot(price=282.45),
            captured_at=datetime(2026, 6, 27, 13, 35, 12, tzinfo=timezone.utc),
            avg_price_before_fill=280.12,
            hold_cycles_before_fill=20,
        )
    )

    message = trader.notifier.messages[-1]
    assert "매입가=$280.1200" in message
    assert "손익=+$9.32" in message
    assert "총손익=+$9.32" in message
    assert "수익률=+0.83%" in message
    assert "보유시간=3m20s" in message


def test_sell_fill_message_includes_hold_time() -> None:
    trader = _build_trader()
    trader.config.auto_trade.poll_interval_sec = 10

    asyncio.run(
        trader._send_fill_message(
            run_id=1,
            action_count=1,
            side="SELL",
            qty=1,
            price=100.0,
            reason="time_exit_profit",
            realized=RealizedBreakdown(gross_pnl_usd=1.0, net_pnl_usd=1.0, net_pnl_krw=1000.0),
            cumulative_pnl_net_krw=1000.0,
            snapshot=_snapshot(price=100.0),
            captured_at=datetime(2026, 6, 27, 13, 35, 12, tzinfo=timezone.utc),
            avg_price_before_fill=99.0,
            hold_cycles_before_fill=20,
        )
    )

    assert "보유시간=3m20s" in trader.notifier.messages[-1]


def test_sell_fill_message_avg_price_unknown_when_zero() -> None:
    trader = _build_trader()
    trader.config.auto_trade.poll_interval_sec = 10

    asyncio.run(
        trader._send_fill_message(
            run_id=7,
            action_count=2,
            side="SELL",
            qty=2,
            price=282.45,
            reason="atr_hard_stop",
            realized=RealizedBreakdown(gross_pnl_usd=9.32, net_pnl_usd=8.91, net_pnl_krw=12000.0),
            cumulative_pnl_net_krw=18000.0,
            snapshot=_snapshot(price=282.45),
            captured_at=datetime(2026, 6, 27, 13, 35, 12, tzinfo=timezone.utc),
            avg_price_before_fill=0.0,
            hold_cycles_before_fill=20,
        )
    )

    message = trader.notifier.messages[-1]
    assert "매입가=알수없음" in message
    assert "수익률=알수없음" in message
    assert "손익=+$8.91" in message


def test_sell_fill_message_normal_all_fields() -> None:
    trader = _build_trader()
    trader.config.auto_trade.poll_interval_sec = 10

    asyncio.run(
        trader._send_fill_message(
            run_id=42,
            action_count=3,
            side="SELL",
            qty=4,
            price=282.45,
            reason="trend_filter_lost",
            realized=RealizedBreakdown(gross_pnl_usd=9.32, net_pnl_usd=9.32, net_pnl_krw=12850.0),
            cumulative_pnl_net_krw=18200.0,
            snapshot=_snapshot(price=282.45),
            captured_at=datetime(2026, 6, 27, 14, 41, 22, tzinfo=timezone.utc),
            avg_price_before_fill=280.12,
            hold_cycles_before_fill=20,
        )
    )

    message = trader.notifier.messages[-1]
    assert "매입가=$280.1200" in message
    assert "수익률=+0.83%" in message
    assert "손익=+$9.32" in message
    assert "총손익=+$9.32" in message
    assert "원화손익=12850원" in message
    assert "누적손익=18200원" in message
    assert "보유시간=3m20s" in message


def test_sync_startup_position_records_avg_price_fallback_heartbeat() -> None:
    trader = _build_trader()
    trader.client = SimpleNamespace(
        get_overseas_balance=lambda **kwargs: _async_return(
            {
                "positions": [
                    {
                        "ovrs_pdno": trader.config.auto_trade.symbol,
                        "ovrs_cblc_qty": "3",
                        "pchs_avg_pric": "0",
                    }
                ]
            }
        )
    )

    asyncio.run(trader._sync_startup_position())

    assert trader.position.avg_price == 0.0
    assert trader.repository.heartbeats[-1][0] == "POSITION_AVG_PRICE_FALLBACK"


async def _async_return(value):
    return value
