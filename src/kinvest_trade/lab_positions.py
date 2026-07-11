from __future__ import annotations

from dataclasses import dataclass

from .repository import SqliteRepository


@dataclass(slots=True)
class VirtualPosition:
    market: str
    symbol: str
    exchange_code: str | None
    qty: int
    avg_price: float
    currency: str


class VirtualTradeManager:
    """
    Keep a virtual portfolio for orders rejected by the paper environment while the
    underlying market itself is open.
    """

    def __init__(self, repository: SqliteRepository) -> None:
        self.repository = repository

    def get_position(self, market: str, symbol: str) -> VirtualPosition | None:
        row = self.repository.get_virtual_position(market.strip().lower(), symbol.strip().upper())
        if row is None:
            return None
        return self._to_position(row)

    def list_positions(self, market: str | None = None) -> list[VirtualPosition]:
        positions = [self._to_position(row) for row in self.repository.list_virtual_positions()]
        if market is None:
            return positions
        market_key = market.strip().lower()
        return [position for position in positions if position.market == market_key]

    def record_buy(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        fill_price: float,
        currency: str,
        session: str,
        reason: str,
        created_at: str,
    ) -> VirtualPosition | None:
        if qty <= 0 or fill_price <= 0:
            return None

        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        existing = self.get_position(market_key, symbol_key)
        next_qty = qty if existing is None else existing.qty + qty
        avg_price = fill_price
        if existing is not None and next_qty > 0:
            avg_price = (
                existing.avg_price * existing.qty + fill_price * qty
            ) / next_qty

        self.repository.upsert_virtual_position(
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            qty=next_qty,
            avg_price=avg_price,
            currency=currency,
            opened_at=created_at,
            updated_at=created_at,
        )
        self.repository.save_virtual_order(
            created_at=created_at,
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            side="buy",
            qty=qty,
            fill_price=fill_price,
            currency=currency,
            session=session,
            reason=reason,
        )
        return self.get_position(market_key, symbol_key)

    def record_sell(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        fill_price: float,
        currency: str,
        session: str,
        reason: str,
        created_at: str,
        seed_avg_price: float | None = None,
        seed_qty: int | None = None,
    ) -> tuple[float, float]:
        if qty <= 0 or fill_price <= 0:
            return 0.0, 0.0

        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        position = self.get_position(market_key, symbol_key)
        if position is None and seed_avg_price and seed_avg_price > 0 and seed_qty and seed_qty > 0:
            position = VirtualPosition(
                market=market_key,
                symbol=symbol_key,
                exchange_code=exchange_code,
                qty=seed_qty,
                avg_price=seed_avg_price,
                currency=currency,
            )
        if position is None or position.qty <= 0:
            return 0.0, 0.0

        sell_qty = min(qty, position.qty)
        realized_pnl = (fill_price - position.avg_price) * sell_qty
        realized_pnl_pct = (
            (fill_price - position.avg_price) / position.avg_price
            if position.avg_price > 0
            else 0.0
        )
        remaining_qty = position.qty - sell_qty

        if remaining_qty > 0:
            self.repository.upsert_virtual_position(
                market=market_key,
                symbol=symbol_key,
                exchange_code=exchange_code or position.exchange_code,
                qty=remaining_qty,
                avg_price=position.avg_price,
                currency=position.currency,
                opened_at=created_at,
                updated_at=created_at,
            )
        else:
            self.repository.delete_virtual_position(market_key, symbol_key)

        self.repository.save_virtual_order(
            created_at=created_at,
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code or position.exchange_code,
            side="sell",
            qty=sell_qty,
            fill_price=fill_price,
            currency=position.currency,
            session=session,
            reason=reason,
            realized_pnl=realized_pnl,
            realized_pnl_pct=realized_pnl_pct,
        )
        return realized_pnl, realized_pnl_pct

    def performance_summary(self) -> dict:
        return self.repository.get_virtual_performance_summary()

    @staticmethod
    def _to_position(row: dict) -> VirtualPosition:
        return VirtualPosition(
            market=str(row["market"]),
            symbol=str(row["symbol"]),
            exchange_code=row.get("exchange_code"),
            qty=int(row["qty"]),
            avg_price=float(row["avg_price"]),
            currency=str(row["currency"]),
        )


@dataclass(slots=True)
class UnifiedPosition:
    market: str
    symbol: str
    exchange_code: str | None
    real_qty: int
    virtual_buy_qty: int
    virtual_sell_qty: int
    currency: str

    @property
    def total_qty(self) -> int:
        return self.real_qty + self.virtual_buy_qty - self.virtual_sell_qty


class UnifiedPositionTracker:
    """
    Track real holdings, virtual buys, and pending virtual sells in one place.
    """

    def __init__(
        self,
        repository: SqliteRepository,
        virtual_trades: VirtualTradeManager,
    ) -> None:
        self.repository = repository
        self.virtual_trades = virtual_trades

    def get_unified(
        self,
        market: str,
        symbol: str,
        real_qty: int,
        currency: str,
        exchange_code: str | None = None,
    ) -> UnifiedPosition:
        market_key = market.strip().lower()
        symbol_key = symbol.strip().upper()
        virtual_buy = self.virtual_trades.get_position(market_key, symbol_key)
        virtual_sell = self.repository.get_virtual_sell_pending(market_key, symbol_key)
        return UnifiedPosition(
            market=market_key,
            symbol=symbol_key,
            exchange_code=exchange_code,
            real_qty=real_qty,
            virtual_buy_qty=0 if virtual_buy is None else virtual_buy.qty,
            virtual_sell_qty=0 if virtual_sell is None else int(virtual_sell["qty"]),
            currency=currency,
        )

    def apply_sell(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        sell_qty: int,
        price: float,
        currency: str,
        session: str,
        reason: str,
        real_qty: int,
        can_execute_real: bool,
        created_at: str,
        reference_avg_price: float | None = None,
    ) -> dict:
        del real_qty
        virtual_buy = self.virtual_trades.get_position(market, symbol)
        virtual_buy_qty = 0 if virtual_buy is None else virtual_buy.qty
        virtual_buy_avg = 0.0 if virtual_buy is None else virtual_buy.avg_price

        remaining = sell_qty
        realized_pnl = 0.0
        from_virtual_buy = min(remaining, virtual_buy_qty)
        if from_virtual_buy > 0:
            sell_pnl, _ = self.virtual_trades.record_sell(
                market=market,
                symbol=symbol,
                exchange_code=exchange_code,
                qty=from_virtual_buy,
                fill_price=price,
                currency=currency,
                session=session,
                reason=reason,
                created_at=created_at,
            )
            realized_pnl += sell_pnl
            remaining -= from_virtual_buy

        if remaining <= 0:
            return {
                "realized_pnl": realized_pnl,
                "fully_virtual": True,
                "qty_from_real": 0,
                "qty_from_virtual_buy": from_virtual_buy,
                "qty_pending_real": 0,
                "virtual_buy_avg_price": virtual_buy_avg,
            }

        if can_execute_real:
            return {
                "realized_pnl": realized_pnl,
                "fully_virtual": False,
                "qty_from_real": remaining,
                "qty_from_virtual_buy": from_virtual_buy,
                "qty_pending_real": 0,
                "virtual_buy_avg_price": virtual_buy_avg,
            }

        existing = self.repository.get_virtual_sell_pending(market, symbol)
        existing_qty = 0 if existing is None else int(existing["qty"])
        existing_avg = 0.0 if existing is None else float(existing["avg_sell_price"])
        next_qty = existing_qty + remaining
        next_avg = price
        if next_qty > 0:
            next_avg = ((existing_avg * existing_qty) + (price * remaining)) / next_qty
        self.repository.upsert_virtual_sell_pending(
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            qty=next_qty,
            avg_sell_price=next_avg,
            currency=currency,
            updated_at=created_at,
        )
        pending_realized_pnl = 0.0
        pending_realized_pnl_pct = 0.0
        if reference_avg_price is not None and reference_avg_price > 0:
            pending_realized_pnl = (price - reference_avg_price) * remaining
            pending_realized_pnl_pct = (price - reference_avg_price) / reference_avg_price
        self.repository.save_virtual_order(
            created_at=created_at,
            market=market,
            symbol=symbol,
            exchange_code=exchange_code,
            side="sell",
            qty=remaining,
            fill_price=price,
            currency=currency,
            session=session,
            reason=reason,
            realized_pnl=pending_realized_pnl,
            realized_pnl_pct=pending_realized_pnl_pct,
        )
        return {
            "realized_pnl": realized_pnl + pending_realized_pnl,
            "fully_virtual": True,
            "qty_from_real": remaining,
            "qty_from_virtual_buy": from_virtual_buy,
            "qty_pending_real": remaining,
            "virtual_buy_avg_price": virtual_buy_avg,
        }

    def get_pending_settlement(self, market: str, symbol: str) -> tuple[int, float] | None:
        row = self.repository.get_virtual_sell_pending(market, symbol)
        if row is None:
            return None
        qty = int(row["qty"])
        if qty <= 0:
            return None
        return qty, float(row["avg_sell_price"])

    def settle(
        self,
        *,
        market: str,
        symbol: str,
        real_qty_after_settlement: int,
    ) -> None:
        del real_qty_after_settlement
        self.repository.delete_virtual_sell_pending(market, symbol)
