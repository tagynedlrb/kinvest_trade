import json

from kinvest_trade.config import _normalize_kis_env, _split_account_fields, load_app_config


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
    assert domestic.post_cb_reentry_benchmark_floor_pct is None
    assert overseas.post_cb_reentry_benchmark_floor_pct == -1.0
    assert domestic.post_cb_reentry_regime_max_age_sec == 600
    assert overseas.post_cb_reentry_regime_max_age_sec == 600
    assert domestic.post_cb_max_fires_per_session is None
    assert overseas.post_cb_max_fires_per_session == 2
    assert domestic.auto_trade.strategy_guard_lookback_hours == 48
    assert overseas.auto_trade.strategy_guard_lookback_hours == 48
    assert domestic.auto_trade.strategy_guard_min_trades == 3
    assert overseas.auto_trade.strategy_guard_min_trades == 3
    assert domestic.auto_trade.strategy_guard_max_avg_net_pnl_pct == -0.003
    assert overseas.auto_trade.strategy_guard_max_avg_net_pnl_pct == -0.003
    assert domestic.auto_trade.strategy_guard_strategy_flags == [
        "VWAP",
        "RSI",
        "VOL",
    ]
    assert overseas.auto_trade.strategy_guard_strategy_flags == [
        "VWAP",
        "RSI",
        "VOL",
    ]
    assert domestic.engine == overseas.engine == "momentum_v1"
    assert domestic.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert overseas.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert domestic.auto_trade.inverse_etf_symbols == ["114800", "252670"]
    assert overseas.auto_trade.inverse_etf_symbols == ["SQQQ", "SOXS", "SPXU"]
    assert domestic.auto_trade.leveraged_etf_symbols == ["122630"]
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

    domestic.auto_trade.take_profit_pct = 0.123
    domestic.auto_trade.inverse_etf_symbols.append("KRX_TEST")
    domestic.auto_trade.strategy_guard_strategy_flags.append("KRX_TEST")

    assert overseas.auto_trade.take_profit_pct == config.auto_trade.take_profit_pct
    assert "KRX_TEST" not in overseas.auto_trade.inverse_etf_symbols
    assert "KRX_TEST" not in overseas.auto_trade.strategy_guard_strategy_flags
    assert domestic.source_path is not None
    assert domestic.source_path.name == "domestic.json"
    assert overseas.source_path is not None
    assert overseas.source_path.name == "overseas.json"


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
