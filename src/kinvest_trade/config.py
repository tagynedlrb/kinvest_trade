from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class KisCredentials:
    env: str
    appkey: str
    appsecret: str
    account_no: str
    account_product_code: str
    hts_id: str
    dry_run: bool
    live_trading_enabled: bool
    appkey_path: Path | None
    appsecret_path: Path | None
    token_cache_path: Path

    @property
    def base_url(self) -> str:
        if self.env == "prod":
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    @property
    def websocket_url(self) -> str:
        if self.env == "prod":
            return "ws://ops.koreainvestment.com:21000"
        return "ws://ops.koreainvestment.com:31000"

    @property
    def profile_name(self) -> str:
        if self.env == "prod":
            return "live"
        return "paper"


@dataclass(slots=True)
class TradingConfig:
    market: str
    market_code: str
    watchlist: list[str]
    signal_interval_sec: int
    initial_entry_fraction: float
    scale_in_fraction: float
    max_positions: int
    max_position_value_krw: int
    total_capital_limit_krw: int
    allow_scale_in: bool
    no_new_buy_after: str


@dataclass(slots=True)
class WatchConfig:
    poll_interval_sec: int
    chart_timeframe: str
    chart_bar_limit: int
    clear_screen: bool
    max_cycles: int
    telegram_summary_every: int


@dataclass(slots=True)
class PaperConfig:
    starting_cash_krw: int
    poll_interval_sec: int
    history_window: int
    entry_trigger_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    trailing_stop_pct: float
    max_spread_pct: float
    min_bid_ask_ratio: float
    max_iterations: int


@dataclass(slots=True)
class AutoTradeConfig:
    enabled: bool
    mode: str
    symbol: str
    exchange_code: str
    currency_code: str
    quantity: int
    use_slot_sizing: bool
    slot_entry_pct: float
    slot_scale_in_pct: float
    slot_max_pct: float
    max_position_qty: int
    poll_interval_sec: int
    max_actions_per_run: int
    max_decision_cycles_per_run: int
    min_history_points: int
    daily_fast_window: int
    daily_slow_window: int
    intraday_fast_window: int
    intraday_slow_window: int
    intraday_bar_minutes: int
    intraday_chart_refresh_sec: int
    daily_chart_refresh_sec: int
    entry_pullback_pct: float
    add_on_pullback_pct: float
    breakout_entry_pct: float
    take_profit_pct: float
    full_take_profit_pct: float
    stop_loss_pct: float
    hard_stop_loss_pct: float
    trailing_stop_pct: float
    trailing_volatility_multiplier: float
    soft_stop_volatility_multiplier: float
    hard_stop_volatility_multiplier: float
    volatility_window: int
    momentum_window: int
    volume_window: int
    rsi_period: int
    breakout_lookback_bars: int
    breakout_proximity_pct: float
    volume_spike_ratio: float
    volume_spike_ratio_prefilter_factor: float
    scale_in_volume_ratio: float
    volume_fade_ratio: float
    min_intraday_momentum_pct: float
    min_bar_return_pct: float
    max_breakout_extension_pct: float
    pullback_distance_lower_pct: float
    pullback_distance_upper_pct: float
    pullback_rsi_low: float
    pullback_rsi_high: float
    pullback_min_volume_ratio: float
    bollinger_window: int
    bollinger_stddev: float
    bollinger_breakout_buffer_pct: float
    atr_window: int
    atr_soft_stop_multiplier: float
    atr_hard_stop_multiplier: float
    atr_trailing_stop_multiplier: float
    partial_exit_rsi14: float
    min_hold_before_marginal_exit: int
    scale_in_profit_trigger_pct: float
    volatility_high_threshold: float
    strong_rebound_pct: float
    max_spread_pct: float
    ma60_entry_buffer_pct: float
    ma20_breakdown_buffer_pct: float
    ma60_hard_stop_buffer_pct: float
    ma20_partial_exit_buffer_pct: float
    trend_chase_limit_pct: float
    max_entry_rsi14: float
    trend_require_price_above_slow: bool
    min_hold_before_trend_exit: int
    max_hold_cycles: int
    force_reentry_after_cycles: int
    startup_buy_if_flat: bool
    allow_time_reentry: bool
    allow_scale_in: bool
    allow_partial_exit: bool
    scale_in_cooldown_cycles: int
    telegram_notify_each_fill: bool
    commission_rate: float
    sec_fee_rate: float
    fx_fee_rate: float
    min_expected_reward_cost_ratio: float
    min_expected_reward_risk_ratio: float
    annual_tax_free_allowance_krw: int
    capital_gains_tax_rate: float
    usd_krw_fallback_rate: float
    stale_run_grace_minutes: int
    inverse_etf_symbols: list[str]
    leveraged_etf_symbols: list[str]


@dataclass(slots=True)
class StrategyConfig:
    rsi_min: float
    rsi_max: float
    min_volume_ratio: float
    max_spread_pct: float
    min_recent_turnover_krw: int
    max_ret_1m: float
    max_ret_3m: float


@dataclass(slots=True)
class RiskConfig:
    daily_loss_limit_pct: float
    max_consecutive_losses: int
    circuit_breaker_cooldown_minutes: int = 30
    operating_capital_krw: int = 50_000_000


@dataclass(slots=True)
class StorageConfig:
    db_path: Path
    log_dir: Path
    runtime_state_path: Path


@dataclass(slots=True)
class NotificationConfig:
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_bot_token_path: Path | None
    telegram_chat_id_path: Path | None
    telegram_command_poll_timeout_sec: int


@dataclass(slots=True)
class OverseasCandidateConfig:
    symbol: str
    exchange_code: str


@dataclass(slots=True)
class LiquidityLabConfig:
    enabled: bool
    domestic_candidates: list[str]
    overseas_candidates: list[OverseasCandidateConfig]
    tv_scan_enabled: bool
    tv_top_n: int
    tv_min_rel_volume: float
    tv_min_price_usd: float
    tv_min_volume: int
    tv_min_market_cap: float
    tv_max_market_cap: float
    tv_max_change_pct: float
    loop_interval_sec: int
    use_slot_sizing: bool
    slot_entry_pct: float
    slot_max_pct: float
    domestic_paper_iterations: int
    domestic_paper_interval_sec: int
    unified_watch_top_n: int
    unified_scan_top_n: int
    overseas_scan_top_n: int
    overseas_rescan_cycles: int
    max_wait_cycles_before_penalty: int
    wait_penalty_decay: float
    domestic_dynamic_scan: bool
    domestic_dynamic_top_n: int
    domestic_dynamic_rescan_cycles: int
    domestic_dynamic_min_price_krw: int
    domestic_dynamic_min_volume: int
    vol_surge_threshold_strong: float
    vol_surge_threshold_mild: float
    overseas_relist_schedule_kst: str
    domestic_test_order_qty: int
    overseas_test_order_qty: int
    max_concurrent_overseas_orders: int
    max_concurrent_domestic_orders: int
    domestic_min_price_krw: int
    domestic_min_intraday_turnover_krw: int
    domestic_min_volume_sum: int
    domestic_max_spread_pct: float
    overseas_min_price_usd: float
    overseas_min_volume: int
    overseas_max_spread_pct: float
    overseas_take_profit_pct: float
    overseas_stop_loss_pct: float
    overseas_max_position_qty: int
    inverse_etf_symbols: list[str]
    leveraged_etf_symbols: list[str]


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    credentials: KisCredentials
    trading: TradingConfig
    watch: WatchConfig
    paper: PaperConfig
    auto_trade: AutoTradeConfig
    strategy: StrategyConfig
    risk: RiskConfig
    storage: StorageConfig
    notifications: NotificationConfig
    liquidity_lab: LiquidityLabConfig
    github_token: str
    github_repo: str
    skip_holiday_overseas: bool
    skip_holiday_domestic: bool


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return project_root / path


def _parse_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_optional_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _normalize_kis_env(raw_env: str | None) -> str:
    value = (raw_env or "").strip().lower()
    if value in {"prod", "live", "real", "production"}:
        return "prod"
    if value in {"vps", "paper", "mock", "demo", "test"}:
        return "vps"
    return "prod"


def _load_optional_value(env_names: list[str]) -> str:
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def _load_secret(
    env_names: list[str],
    candidate_paths: list[Path],
) -> tuple[str, Path | None]:
    value = _load_optional_value(env_names)
    if value:
        return value, None

    for path in candidate_paths:
        secret = _read_optional_file(path)
        if secret:
            return secret, path

    return "", candidate_paths[0] if candidate_paths else None


def _split_account_fields(raw_account_no: str, raw_product_code: str) -> tuple[str, str]:
    account_no = raw_account_no.strip()
    product_code = raw_product_code.strip()

    # 사용자가 12345678-01 형식으로 넣는 경우도 자연스럽게 지원한다.
    if not product_code and "-" in account_no:
        left, right = account_no.split("-", 1)
        left = left.strip()
        right = right.strip()
        if left.isdigit() and len(left) == 8 and right.isdigit() and len(right) == 2:
            return left, right

    # KIS는 보통 계좌 앞 8자리 + 상품코드 2자리 구조다.
    # 사용자가 10자리를 한 번에 넣는 경우를 대비해 자동으로 분리한다.
    if not product_code and account_no.isdigit() and len(account_no) == 10:
        return account_no[:8], account_no[8:]

    # 상품코드를 따로 비워둔 8자리 계좌는 일반적으로 01을 많이 사용한다.
    # 사용자가 별도 값을 넣지 않았다면 우선 01로 보정해 테스트 가능성을 높인다.
    if not product_code and account_no.isdigit() and len(account_no) == 8:
        return account_no, "01"

    return account_no, product_code


def load_app_config(settings_path: str | Path | None = None) -> AppConfig:
    load_dotenv()
    project_root = _project_root()
    normalized_env = _normalize_kis_env(os.getenv("KIS_ENV", "prod"))
    config_path = (
        Path(settings_path)
        if settings_path is not None
        else project_root / "config" / "fixed_config.json"
    )
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    trading_raw = raw["trading"]
    watch_raw = raw.get("watch", {})
    paper_raw = raw["paper"]
    auto_trade_raw = raw.get("auto_trade", {})
    strategy_raw = raw["strategy"]
    risk_raw = raw["risk"]
    storage_raw = raw["storage"]
    notification_raw = raw.get("notifications", {})
    liquidity_lab_raw = raw.get("liquidity_lab", {})
    github_token = (
        os.getenv("GITHUB_TOKEN", "").strip()
        or str(raw.get("github_token", "") or "").strip()
        or _read_optional_file(project_root.parent / "git_token.txt")
    )
    github_repo = str(raw.get("github_repo", "tagynedlrb/kinvest_trade") or "").strip()

    sibling_telegram_root = project_root.parent / "kiwoom_trade" / "keys"

    if normalized_env == "prod":
        appkey_env_names = ["KIS_PROD_APPKEY", "KIS_LIVE_APPKEY", "KIS_APPKEY"]
        appsecret_env_names = ["KIS_PROD_APPSECRET", "KIS_LIVE_APPSECRET", "KIS_APPSECRET"]
        account_no_env_names = ["KIS_PROD_ACCOUNT_NO", "KIS_LIVE_ACCOUNT_NO", "KIS_ACCOUNT_NO"]
        account_product_env_names = [
            "KIS_PROD_ACCOUNT_PRODUCT_CODE",
            "KIS_LIVE_ACCOUNT_PRODUCT_CODE",
            "KIS_ACCOUNT_PRODUCT_CODE",
        ]
        hts_id_env_names = ["KIS_PROD_HTS_ID", "KIS_LIVE_HTS_ID", "KIS_HTS_ID"]
        appkey_paths = [
            project_root / "keys" / "prod_appkey.txt",
            project_root / "keys" / "live_appkey.txt",
            project_root / "keys" / "appkey.txt",
        ]
        appsecret_paths = [
            project_root / "keys" / "prod_appsecret.txt",
            project_root / "keys" / "live_appsecret.txt",
            project_root / "keys" / "appsecret.txt",
        ]
    else:
        appkey_env_names = [
            "KIS_VPS_APPKEY",
            "KIS_PAPER_APPKEY",
            "KIS_MOCK_APPKEY",
            "KIS_APPKEY",
        ]
        appsecret_env_names = [
            "KIS_VPS_APPSECRET",
            "KIS_PAPER_APPSECRET",
            "KIS_MOCK_APPSECRET",
            "KIS_APPSECRET",
        ]
        account_no_env_names = [
            "KIS_VPS_ACCOUNT_NO",
            "KIS_PAPER_ACCOUNT_NO",
            "KIS_MOCK_ACCOUNT_NO",
            "KIS_ACCOUNT_NO",
        ]
        account_product_env_names = [
            "KIS_VPS_ACCOUNT_PRODUCT_CODE",
            "KIS_PAPER_ACCOUNT_PRODUCT_CODE",
            "KIS_MOCK_ACCOUNT_PRODUCT_CODE",
            "KIS_ACCOUNT_PRODUCT_CODE",
        ]
        hts_id_env_names = ["KIS_VPS_HTS_ID", "KIS_PAPER_HTS_ID", "KIS_MOCK_HTS_ID", "KIS_HTS_ID"]
        appkey_paths = [
            project_root / "keys" / "vps_appkey.txt",
            project_root / "keys" / "paper_appkey.txt",
            project_root / "keys" / "mock_appkey.txt",
            project_root / "keys" / "appkey.txt",
        ]
        appsecret_paths = [
            project_root / "keys" / "vps_appsecret.txt",
            project_root / "keys" / "paper_appsecret.txt",
            project_root / "keys" / "mock_appsecret.txt",
            project_root / "keys" / "appsecret.txt",
        ]

    appkey, appkey_path = _load_secret(
        appkey_env_names,
        appkey_paths,
    )
    appsecret, appsecret_path = _load_secret(
        appsecret_env_names,
        appsecret_paths,
    )
    telegram_bot_token, telegram_bot_token_path = _load_secret(
        ["TELEGRAM_BOT_TOKEN"],
        [
            project_root / "keys" / "telegram_bot_token.txt",
            sibling_telegram_root / "telegram_bot_token.txt",
        ],
    )
    telegram_chat_id, telegram_chat_id_path = _load_secret(
        ["TELEGRAM_CHAT_ID"],
        [
            project_root / "keys" / "telegram_chat_id.txt",
            sibling_telegram_root / "telegram_chat_id.txt",
        ],
    )

    account_no, account_product_code = _split_account_fields(
        _load_optional_value(account_no_env_names),
        _load_optional_value(account_product_env_names),
    )

    storage = StorageConfig(
        db_path=_resolve_path(project_root, storage_raw["db_path"]),
        log_dir=_resolve_path(project_root, storage_raw["log_dir"]),
        runtime_state_path=_resolve_path(project_root, storage_raw["runtime_state_path"]),
    )

    return AppConfig(
        project_root=project_root,
        credentials=KisCredentials(
            env=normalized_env,
            appkey=appkey,
            appsecret=appsecret,
            account_no=account_no,
            account_product_code=account_product_code,
            hts_id=_load_optional_value(hts_id_env_names),
            dry_run=_parse_bool("DRY_RUN", True),
            live_trading_enabled=_parse_bool("LIVE_TRADING_ENABLED", False),
            appkey_path=appkey_path,
            appsecret_path=appsecret_path,
            token_cache_path=project_root / "state" / f"token_{normalized_env}.json",
        ),
        trading=TradingConfig(
            market=str(trading_raw["market"]),
            market_code=str(trading_raw.get("market_code", "J")),
            watchlist=list(trading_raw["watchlist"]),
            signal_interval_sec=int(trading_raw["signal_interval_sec"]),
            initial_entry_fraction=float(trading_raw["initial_entry_fraction"]),
            scale_in_fraction=float(trading_raw["scale_in_fraction"]),
            max_positions=int(trading_raw["max_positions"]),
            max_position_value_krw=int(trading_raw["max_position_value_krw"]),
            total_capital_limit_krw=int(trading_raw["total_capital_limit_krw"]),
            allow_scale_in=bool(trading_raw["allow_scale_in"]),
            no_new_buy_after=str(trading_raw["no_new_buy_after"]),
        ),
        watch=WatchConfig(
            poll_interval_sec=int(watch_raw.get("poll_interval_sec", 15)),
            chart_timeframe=str(watch_raw.get("chart_timeframe", "minute")),
            chart_bar_limit=int(watch_raw.get("chart_bar_limit", 30)),
            clear_screen=bool(watch_raw.get("clear_screen", True)),
            max_cycles=int(watch_raw.get("max_cycles", 0)),
            telegram_summary_every=int(watch_raw.get("telegram_summary_every", 4)),
        ),
        paper=PaperConfig(
            starting_cash_krw=int(paper_raw["starting_cash_krw"]),
            poll_interval_sec=int(paper_raw["poll_interval_sec"]),
            history_window=int(paper_raw["history_window"]),
            entry_trigger_pct=float(paper_raw["entry_trigger_pct"]),
            take_profit_pct=float(paper_raw["take_profit_pct"]),
            stop_loss_pct=float(paper_raw["stop_loss_pct"]),
            trailing_stop_pct=float(paper_raw.get("trailing_stop_pct", 0.004)),
            max_spread_pct=float(paper_raw.get("max_spread_pct", 0.003)),
            min_bid_ask_ratio=float(paper_raw["min_bid_ask_ratio"]),
            max_iterations=int(paper_raw["max_iterations"]),
        ),
        auto_trade=AutoTradeConfig(
            enabled=bool(auto_trade_raw.get("enabled", True)),
            mode=str(auto_trade_raw.get("mode", "FIXED_SYMBOL_MOMENTUM")),
            symbol=str(auto_trade_raw.get("symbol", "NVDA")),
            exchange_code=str(auto_trade_raw.get("exchange_code", "NASD")),
            currency_code=str(auto_trade_raw.get("currency_code", "USD")),
            quantity=int(auto_trade_raw.get("quantity", 1)),
            use_slot_sizing=bool(auto_trade_raw.get("use_slot_sizing", False)),
            slot_entry_pct=float(auto_trade_raw.get("slot_entry_pct", 0.10)),
            slot_scale_in_pct=float(auto_trade_raw.get("slot_scale_in_pct", 0.05)),
            slot_max_pct=float(auto_trade_raw.get("slot_max_pct", 0.20)),
            max_position_qty=int(auto_trade_raw.get("max_position_qty", 4)),
            poll_interval_sec=int(auto_trade_raw.get("poll_interval_sec", 3)),
            max_actions_per_run=int(auto_trade_raw.get("max_actions_per_run", 0)),
            max_decision_cycles_per_run=int(
                auto_trade_raw.get("max_decision_cycles_per_run", 0)
            ),
            min_history_points=int(auto_trade_raw.get("min_history_points", 6)),
            daily_fast_window=int(auto_trade_raw.get("daily_fast_window", 20)),
            daily_slow_window=int(auto_trade_raw.get("daily_slow_window", 60)),
            intraday_fast_window=int(auto_trade_raw.get("intraday_fast_window", 5)),
            intraday_slow_window=int(auto_trade_raw.get("intraday_slow_window", 20)),
            intraday_bar_minutes=int(auto_trade_raw.get("intraday_bar_minutes", 5)),
            intraday_chart_refresh_sec=int(
                auto_trade_raw.get("intraday_chart_refresh_sec", 60)
            ),
            daily_chart_refresh_sec=int(
                auto_trade_raw.get("daily_chart_refresh_sec", 900)
            ),
            rsi_period=int(auto_trade_raw.get("rsi_period", 14)),
            entry_pullback_pct=float(auto_trade_raw.get("entry_pullback_pct", 0.0005)),
            add_on_pullback_pct=float(auto_trade_raw.get("add_on_pullback_pct", 0.001)),
            breakout_entry_pct=float(auto_trade_raw.get("breakout_entry_pct", 0.001)),
            take_profit_pct=float(auto_trade_raw.get("take_profit_pct", 0.0005)),
            full_take_profit_pct=float(auto_trade_raw.get("full_take_profit_pct", 0.0015)),
            stop_loss_pct=float(auto_trade_raw.get("stop_loss_pct", 0.0005)),
            hard_stop_loss_pct=float(auto_trade_raw.get("hard_stop_loss_pct", 0.0015)),
            trailing_stop_pct=float(auto_trade_raw.get("trailing_stop_pct", 0.0008)),
            trailing_volatility_multiplier=float(
                auto_trade_raw.get("trailing_volatility_multiplier", 1.5)
            ),
            soft_stop_volatility_multiplier=float(
                auto_trade_raw.get("soft_stop_volatility_multiplier", 1.25)
            ),
            hard_stop_volatility_multiplier=float(
                auto_trade_raw.get("hard_stop_volatility_multiplier", 2.0)
            ),
            volatility_window=int(auto_trade_raw.get("volatility_window", 8)),
            momentum_window=int(auto_trade_raw.get("momentum_window", 6)),
            volume_window=int(auto_trade_raw.get("volume_window", 6)),
            breakout_lookback_bars=int(auto_trade_raw.get("breakout_lookback_bars", 6)),
            breakout_proximity_pct=float(
                auto_trade_raw.get("breakout_proximity_pct", 0.98)
            ),
            volume_spike_ratio=float(auto_trade_raw.get("volume_spike_ratio", 1.8)),
            volume_spike_ratio_prefilter_factor=float(
                auto_trade_raw.get("volume_spike_ratio_prefilter_factor", 0.7)
            ),
            scale_in_volume_ratio=float(auto_trade_raw.get("scale_in_volume_ratio", 1.3)),
            volume_fade_ratio=float(auto_trade_raw.get("volume_fade_ratio", 0.85)),
            min_intraday_momentum_pct=float(
                auto_trade_raw.get("min_intraday_momentum_pct", 0.003)
            ),
            min_bar_return_pct=float(auto_trade_raw.get("min_bar_return_pct", 0.0015)),
            max_breakout_extension_pct=float(
                auto_trade_raw.get("max_breakout_extension_pct", 0.01)
            ),
            pullback_distance_lower_pct=float(
                auto_trade_raw.get("pullback_distance_lower_pct", 0.015)
            ),
            pullback_distance_upper_pct=float(
                auto_trade_raw.get("pullback_distance_upper_pct", 0.005)
            ),
            pullback_rsi_low=float(auto_trade_raw.get("pullback_rsi_low", 35.0)),
            pullback_rsi_high=float(auto_trade_raw.get("pullback_rsi_high", 62.0)),
            pullback_min_volume_ratio=float(
                auto_trade_raw.get("pullback_min_volume_ratio", 1.3)
            ),
            bollinger_window=int(auto_trade_raw.get("bollinger_window", 20)),
            bollinger_stddev=float(auto_trade_raw.get("bollinger_stddev", 2.0)),
            bollinger_breakout_buffer_pct=float(
                auto_trade_raw.get("bollinger_breakout_buffer_pct", 0.0)
            ),
            atr_window=int(auto_trade_raw.get("atr_window", 14)),
            atr_soft_stop_multiplier=float(
                auto_trade_raw.get("atr_soft_stop_multiplier", 1.2)
            ),
            atr_hard_stop_multiplier=float(
                auto_trade_raw.get("atr_hard_stop_multiplier", 1.8)
            ),
            atr_trailing_stop_multiplier=float(
                auto_trade_raw.get("atr_trailing_stop_multiplier", 1.4)
            ),
            partial_exit_rsi14=float(auto_trade_raw.get("partial_exit_rsi14", 70.0)),
            min_hold_before_marginal_exit=int(
                auto_trade_raw.get("min_hold_before_marginal_exit", 10)
            ),
            scale_in_profit_trigger_pct=float(
                auto_trade_raw.get("scale_in_profit_trigger_pct", 0.003)
            ),
            volatility_high_threshold=float(
                auto_trade_raw.get("volatility_high_threshold", 0.004)
            ),
            strong_rebound_pct=float(auto_trade_raw.get("strong_rebound_pct", 0.0015)),
            max_spread_pct=float(auto_trade_raw.get("max_spread_pct", 0.003)),
            ma60_entry_buffer_pct=float(auto_trade_raw.get("ma60_entry_buffer_pct", 0.012)),
            ma20_breakdown_buffer_pct=float(
                auto_trade_raw.get("ma20_breakdown_buffer_pct", 0.004)
            ),
            ma60_hard_stop_buffer_pct=float(
                auto_trade_raw.get("ma60_hard_stop_buffer_pct", 0.01)
            ),
            ma20_partial_exit_buffer_pct=float(
                auto_trade_raw.get("ma20_partial_exit_buffer_pct", 0.0025)
            ),
            trend_chase_limit_pct=float(
                auto_trade_raw.get("trend_chase_limit_pct", 0.02)
            ),
            max_entry_rsi14=float(auto_trade_raw.get("max_entry_rsi14", 62.0)),
            trend_require_price_above_slow=bool(
                auto_trade_raw.get("trend_require_price_above_slow", True)
            ),
            min_hold_before_trend_exit=int(
                auto_trade_raw.get("min_hold_before_trend_exit", 3)
            ),
            max_hold_cycles=int(auto_trade_raw.get("max_hold_cycles", 1)),
            force_reentry_after_cycles=int(auto_trade_raw.get("force_reentry_after_cycles", 1)),
            startup_buy_if_flat=bool(auto_trade_raw.get("startup_buy_if_flat", True)),
            allow_time_reentry=bool(auto_trade_raw.get("allow_time_reentry", False)),
            allow_scale_in=bool(auto_trade_raw.get("allow_scale_in", True)),
            allow_partial_exit=bool(auto_trade_raw.get("allow_partial_exit", True)),
            scale_in_cooldown_cycles=int(auto_trade_raw.get("scale_in_cooldown_cycles", 3)),
            telegram_notify_each_fill=bool(
                auto_trade_raw.get("telegram_notify_each_fill", True)
            ),
            commission_rate=float(auto_trade_raw.get("commission_rate", 0.0025)),
            sec_fee_rate=float(auto_trade_raw.get("sec_fee_rate", 0.0000206)),
            fx_fee_rate=float(auto_trade_raw.get("fx_fee_rate", 0.0)),
            min_expected_reward_cost_ratio=float(
                auto_trade_raw.get("min_expected_reward_cost_ratio", 0.5)
            ),
            min_expected_reward_risk_ratio=float(
                auto_trade_raw.get("min_expected_reward_risk_ratio", 1.2)
            ),
            annual_tax_free_allowance_krw=int(
                auto_trade_raw.get("annual_tax_free_allowance_krw", 2_500_000)
            ),
            capital_gains_tax_rate=float(
                auto_trade_raw.get("capital_gains_tax_rate", 0.22)
            ),
            usd_krw_fallback_rate=float(
                auto_trade_raw.get("usd_krw_fallback_rate", 1350.0)
            ),
            stale_run_grace_minutes=int(auto_trade_raw.get("stale_run_grace_minutes", 180)),
            inverse_etf_symbols=[
                str(value)
                for value in liquidity_lab_raw.get("inverse_etf_symbols", ["SQQQ", "SOXS"])
            ],
            leveraged_etf_symbols=[
                str(value)
                for value in liquidity_lab_raw.get("leveraged_etf_symbols", ["TQQQ", "SOXL"])
            ],
        ),
        strategy=StrategyConfig(
            rsi_min=float(strategy_raw["rsi_min"]),
            rsi_max=float(strategy_raw["rsi_max"]),
            min_volume_ratio=float(strategy_raw["min_volume_ratio"]),
            max_spread_pct=float(strategy_raw["max_spread_pct"]),
            min_recent_turnover_krw=int(strategy_raw["min_recent_turnover_krw"]),
            max_ret_1m=float(strategy_raw["max_ret_1m"]),
            max_ret_3m=float(strategy_raw["max_ret_3m"]),
        ),
        risk=RiskConfig(
            daily_loss_limit_pct=float(risk_raw["daily_loss_limit_pct"]),
            max_consecutive_losses=int(risk_raw["max_consecutive_losses"]),
            circuit_breaker_cooldown_minutes=int(
                risk_raw.get("circuit_breaker_cooldown_minutes", 30)
            ),
            operating_capital_krw=int(risk_raw.get("operating_capital_krw", 50_000_000)),
        ),
        storage=storage,
        notifications=NotificationConfig(
            telegram_enabled=bool(notification_raw.get("telegram_enabled", True)),
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            telegram_bot_token_path=telegram_bot_token_path,
            telegram_chat_id_path=telegram_chat_id_path,
            telegram_command_poll_timeout_sec=int(
                notification_raw.get("telegram_command_poll_timeout_sec", 20)
            ),
        ),
        liquidity_lab=LiquidityLabConfig(
            enabled=bool(liquidity_lab_raw.get("enabled", True)),
            domestic_candidates=[
                str(value) for value in liquidity_lab_raw.get("domestic_candidates", ["005930"])
            ],
            overseas_candidates=[
                OverseasCandidateConfig(
                    symbol=str(item.get("symbol", "")),
                    exchange_code=str(item.get("exchange_code", "NASD")),
                )
                for item in liquidity_lab_raw.get(
                    "overseas_candidates",
                    [],
                )
                if str(item.get("symbol", "")).strip()
            ],
            tv_scan_enabled=bool(liquidity_lab_raw.get("tv_scan_enabled", True)),
            tv_top_n=int(liquidity_lab_raw.get("tv_top_n", 30)),
            tv_min_rel_volume=float(liquidity_lab_raw.get("tv_min_rel_volume", 2.0)),
            tv_min_price_usd=float(liquidity_lab_raw.get("tv_min_price_usd", 1.0)),
            tv_min_volume=int(liquidity_lab_raw.get("tv_min_volume", 500_000)),
            tv_min_market_cap=float(liquidity_lab_raw.get("tv_min_market_cap", 3e8)),
            tv_max_market_cap=float(liquidity_lab_raw.get("tv_max_market_cap", 2e12)),
            tv_max_change_pct=float(liquidity_lab_raw.get("tv_max_change_pct", 20.0)),
            loop_interval_sec=int(liquidity_lab_raw.get("loop_interval_sec", 120)),
            use_slot_sizing=bool(liquidity_lab_raw.get("use_slot_sizing", False)),
            slot_entry_pct=float(liquidity_lab_raw.get("slot_entry_pct", 0.10)),
            slot_max_pct=float(liquidity_lab_raw.get("slot_max_pct", 0.20)),
            domestic_paper_iterations=int(
                liquidity_lab_raw.get("domestic_paper_iterations", 6)
            ),
            domestic_paper_interval_sec=int(
                liquidity_lab_raw.get("domestic_paper_interval_sec", 5)
            ),
            unified_watch_top_n=int(liquidity_lab_raw.get("unified_watch_top_n", 15)),
            unified_scan_top_n=int(liquidity_lab_raw.get("unified_scan_top_n", 15)),
            overseas_scan_top_n=int(liquidity_lab_raw.get("overseas_scan_top_n", 69)),
            overseas_rescan_cycles=int(liquidity_lab_raw.get("overseas_rescan_cycles", 20)),
            max_wait_cycles_before_penalty=int(
                liquidity_lab_raw.get("max_wait_cycles_before_penalty", 15)
            ),
            wait_penalty_decay=float(liquidity_lab_raw.get("wait_penalty_decay", 0.07)),
            domestic_dynamic_scan=bool(liquidity_lab_raw.get("domestic_dynamic_scan", True)),
            domestic_dynamic_top_n=int(liquidity_lab_raw.get("domestic_dynamic_top_n", 20)),
            domestic_dynamic_rescan_cycles=int(
                liquidity_lab_raw.get("domestic_dynamic_rescan_cycles", 20)
            ),
            domestic_dynamic_min_price_krw=int(
                liquidity_lab_raw.get("domestic_dynamic_min_price_krw", 5000)
            ),
            domestic_dynamic_min_volume=int(
                liquidity_lab_raw.get("domestic_dynamic_min_volume", 200_000)
            ),
            vol_surge_threshold_strong=float(
                liquidity_lab_raw.get("vol_surge_threshold_strong", 5.0)
            ),
            vol_surge_threshold_mild=float(
                liquidity_lab_raw.get("vol_surge_threshold_mild", 3.0)
            ),
            overseas_relist_schedule_kst=str(
                liquidity_lab_raw.get("overseas_relist_schedule_kst", "22:35,01:00,03:30")
            ),
            domestic_test_order_qty=int(liquidity_lab_raw.get("domestic_test_order_qty", 1)),
            overseas_test_order_qty=int(liquidity_lab_raw.get("overseas_test_order_qty", 1)),
            max_concurrent_overseas_orders=int(
                liquidity_lab_raw.get("max_concurrent_overseas_orders", 20)
            ),
            max_concurrent_domestic_orders=int(
                liquidity_lab_raw.get("max_concurrent_domestic_orders", 5)
            ),
            domestic_min_price_krw=int(liquidity_lab_raw.get("domestic_min_price_krw", 3000)),
            domestic_min_intraday_turnover_krw=int(
                liquidity_lab_raw.get("domestic_min_intraday_turnover_krw", 50_000_000_000)
            ),
            domestic_min_volume_sum=int(liquidity_lab_raw.get("domestic_min_volume_sum", 100_000)),
            domestic_max_spread_pct=float(liquidity_lab_raw.get("domestic_max_spread_pct", 0.003)),
            overseas_min_price_usd=float(liquidity_lab_raw.get("overseas_min_price_usd", 5.0)),
            overseas_min_volume=int(liquidity_lab_raw.get("overseas_min_volume", 500_000)),
            overseas_max_spread_pct=float(liquidity_lab_raw.get("overseas_max_spread_pct", 0.003)),
            overseas_take_profit_pct=float(liquidity_lab_raw.get("overseas_take_profit_pct", 0.012)),
            overseas_stop_loss_pct=float(liquidity_lab_raw.get("overseas_stop_loss_pct", 0.008)),
            overseas_max_position_qty=int(liquidity_lab_raw.get("overseas_max_position_qty", 1)),
            inverse_etf_symbols=[
                str(value)
                for value in liquidity_lab_raw.get("inverse_etf_symbols", ["SQQQ", "SOXS"])
            ],
            leveraged_etf_symbols=[
                str(value)
                for value in liquidity_lab_raw.get("leveraged_etf_symbols", ["TQQQ", "SOXL"])
            ],
        ),
        github_token=github_token,
        github_repo=github_repo or "tagynedlrb/kinvest_trade",
        skip_holiday_overseas=bool(raw.get("skip_holiday_overseas", True)),
        skip_holiday_domestic=bool(raw.get("skip_holiday_domestic", True)),
    )
