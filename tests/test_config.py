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
    assert len(config.liquidity_lab.domestic_candidates) >= 1
    assert len(config.liquidity_lab.overseas_candidates) >= 1
    assert config.notifications.telegram_command_poll_timeout_sec > 0
    assert config.liquidity_lab.loop_interval_sec > 0


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
    assert config.notifications.telegram_command_poll_timeout_sec > 0
