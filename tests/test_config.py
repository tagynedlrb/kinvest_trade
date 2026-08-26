import json
from pathlib import Path

import pytest

from kinvest_trade.config import (
    _load_market_policies,
    _normalize_kis_env,
    _split_account_fields,
    load_app_config,
)


def test_split_account_fields_with_10_digits() -> None:
    account_no, product_code = _split_account_fields("1234567801", "")
    assert account_no == "12345678"
    assert product_code == "01"


def test_split_account_fields_with_explicit_product_code() -> None:
    account_no, product_code = _split_account_fields("12345678", "22")
    assert account_no == "12345678"
    assert product_code == "22"


def test_split_account_fields_with_hyphenated_account() -> None:
    account_no, product_code = _split_account_fields("12345678-01", "")
    assert account_no == "12345678"
    assert product_code == "01"


def test_split_account_fields_with_8_digits_defaults_to_01() -> None:
    account_no, product_code = _split_account_fields("12345678", "")
    assert account_no == "12345678"
    assert product_code == "01"


def test_normalize_kis_env_aliases() -> None:
    assert _normalize_kis_env("prod") == "prod"
    assert _normalize_kis_env("live") == "prod"
    assert _normalize_kis_env("mock") == "vps"
    assert _normalize_kis_env("paper") == "vps"


def test_load_app_config_uses_paper_profile_variables(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")
    monkeypatch.delenv("KIS_VPS_ACCOUNT_PRODUCT_CODE", raising=False)

    config = load_app_config()

    assert config.credentials.env == "vps"
    assert config.credentials.profile_name == "paper"
    assert config.credentials.appkey == "paper-key"
    assert config.credentials.appsecret == "paper-secret"
    assert config.credentials.account_no == "87654321"
    assert config.credentials.account_product_code == "01"
    assert config.auto_trade.max_position_qty >= config.auto_trade.quantity
    assert config.auto_trade.max_decision_cycles_per_run >= 0
    assert config.auto_trade.max_actions_per_run >= 0
    assert config.auto_trade.capital_gains_tax_rate == 0.22
    assert config.auto_trade.daily_fast_window < config.auto_trade.daily_slow_window
    assert config.auto_trade.intraday_fast_window < config.auto_trade.intraday_slow_window
    assert config.auto_trade.min_hold_before_trend_exit == 30
    assert (
        config.auto_trade.dynamic_pool_approved_leveraged_symbols
        == config.auto_trade.leveraged_etf_symbols
    )
    assert len(config.liquidity_lab.domestic_candidates) >= 1
    assert len(config.liquidity_lab.overseas_candidates) == 0
    assert config.notifications.telegram_command_poll_timeout_sec > 0
    assert config.liquidity_lab.loop_interval_sec > 0
    assert config.paper.max_spread_pct == 0.003
    assert config.paper.trailing_stop_pct == 0.004
    assert config.liquidity_lab.overseas_block_standalone_vwap is True
    assert config.liquidity_lab.overseas_block_standalone_rsi is True
    assert config.liquidity_lab.overseas_block_standalone_vol is True
    assert config.liquidity_lab.overseas_min_strategy_volume_ratio == 0.8
    assert config.liquidity_lab.overseas_signal_failure_threshold == 3
    assert config.liquidity_lab.overseas_signal_failure_cooldown_minutes == 180
    assert config.liquidity_lab.max_concurrent_overseas_orders == 10
    assert config.liquidity_lab.max_concurrent_domestic_orders == 10
    assert config.liquidity_lab.max_concurrent_total_positions == 10
    assert config.liquidity_lab.overseas_stop_loss_confirm_enabled is True
    assert config.liquidity_lab.overseas_stop_loss_hard_multiplier == 2.0
    assert config.liquidity_lab.overseas_stop_loss_volume_confirm_ratio == 1.5
    assert config.liquidity_lab.overseas_stop_loss_confirm_max_age_sec == 600
    assert config.liquidity_lab.overseas_exit_mid_mismatch_pct == 0.03
    assert config.liquidity_lab.overseas_exit_price_shock_pct == 0.20
    assert config.liquidity_lab.overseas_exit_price_shock_confirm_pct == 0.02
    assert config.liquidity_lab.overseas_exit_price_shock_min_volume_ratio == 0.5
    assert config.liquidity_lab.overseas_exit_price_shock_min_bar_volume == 10
    assert config.liquidity_lab.strategy_guard_enabled is True
    assert config.liquidity_lab.strategy_guard_lookback_hours == 48
    assert config.liquidity_lab.strategy_guard_min_trades == 3
    assert config.liquidity_lab.strategy_guard_max_avg_net_pnl_pct == -0.003
    assert config.liquidity_lab.strategy_guard_markets == ["domestic", "overseas"]
    assert config.liquidity_lab.strategy_guard_strategy_flags == ["VWAP", "RSI", "VOL"]
    assert (
        config.liquidity_lab.tv_min_price_usd
        == config.liquidity_lab.overseas_min_price_usd
    )


def test_load_app_config_includes_circuit_breaker_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")
    monkeypatch.delenv("KIS_VPS_ACCOUNT_PRODUCT_CODE", raising=False)

    config = load_app_config()

    assert config.risk.circuit_breaker_cooldown_minutes == 30
    assert config.risk.operating_capital_krw == 50_000_000
    assert config.risk.account_risk_day_rollover_hour_kst == 7


def test_market_policies_clone_baseline_and_remain_independent(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")

    config = load_app_config()
    assert config.market_policies is not None
    domestic = config.market_policies.domestic
    overseas = config.market_policies.overseas

    assert domestic.policy_id == "domestic_momentum_v4"
    assert overseas.policy_id == "overseas_momentum_v2"
    assert domestic.auto_trade.strategy_guard_min_final_sessions == 3
    assert overseas.auto_trade.strategy_guard_min_final_sessions == 3
    assert domestic.auto_trade.stale_order_cancel_minutes == 30
    assert domestic.auto_trade.close_guard_cancel_window_minutes == 15
    assert domestic.auto_trade.close_guard_min_order_age_minutes == 5
    assert domestic.auto_trade.close_guard_poll_interval_minutes == 1
    assert overseas.auto_trade.stale_order_cancel_minutes == 30
    assert overseas.auto_trade.close_guard_cancel_window_minutes == 0
    assert domestic.post_cb_reentry_benchmark_floor_pct == -3.0
    assert overseas.post_cb_reentry_benchmark_floor_pct == -1.0
    assert domestic.entry_require_same_session_regime is True
    assert overseas.entry_require_same_session_regime is True
    assert domestic.entry_regime_max_age_sec == 600
    assert overseas.entry_regime_max_age_sec == 600
    assert domestic.entry_benchmark_floor_pct == 0.0
    assert overseas.entry_benchmark_floor_pct is None
    assert domestic.post_cb_reentry_regime_max_age_sec == 600
    assert overseas.post_cb_reentry_regime_max_age_sec == 600
    assert domestic.post_cb_max_fires_per_session == 1
    assert overseas.post_cb_max_fires_per_session == 1
    assert domestic.inverse_require_symbol_benchmark is True
    assert domestic.inverse_benchmarks["114800"].market == "domestic"
    assert domestic.inverse_benchmarks["114800"].benchmark_code == "101000"
    assert (
        domestic.inverse_benchmarks["114800"].instrument_type
        == "domestic_futures_continuous"
    )
    assert domestic.inverse_benchmarks["252670"].benchmark_name == "F-KOSPI200"
    assert overseas.inverse_require_symbol_benchmark is True
    assert overseas.inverse_benchmarks["SQQQ"].market == "overseas"
    assert overseas.inverse_benchmarks["SQQQ"].benchmark_code == "NDX"
    assert (
        overseas.inverse_benchmarks["SQQQ"].instrument_type
        == "overseas_index"
    )
    assert overseas.inverse_benchmarks["SQQQ"].benchmark_name == "NASDAQ-100"
    assert overseas.inverse_benchmarks["SPXU"].benchmark_code == "SPX"
    assert overseas.inverse_benchmarks["SPXU"].benchmark_name == "S&P 500"
    assert overseas.inverse_benchmarks["SOXS"].available is False
    assert (
        overseas.inverse_benchmarks["SOXS"].unavailable_reason
        == "inverse_exact_benchmark_unavailable"
    )
    assert domestic.corporate_actions == {}
    assert domestic.corporate_actions is not overseas.corporate_actions
    cprx_action = overseas.corporate_actions["CPRX"]
    assert cprx_action.market == "overseas"
    assert cprx_action.symbol == "CPRX"
    assert cprx_action.action_type == "cash_merger"
    assert cprx_action.effective_date == "2026-07-15"
    assert cprx_action.last_trading_date == "2026-07-14"
    assert cprx_action.cash_consideration == 31.50
    assert cprx_action.currency == "USD"
    assert cprx_action.status == "effective"
    assert len(cprx_action.reference_urls) == 2
    assert domestic.auto_trade.strategy_guard_lookback_hours == 336
    assert overseas.auto_trade.strategy_guard_lookback_hours == 336
    assert domestic.auto_trade.strategy_guard_min_trades == 3
    assert overseas.auto_trade.strategy_guard_min_trades == 3
    assert domestic.auto_trade.strategy_guard_max_avg_net_pnl_pct == -0.003
    assert overseas.auto_trade.strategy_guard_max_avg_net_pnl_pct == -0.0025
    assert (
        domestic.auto_trade.strategy_guard_max_capital_weighted_net_pnl_pct
        == -0.003
    )
    assert (
        overseas.auto_trade.strategy_guard_max_capital_weighted_net_pnl_pct
        == -0.001
    )
    assert domestic.auto_trade.strategy_guard_strategy_flags == [
        "VWAP",
        "RSI",
        "VOL",
        "VWAP+VOL",
        "VOL+RSI",
        "VWAP+RSI",
        "VWAP+VOL+RSI",
    ]
    assert overseas.auto_trade.strategy_guard_strategy_flags == [
        "VWAP",
        "RSI",
        "VOL",
        "VWAP+VOL",
        "VOL+RSI",
        "VWAP+RSI",
        "VWAP+VOL+RSI",
    ]
    assert domestic.auto_trade.strategy_guard_probe_enabled is False
    assert domestic.auto_trade.strategy_guard_probe_strategy_flags == []
    assert domestic.auto_trade.strategy_guard_probe_max_entries_per_session == 0
    assert domestic.auto_trade.strategy_guard_probe_max_submissions_per_session == 0
    assert overseas.auto_trade.strategy_guard_probe_enabled is True
    assert overseas.auto_trade.strategy_guard_probe_strategy_flags == [
        "VWAP+RSI",
        "VOL+RSI",
        "VWAP+VOL",
        "VWAP+VOL+RSI",
    ]
    assert overseas.auto_trade.strategy_guard_probe_max_entries_per_session == 3
    assert overseas.auto_trade.strategy_guard_probe_max_submissions_per_session == 6
    assert domestic.auto_trade.virtual_settlement_aggressive_after_sessions == 2
    assert overseas.auto_trade.virtual_settlement_aggressive_after_sessions == 2
    assert domestic.auto_trade.virtual_settlement_aggressive_limit_bps == 50
    assert overseas.auto_trade.virtual_settlement_aggressive_limit_bps == 50
    assert overseas.auto_trade.strategy_guard_probe_slot_multiplier == 0.10
    assert overseas.auto_trade.strategy_guard_probe_benchmark_floor_pct == 0.0
    assert overseas.auto_trade.strategy_guard_probe_regime_max_age_sec == 600
    assert domestic.auto_trade.strategy_guard_release_requires_recovery is False
    assert overseas.auto_trade.strategy_guard_release_requires_recovery is True
    assert domestic.auto_trade.strategy_guard_release_min_trades == 3
    assert overseas.auto_trade.strategy_guard_release_min_trades == 3
    assert domestic.auto_trade.strategy_guard_release_min_avg_net_pnl_pct == 0.0
    assert overseas.auto_trade.strategy_guard_release_min_avg_net_pnl_pct == 0.0
    assert domestic.auto_trade.virtual_settlement_stale_order_minutes == 5
    assert overseas.auto_trade.virtual_settlement_stale_order_minutes == 5
    assert domestic.auto_trade.virtual_settlement_retry_cooldown_minutes == 15
    assert overseas.auto_trade.virtual_settlement_retry_cooldown_minutes == 15
    assert domestic.auto_trade.virtual_settlement_max_submissions_per_session == 3
    assert overseas.auto_trade.virtual_settlement_max_submissions_per_session == 3
    assert domestic.auto_trade.post_fill_stale_balance_minutes == 10
    assert overseas.auto_trade.post_fill_stale_balance_minutes == 30
    assert domestic.auto_trade.entry_confirmation_strategy_flags == ["VWAP"]
    assert overseas.auto_trade.entry_confirmation_strategy_flags == []
    assert domestic.auto_trade.dynamic_pool_approved_leveraged_symbols == [
        "122630"
    ]
    assert overseas.auto_trade.dynamic_pool_approved_leveraged_symbols == []
    assert domestic.engine == overseas.engine == "momentum_v1"
    assert domestic.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert overseas.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert domestic.auto_trade.inverse_etf_symbols == ["114800", "252670"]
    assert overseas.auto_trade.inverse_etf_symbols == ["SQQQ", "SOXS", "SPXU"]
    assert domestic.auto_trade.leveraged_etf_symbols == ["122630", "233740"]
    assert overseas.auto_trade.leveraged_etf_symbols == ["TQQQ", "SOXL"]
    assert domestic.auto_trade.leveraged_require_dual_trend_confirmation is True
    assert overseas.auto_trade.leveraged_require_dual_trend_confirmation is False
    assert domestic.auto_trade.inverse_execution_mode == "shadow"
    assert overseas.auto_trade.inverse_execution_mode == "shadow"
    assert (
        domestic.auto_trade.inverse_entry_formula
        == "regime_trend_breakout_v1"
    )
    assert (
        overseas.auto_trade.inverse_entry_formula
        == "us_regime_trend_breakout_v1"
    )
    assert (
        overseas.auto_trade.inverse_trend_breakout_benchmark_threshold_pct
        == -1.0
    )
    assert overseas.auto_trade.inverse_trend_breakout_min_volume_ratio == 1.3
    assert domestic.auto_trade.inverse_require_nav_validation is True
    assert overseas.auto_trade.inverse_require_nav_validation is False
    assert domestic.auto_trade.domestic_sell_tax_rate == 0.002
    assert overseas.auto_trade.domestic_sell_tax_rate == 0.0
    assert domestic.auto_trade is not overseas.auto_trade
    assert domestic.auto_trade.inverse_etf_symbols is not overseas.auto_trade.inverse_etf_symbols
    assert (
        domestic.auto_trade.strategy_guard_strategy_flags
        is not overseas.auto_trade.strategy_guard_strategy_flags
    )
    assert (
        domestic.auto_trade.entry_confirmation_strategy_flags
        is not overseas.auto_trade.entry_confirmation_strategy_flags
    )
    assert (
        domestic.auto_trade.dynamic_pool_approved_leveraged_symbols
        is not overseas.auto_trade.dynamic_pool_approved_leveraged_symbols
    )

    domestic.auto_trade.take_profit_pct = 0.123
    domestic.auto_trade.inverse_etf_symbols.append("KRX_TEST")
    domestic.auto_trade.strategy_guard_strategy_flags.append("KRX_TEST")
    domestic.auto_trade.strategy_guard_probe_strategy_flags.append("KRX_PROBE")
    domestic.auto_trade.entry_confirmation_strategy_flags.append("KRX_TEST")
    domestic.auto_trade.dynamic_pool_approved_leveraged_symbols.append("KRX_TEST")

    assert overseas.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert "KRX_TEST" not in overseas.auto_trade.inverse_etf_symbols
    assert "KRX_TEST" not in overseas.auto_trade.strategy_guard_strategy_flags
    assert "KRX_PROBE" not in overseas.auto_trade.strategy_guard_probe_strategy_flags
    assert "KRX_TEST" not in overseas.auto_trade.entry_confirmation_strategy_flags
    assert (
        "KRX_TEST"
        not in overseas.auto_trade.dynamic_pool_approved_leveraged_symbols
    )
    assert domestic.source_path is not None
    assert domestic.source_path.name == "domestic.json"
    assert overseas.source_path is not None
    assert overseas.source_path.name == "overseas.json"


@pytest.mark.parametrize(
    "action",
    [
        {
            "action_type": "cash_merger",
            "effective_date": "2026-07-15",
            "last_trading_date": "2026-07-14",
            "cash_consideration": 31.50,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["https://example.com/action"],
            "unexpected": True,
        },
        {
            "action_type": "cash_merger",
            "effective_date": "2026/07/15",
            "last_trading_date": "2026-07-14",
            "cash_consideration": 31.50,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["https://example.com/action"],
        },
        {
            "action_type": "cash_merger",
            "effective_date": "2026-07-15",
            "last_trading_date": "2026-07-16",
            "cash_consideration": 31.50,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["https://example.com/action"],
        },
        {
            "action_type": "cash_merger",
            "effective_date": "2026-07-15",
            "last_trading_date": "2026-07-14",
            "cash_consideration": 0,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["https://example.com/action"],
        },
        {
            "action_type": "cash_merger",
            "effective_date": "2026-07-15",
            "last_trading_date": "2026-07-14",
            "cash_consideration": 31.50,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["file:///tmp/action"],
        },
        {
            "action_type": "stock_split",
            "effective_date": "2026-07-15",
            "last_trading_date": "2026-07-14",
            "cash_consideration": 31.50,
            "currency": "USD",
            "status": "effective",
            "reference_urls": ["https://example.com/action"],
        },
    ],
)
def test_market_policy_rejects_invalid_corporate_actions(
    monkeypatch,
    action,
) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")
    config = load_app_config()

    with pytest.raises(ValueError):
        _load_market_policies(
            project_root=Path(__file__).resolve().parents[1],
            raw_definitions={
                "overseas": {
                    "corporate_actions": {
                        "CPRX": action,
                    }
                }
            },
            base_auto_trade=config.auto_trade,
            base_risk=config.risk,
        )


def test_market_policy_fallback_preserves_legacy_leveraged_pool_approval(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")
    config = load_app_config()

    policies = _load_market_policies(
        project_root=Path(__file__).resolve().parents[1],
        raw_definitions={},
        base_auto_trade=config.auto_trade,
        base_risk=config.risk,
    )

    expected = config.auto_trade.leveraged_etf_symbols
    assert policies.domestic.auto_trade.dynamic_pool_approved_leveraged_symbols == expected
    assert policies.overseas.auto_trade.dynamic_pool_approved_leveraged_symbols == expected
    assert (
        policies.domestic.auto_trade.dynamic_pool_approved_leveraged_symbols
        is not policies.overseas.auto_trade.dynamic_pool_approved_leveraged_symbols
    )


def test_fixed_config_risk_section_contains_only_live_keys() -> None:
    with open(
        "/home/ubuntu/kinvest_trade/config/fixed_config.json",
        encoding="utf-8",
    ) as fh:
        payload = json.load(fh)

    risk = payload["risk"]
    assert set(risk) == {
        "daily_loss_limit_pct",
        "max_consecutive_losses",
        "circuit_breaker_cooldown_minutes",
        "operating_capital_krw",
        "account_risk_day_rollover_hour_kst",
        "order_reject_threshold",
        "order_reject_window_minutes",
        "order_reject_cooldown_minutes",
        "stale_exit_replace_minutes",
        "repeated_skip_notify_cooldown_minutes",
    }
    assert payload["_strategy_changes"][0]["date"] == "2026-07-14"


def test_load_app_config_uses_live_profile_variables(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ENV", "prod")
    monkeypatch.setenv("KIS_PROD_APPKEY", "live-key")
    monkeypatch.setenv("KIS_PROD_APPSECRET", "live-secret")
    monkeypatch.setenv("KIS_PROD_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_PROD_ACCOUNT_PRODUCT_CODE", "22")

    config = load_app_config()

    assert config.credentials.env == "prod"
    assert config.credentials.profile_name == "live"
    assert config.credentials.appkey == "live-key"
    assert config.credentials.appsecret == "live-secret"
    assert config.credentials.account_no == "12345678"
    assert config.credentials.account_product_code == "22"
    assert config.auto_trade.sec_fee_rate > 0
    assert config.auto_trade.max_decision_cycles_per_run >= 0
    assert config.liquidity_lab.domestic_test_order_qty >= 1
    assert config.liquidity_lab.use_slot_sizing is True
    assert config.liquidity_lab.slot_entry_pct > 0
    assert config.liquidity_lab.slot_max_pct >= config.liquidity_lab.slot_entry_pct
    assert config.notifications.telegram_command_poll_timeout_sec > 0


def test_auto_trade_default_symbol_is_not_soxl(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ENV", "vps")
    monkeypatch.setenv("KIS_VPS_APPKEY", "paper-key")
    monkeypatch.setenv("KIS_VPS_APPSECRET", "paper-secret")
    monkeypatch.setenv("KIS_VPS_ACCOUNT_NO", "8765432101")
    monkeypatch.delenv("KIS_VPS_ACCOUNT_PRODUCT_CODE", raising=False)

    config = load_app_config()

    assert config.auto_trade.symbol != "SOXL"
    assert config.auto_trade.symbol == "NVDA"
    assert config.auto_trade.mode == "FIXED_SYMBOL_MOMENTUM"
