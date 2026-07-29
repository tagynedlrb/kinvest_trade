from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal

from .config import AppConfig, AutoTradeConfig, MarketPolicyDefinition
from .momentum_policy import (
    EntrySetup,
    ExitSetup,
    derive_watch_state,
    evaluate_entry_setup,
    evaluate_exit_setup,
)
from .strategy.manager import PriorityStrategyManager
from .technical_signals import MovingAverageSnapshot

MarketName = Literal["domestic", "overseas"]


@dataclass(frozen=True, slots=True)
class StrategyGuardPolicy:
    lookback_hours: int
    min_trades: int
    max_avg_net_pnl_pct: float
    strategy_flags: frozenset[str]
    min_final_sessions: int
    fallback_cost_pct: float


def normalize_market_name(market: str) -> MarketName:
    normalized = str(market).strip().lower()
    aliases = {
        "domestic": "domestic",
        "krx": "domestic",
        "korea": "domestic",
        "overseas": "overseas",
        "us": "overseas",
        "usa": "overseas",
    }
    resolved = aliases.get(normalized)
    if resolved is None:
        raise ValueError(f"unsupported market policy: {market}")
    return resolved  # type: ignore[return-value]


def get_market_auto_trade_config(
    config: AppConfig | object,
    market: str,
) -> AutoTradeConfig | None:
    normalized = normalize_market_name(market)
    policies = getattr(config, "market_policies", None)
    definition = getattr(policies, normalized, None) if policies is not None else None
    configured = getattr(definition, "auto_trade", None)
    if configured is not None:
        return configured
    return getattr(config, "auto_trade", None)


def get_market_strategy_guard_policy(
    config: AppConfig | object,
    market: str,
) -> StrategyGuardPolicy:
    normalized = normalize_market_name(market)
    policies = getattr(config, "market_policies", None)
    definition = getattr(policies, normalized, None) if policies is not None else None
    market_auto_trade = getattr(definition, "auto_trade", None)
    auto_trade = market_auto_trade or get_market_auto_trade_config(
        config,
        normalized,
    )
    liquidity_lab = getattr(config, "liquidity_lab", object())
    guard_source = market_auto_trade or liquidity_lab

    raw_flags = getattr(
        guard_source,
        "strategy_guard_strategy_flags",
        ["VWAP", "RSI", "VOL"],
    )
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    strategy_flags = frozenset(
        str(flag).strip().upper()
        for flag in raw_flags
        if str(flag).strip()
    )

    raw_legacy_commission = getattr(
        auto_trade,
        "commission_rate",
        None,
    )
    legacy_commission = (
        None
        if raw_legacy_commission is None
        else float(raw_legacy_commission or 0.0)
    )
    if normalized == "domestic":
        raw_commission = getattr(
            auto_trade,
            "domestic_commission_rate",
            None,
        )
        commission = (
            float(raw_commission or 0.0)
            if raw_commission is not None
            else (
                legacy_commission
                if legacy_commission is not None
                else 0.00015
            )
        )
        sell_tax = float(
            getattr(auto_trade, "domestic_sell_tax_rate", 0.002) or 0.0
        )
        fallback_cost_pct = commission * 2 + sell_tax
    else:
        raw_commission = getattr(
            auto_trade,
            "overseas_commission_rate",
            None,
        )
        commission = (
            float(raw_commission or 0.0)
            if raw_commission is not None
            else (
                legacy_commission
                if legacy_commission is not None
                else 0.0025
            )
        )
        sec_fee = float(
            getattr(auto_trade, "sec_fee_rate", 0.0000206) or 0.0
        )
        fallback_cost_pct = commission * 2 + sec_fee

    return StrategyGuardPolicy(
        lookback_hours=max(
            1,
            int(
                getattr(
                    guard_source,
                    "strategy_guard_lookback_hours",
                    48,
                )
                or 48
            ),
        ),
        min_trades=max(
            1,
            int(
                getattr(
                    guard_source,
                    "strategy_guard_min_trades",
                    3,
                )
                or 3
            ),
        ),
        max_avg_net_pnl_pct=float(
            getattr(
                guard_source,
                "strategy_guard_max_avg_net_pnl_pct",
                -0.003,
            )
        ),
        strategy_flags=strategy_flags,
        min_final_sessions=max(
            0,
            int(
                getattr(
                    auto_trade,
                    "strategy_guard_min_final_sessions",
                    3,
                )
                or 0
            ),
        ),
        fallback_cost_pct=max(0.0, fallback_cost_pct),
    )


class MomentumMarketPolicy:
    """Market-scoped strategy facade.

    Both markets initially use the same momentum engine. Their parameter
    objects and strategy-manager instances are separate so either market can
    evolve without silently changing the other.
    """

    market: MarketName

    def __init__(
        self,
        config: AppConfig,
        definition: MarketPolicyDefinition | None,
    ) -> None:
        self._app_config = config
        self.definition = definition
        base_auto_trade = getattr(config, "auto_trade", None)
        self.auto_trade: AutoTradeConfig | None = (
            definition.auto_trade
            if definition is not None
            else deepcopy(base_auto_trade)
        )

    @property
    def policy_id(self) -> str:
        if self.definition is not None:
            return self.definition.policy_id
        return f"{self.market}_momentum_v1"

    @property
    def inverse_require_symbol_benchmark(self) -> bool:
        return bool(
            self.definition is not None
            and self.definition.inverse_require_symbol_benchmark
        )

    @property
    def inverse_benchmarks(self) -> dict:
        if self.definition is None:
            return {}
        return self.definition.inverse_benchmarks

    def evaluate_entry(
        self,
        snapshot: MovingAverageSnapshot,
        *,
        symbol: str,
        inverse_regime_eligible: bool | None = None,
    ) -> EntrySetup:
        if self.auto_trade is None:
            raise RuntimeError(f"{self.market} market policy requires auto_trade configuration")
        return evaluate_entry_setup(
            self.auto_trade,
            snapshot,
            symbol=symbol,
            inverse_etf_symbols=self.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=self.auto_trade.leveraged_etf_symbols,
            inverse_regime_eligible=inverse_regime_eligible,
        )

    def derive_watch_state(
        self,
        snapshot: MovingAverageSnapshot,
        *,
        symbol: str,
        inverse_regime_eligible: bool | None = None,
    ) -> tuple[str, str]:
        if self.auto_trade is None:
            raise RuntimeError(f"{self.market} market policy requires auto_trade configuration")
        return derive_watch_state(
            self.auto_trade,
            snapshot,
            symbol=symbol,
            inverse_etf_symbols=self.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=self.auto_trade.leveraged_etf_symbols,
            inverse_regime_eligible=inverse_regime_eligible,
        )

    def evaluate_exit(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
        *,
        drawdown_from_peak: float,
        hold_cycles: int,
        position_qty: int,
        partial_exit_done: bool,
        take_profit_override: float | None = None,
    ) -> ExitSetup:
        if self.auto_trade is None:
            raise RuntimeError(f"{self.market} market policy requires auto_trade configuration")
        policy_config: AutoTradeConfig = self.auto_trade
        if take_profit_override is not None:
            policy_config = replace(
                policy_config,
                take_profit_pct=take_profit_override,
            )
        return evaluate_exit_setup(
            policy_config,
            snapshot,
            pnl_pct,
            market=self.market,
            drawdown_from_peak=drawdown_from_peak,
            hold_cycles=hold_cycles,
            position_qty=position_qty,
            partial_exit_done=partial_exit_done,
        )

    def make_strategy_manager(self) -> PriorityStrategyManager:
        return PriorityStrategyManager(self.auto_trade)


class DomesticMomentumPolicy(MomentumMarketPolicy):
    market: MarketName = "domestic"


class OverseasMomentumPolicy(MomentumMarketPolicy):
    market: MarketName = "overseas"


class MarketPolicyRegistry:
    def __init__(self, config: AppConfig) -> None:
        definitions = getattr(config, "market_policies", None)
        domestic = definitions.domestic if definitions is not None else None
        overseas = definitions.overseas if definitions is not None else None
        self._policies: dict[MarketName, MomentumMarketPolicy] = {
            "domestic": DomesticMomentumPolicy(config, domestic),
            "overseas": OverseasMomentumPolicy(config, overseas),
        }

    def for_market(self, market: str) -> MomentumMarketPolicy:
        return self._policies[normalize_market_name(market)]
