from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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

    def evaluate_entry(
        self,
        snapshot: MovingAverageSnapshot,
        *,
        symbol: str,
    ) -> EntrySetup:
        if self.auto_trade is None:
            raise RuntimeError(f"{self.market} market policy requires auto_trade configuration")
        return evaluate_entry_setup(
            self.auto_trade,
            snapshot,
            symbol=symbol,
            inverse_etf_symbols=self.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=self.auto_trade.leveraged_etf_symbols,
        )

    def derive_watch_state(
        self,
        snapshot: MovingAverageSnapshot,
        *,
        symbol: str,
    ) -> tuple[str, str]:
        if self.auto_trade is None:
            raise RuntimeError(f"{self.market} market policy requires auto_trade configuration")
        return derive_watch_state(
            self.auto_trade,
            snapshot,
            symbol=symbol,
            inverse_etf_symbols=self.auto_trade.inverse_etf_symbols,
            leveraged_etf_symbols=self.auto_trade.leveraged_etf_symbols,
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
