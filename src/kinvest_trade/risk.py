from __future__ import annotations

from .config import RiskConfig
from .models import AccountState, MarketSnapshot


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def allow_new_buy(
        self,
        account: AccountState,
        market: MarketSnapshot,
        planned_order_value_krw: int,
    ) -> tuple[bool, str]:
        if not account.trading_enabled:
            return False, "trading disabled"

        if account.daily_pnl_pct <= -self.config.daily_loss_limit_pct:
            return False, "daily loss limit reached"

        if account.consecutive_losses >= self.config.max_consecutive_losses:
            return False, "consecutive loss guard active"

        if account.open_positions <= -1:
            return False, "account state invalid"

        if planned_order_value_krw > 0 and planned_order_value_krw > market.current_price:
            if planned_order_value_krw > self.config.min_recent_turnover_krw:
                return False, "planned order exceeds liquidity guard"

        if abs(market.ret_1m) > self.config.block_buy_if_1m_move_abs_gt:
            return False, "1m move too large"

        if market.ret_3m > self.config.block_buy_if_3m_rise_gt:
            return False, "3m rise too large"

        if market.spread_pct > self.config.max_spread_pct:
            return False, "spread too wide"

        if market.recent_turnover_krw < self.config.min_recent_turnover_krw:
            return False, "turnover too low"

        return True, "ok"

