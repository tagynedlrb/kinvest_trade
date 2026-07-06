from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import KisRestClient
from .config import AppConfig
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .time_utils import format_kst


@dataclass(slots=True)
class QuoteSnapshot:
    stock_code: str
    last_price: int
    best_ask: int
    best_bid: int
    ask_size: int
    bid_size: int
    mid_price: float
    spread_pct: float
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class PaperPosition:
    stock_code: str
    qty: int
    avg_price: int
    peak_price: int
    opened_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class PaperRunState:
    run_id: int
    cash_krw: int
    realized_pnl_krw: int = 0


class PaperTradingService:
    """Runs a live-data, no-order paper trading loop.

    We intentionally use real market data plus virtual fills so the operator can
    validate signals, timing, and notification quality before any live order flow
    is enabled.
    """

    def __init__(
        self,
        config: AppConfig,
        client: KisRestClient,
        repository: SqliteRepository,
        notifier: TelegramNotifier,
    ) -> None:
        self.config = config
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.positions: dict[str, PaperPosition] = {}
        self.history: dict[str, deque[QuoteSnapshot]] = defaultdict(
            lambda: deque(maxlen=self.config.paper.history_window)
        )

    async def run(
        self,
        iterations: int | None = None,
        interval_sec: int | None = None,
        watchlist_override: list[str] | None = None,
    ) -> PaperRunState:
        watchlist = watchlist_override if watchlist_override is not None else self.config.trading.watchlist
        max_iterations = iterations if iterations is not None else self.config.paper.max_iterations
        poll_interval = interval_sec if interval_sec is not None else self.config.paper.poll_interval_sec
        state = PaperRunState(
            run_id=self.repository.create_paper_run(
                mode="KIS_REST_PAPER",
                watchlist=watchlist,
                starting_cash_krw=self.config.paper.starting_cash_krw,
                notes="paper trading using KIS domestic quote polling",
            ),
            cash_krw=self.config.paper.starting_cash_krw,
        )

        await self.notifier.send(
            f"[KIS][PAPER_START]\nrun_id={state.run_id}\nwatchlist={','.join(watchlist)}"
        )

        try:
            for step in range(max_iterations):
                for stock_code in watchlist:
                    snapshot = await self._fetch_snapshot(stock_code)
                    self.history[stock_code].append(snapshot)
                    self.repository.save_quote_snapshot(
                        state.run_id,
                        snapshot.captured_at.isoformat(),
                        snapshot.stock_code,
                        snapshot.best_ask,
                        snapshot.best_bid,
                        snapshot.ask_size,
                        snapshot.bid_size,
                        snapshot.mid_price,
                        snapshot.spread_pct,
                    )
                    await self._process_snapshot(state, snapshot)

                self.repository.save_heartbeat(
                    "PAPER_LOOP",
                    f"run_id={state.run_id} iteration={step + 1}/{max_iterations}",
                )
                if step + 1 < max_iterations:
                    await asyncio.sleep(poll_interval)
        finally:
            self.repository.finish_paper_run(
                state.run_id,
                status="FINISHED",
                ending_cash_krw=state.cash_krw,
                realized_pnl_krw=state.realized_pnl_krw,
                notes="paper run finished",
            )
            await self.notifier.send(
                f"[KIS][PAPER_END]\nrun_id={state.run_id}\nending_cash={state.cash_krw}\nrealized_pnl={state.realized_pnl_krw}"
            )

        return state

    async def _fetch_snapshot(self, stock_code: str) -> QuoteSnapshot:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        return QuoteSnapshot(
            stock_code=stock_code,
            last_price=current["current_price"],
            best_ask=orderbook["best_ask"],
            best_bid=orderbook["best_bid"],
            ask_size=orderbook["ask_size"],
            bid_size=orderbook["bid_size"],
            mid_price=orderbook["mid_price"] or float(current["current_price"]),
            spread_pct=orderbook["spread_pct"],
        )

    async def _process_snapshot(self, state: PaperRunState, snapshot: QuoteSnapshot) -> None:
        position = self.positions.get(snapshot.stock_code)
        if position is None:
            await self._maybe_buy(state, snapshot)
            return
        await self._maybe_sell(state, snapshot, position)

    async def _maybe_buy(self, state: PaperRunState, snapshot: QuoteSnapshot) -> None:
        history = list(self.history[snapshot.stock_code])
        if len(history) < self.config.paper.history_window:
            return

        earliest = history[0]
        latest = history[-1]
        if earliest.mid_price <= 0 or latest.mid_price <= 0 or latest.best_ask <= 0:
            return

        momentum = (latest.mid_price - earliest.mid_price) / earliest.mid_price
        bid_ask_ratio = latest.bid_size / latest.ask_size if latest.ask_size > 0 else 0.0

        if latest.spread_pct > self.config.auto_trade.max_spread_pct:
            return
        if momentum < self.config.paper.entry_trigger_pct:
            return
        if bid_ask_ratio < self.config.paper.min_bid_ask_ratio:
            return
        if len(self.positions) >= self.config.trading.max_positions:
            return

        budget = min(self.config.trading.max_position_value_krw, state.cash_krw)
        qty = budget // latest.best_ask
        if qty <= 0:
            return

        state.cash_krw -= qty * latest.best_ask
        now = latest.captured_at
        position = PaperPosition(
            stock_code=latest.stock_code,
            qty=qty,
            avg_price=latest.best_ask,
            peak_price=latest.best_ask,
            opened_at=now,
            updated_at=now,
        )
        self.positions[latest.stock_code] = position
        self.repository.upsert_paper_position(
            state.run_id,
            latest.stock_code,
            qty,
            latest.best_ask,
            latest.best_ask,
            now.isoformat(),
            now.isoformat(),
        )
        self.repository.save_paper_order(
            state.run_id,
            now.isoformat(),
            latest.stock_code,
            "BUY",
            qty,
            latest.best_ask,
            latest.best_ask,
            "FILLED",
            f"momentum={momentum:.5f},bid_ask_ratio={bid_ask_ratio:.3f}",
            realized_pnl_krw=0,
        )
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][PAPER]",
                    f"time={format_kst(now)}",
                    f"symbol={latest.stock_code}",
                    "action=BUY",
                    f"price={latest.best_ask} KRW",
                    f"qty={qty}",
                ]
            )
        )

    async def _maybe_sell(
        self,
        state: PaperRunState,
        snapshot: QuoteSnapshot,
        position: PaperPosition,
    ) -> None:
        if snapshot.best_bid <= 0:
            return

        position.peak_price = max(position.peak_price, snapshot.best_bid)
        position.updated_at = snapshot.captured_at

        pnl_pct = (snapshot.best_bid - position.avg_price) / position.avg_price
        exit_reason = ""
        if pnl_pct >= self.config.paper.take_profit_pct:
            exit_reason = "take_profit"
        elif pnl_pct <= -self.config.paper.stop_loss_pct:
            exit_reason = "stop_loss"
        elif snapshot.best_bid < position.peak_price * (
            1.0 - self.config.auto_trade.trailing_stop_pct
        ):
            exit_reason = "trailing_stop"

        if not exit_reason:
            self.repository.upsert_paper_position(
                state.run_id,
                snapshot.stock_code,
                position.qty,
                position.avg_price,
                position.peak_price,
                position.opened_at.isoformat(),
                snapshot.captured_at.isoformat(),
            )
            return

        realized_pnl = (snapshot.best_bid - position.avg_price) * position.qty
        state.cash_krw += snapshot.best_bid * position.qty
        state.realized_pnl_krw += realized_pnl
        self.repository.save_paper_order(
            state.run_id,
            snapshot.captured_at.isoformat(),
            snapshot.stock_code,
            "SELL",
            position.qty,
            snapshot.best_bid,
            snapshot.best_bid,
            "FILLED",
            exit_reason,
            realized_pnl_krw=realized_pnl,
        )
        self.repository.delete_paper_position(state.run_id, snapshot.stock_code)
        self.positions.pop(snapshot.stock_code, None)
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][PAPER]",
                    f"time={format_kst(snapshot.captured_at)}",
                    f"symbol={snapshot.stock_code}",
                    f"action=SELL ({exit_reason})",
                    f"price={snapshot.best_bid} KRW",
                    f"qty={position.qty}",
                    f"pnl={realized_pnl} KRW",
                ]
            )
        )
