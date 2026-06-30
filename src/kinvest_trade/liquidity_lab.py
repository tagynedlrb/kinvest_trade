from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .client import KisApiError, KisRestClient, parse_kis_number
from .config import AppConfig, OverseasCandidateConfig
from .market_sessions import (
    KST,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    is_us_regular_session,
)
from .message_format import (
    format_krw,
    format_side_korean,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_usd,
)
from .adaptive_params import apply_override, compute_adaptive_override
from .momentum_policy import (
    derive_watch_state,
    evaluate_entry_setup,
    evaluate_exit_setup,
)
from .notifier import TelegramNotifier
from .paper import PaperTradingService, PaperRunState
from .repository import SqliteRepository
from .technical_signals import (
    MovingAverageSnapshot,
    build_moving_average_snapshot,
    extract_price_series,
)
from .time_utils import format_kst, format_kst_korean


@dataclass(slots=True)
class DomesticScanResult:
    stock_code: str
    current_price: int
    best_ask: int
    best_bid: int
    spread_pct: float
    minute_change_pct: float
    intraday_turnover_krw: int
    volume_sum: int
    activity_score: float


@dataclass(slots=True)
class OverseasScanResult:
    symbol: str
    exchange_code: str
    last_price: float
    bid: float
    ask: float
    spread_pct: float
    change_rate_pct: float
    volume: int
    orderable_qty: int
    fx_rate_krw: float
    activity_score: float


@dataclass(slots=True)
class ExcludedCandidate:
    market: str
    code: str
    reasons: list[str]
    snapshot: dict


@dataclass(slots=True)
class VirtualPosition:
    market: str
    symbol: str
    exchange_code: str | None
    qty: int
    avg_price: float
    currency: str


@dataclass(slots=True)
class OverseasHeldPosition:
    symbol: str
    exchange_code: str
    quantity: int
    orderable_qty: int
    avg_price: float
    current_price: float
    pnl_pct: float
    is_virtual: bool = False


@dataclass(slots=True)
class DomesticHeldPosition:
    stock_code: str
    quantity: int
    orderable_qty: int
    avg_price: float
    current_price: float
    pnl_pct: float


@dataclass(slots=True)
class WatchTargetStatus:
    market: str
    code: str
    exchange_code: str | None
    price: float
    activity_score: float
    signal_score: float
    action_bias: str
    signal_state: str
    ma_summary: str
    note: str
    holding_qty: int = 0


@dataclass(slots=True)
class LiquidityLabReport:
    scanned_at: str
    krx_market_open: bool
    us_market_open: bool
    us_market_session: str
    us_orderable_in_profile: bool
    primary_market: str
    primary_target: str | None
    primary_selection_reason: str
    domestic_ranked: list[DomesticScanResult]
    overseas_ranked: list[OverseasScanResult]
    domestic_excluded: list[ExcludedCandidate]
    overseas_excluded: list[ExcludedCandidate]
    domestic_positions: list[DomesticHeldPosition]
    overseas_positions: list[OverseasHeldPosition]
    watch_targets: list[WatchTargetStatus]
    estimated_api_calls_per_cycle: int
    paper_run: dict | None
    domestic_order: dict | None
    overseas_order: dict | None

    def to_dict(self) -> dict:
        return {
            "scanned_at": self.scanned_at,
            "krx_market_open": self.krx_market_open,
            "us_market_open": self.us_market_open,
            "us_market_session": self.us_market_session,
            "us_orderable_in_profile": self.us_orderable_in_profile,
            "primary_market": self.primary_market,
            "primary_target": self.primary_target,
            "primary_selection_reason": self.primary_selection_reason,
            "domestic_ranked": [asdict(item) for item in self.domestic_ranked],
            "overseas_ranked": [asdict(item) for item in self.overseas_ranked],
            "domestic_excluded": [asdict(item) for item in self.domestic_excluded],
            "overseas_excluded": [asdict(item) for item in self.overseas_excluded],
            "domestic_positions": [asdict(item) for item in self.domestic_positions],
            "overseas_positions": [asdict(item) for item in self.overseas_positions],
            "watch_targets": [asdict(item) for item in self.watch_targets],
            "estimated_api_calls_per_cycle": self.estimated_api_calls_per_cycle,
            "paper_run": self.paper_run,
            "domestic_order": self.domestic_order,
            "overseas_order": self.overseas_order,
        }


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


class LiquidityLabService:
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
        self.virtual_trades = VirtualTradeManager(repository)
        self._domestic_excluded: list[ExcludedCandidate] = []
        self._overseas_excluded: list[ExcludedCandidate] = []
        self._last_held_symbols: set[str] = set()
        self._signal_cache: dict[str, MovingAverageSnapshot | None] = {}

    async def run(self) -> LiquidityLabReport:
        now = datetime.now(timezone.utc)
        krx_open = is_krx_regular_session(now)
        us_open = is_us_regular_session(now)
        us_session = get_us_trading_session(now)
        us_orderable_in_profile = is_us_orderable_session_for_env(
            now,
            self.config.credentials.env,
        )

        if not krx_open and not us_open:
            return LiquidityLabReport(
                scanned_at=format_kst(now) or "",
                krx_market_open=False,
                us_market_open=False,
                us_market_session=us_session,
                us_orderable_in_profile=False,
                primary_market="none",
                primary_target=None,
                primary_selection_reason="no_supported_market_open",
                domestic_ranked=[],
                overseas_ranked=[],
                domestic_excluded=[],
                overseas_excluded=[],
                domestic_positions=[],
                overseas_positions=[],
                watch_targets=[],
                estimated_api_calls_per_cycle=0,
                paper_run={"skipped": True, "reason": "market_closed"},
                domestic_order={"skipped": True, "reason": "market_closed"},
                overseas_order={"skipped": True, "reason": "market_closed"},
            )

        domestic_ranked = await self.scan_domestic() if krx_open else []
        domestic_positions = (
            await self._load_domestic_positions(domestic_ranked)
            if krx_open
            else []
        )
        if us_open:
            overseas_ranked, held_symbols_cache = await self.scan_overseas()
            overseas_positions = await self._load_overseas_positions(
                overseas_ranked,
                held_symbols_cache=held_symbols_cache,
            )
            virtual_overseas_positions = self._load_virtual_overseas_positions(overseas_ranked)
            monitored_overseas_positions = [
                *overseas_positions,
                *virtual_overseas_positions,
            ]
        else:
            overseas_ranked = []
            overseas_positions = []
            monitored_overseas_positions = []
        domestic_watch_targets = (
            await self._build_domestic_watch_targets(domestic_ranked, domestic_positions)
            if krx_open
            else []
        )
        overseas_watch_targets = (
            await self._build_overseas_watch_targets(overseas_ranked, monitored_overseas_positions)
            if us_open
            else []
        )
        overseas_exit_target = (
            await self._select_overseas_exit_target(overseas_ranked, monitored_overseas_positions)
            if us_open
            else None
        )
        domestic_exit_target = (
            self._select_domestic_exit_target(
                domestic_ranked,
                domestic_watch_targets,
                domestic_positions,
            )
            if krx_open
            else None
        )
        domestic_buy_target = self._select_domestic_buy_target(domestic_ranked, domestic_watch_targets)
        overseas_buy_target = self._select_overseas_buy_target(overseas_ranked, overseas_watch_targets)
        primary_market, primary_target, primary_reason = self._select_primary_target(
            krx_open=krx_open,
            us_open=us_open,
            us_orderable_in_profile=us_orderable_in_profile,
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
        )
        if domestic_exit_target is not None:
            exit_candidate, _, exit_reason, _ = domestic_exit_target
            primary_market = "domestic"
            primary_target = exit_candidate.stock_code
            primary_reason = f"existing_position_{exit_reason}"
        elif overseas_exit_target is not None:
            exit_candidate, _, exit_reason, _ = overseas_exit_target
            primary_market = "overseas"
            primary_target = exit_candidate.symbol
            primary_reason = f"existing_position_{exit_reason}"
        elif domestic_buy_target is not None:
            primary_market = "domestic"
            primary_target = domestic_buy_target.stock_code
            primary_reason = "watchlist_buy_signal"
        elif overseas_buy_target is not None and us_orderable_in_profile:
            primary_market = "overseas"
            primary_target = overseas_buy_target.symbol
            primary_reason = "watchlist_buy_signal"
        elif krx_open and domestic_watch_targets:
            primary_market = "domestic"
            primary_target = domestic_watch_targets[0].code
            primary_reason = "watchlist_wait"
        elif us_open and overseas_watch_targets:
            primary_market = "overseas"
            primary_target = overseas_watch_targets[0].code
            primary_reason = "watchlist_wait"

        paper_summary = None
        domestic_order = None
        overseas_order = None

        if primary_market == "domestic" and domestic_exit_target is not None:
            exit_candidate, held, exit_reason, exit_signal = domestic_exit_target
            paper_summary = {"skipped": True, "reason": "paper_test_removed_for_speed"}
            domestic_order = await self._place_domestic_sell_order(
                exit_candidate,
                held,
                exit_reason,
                exit_signal,
            )
        elif primary_market == "domestic" and domestic_buy_target is not None:
            paper_summary = {"skipped": True, "reason": "paper_test_removed_for_speed"}
            domestic_order = await self._place_domestic_test_order(domestic_buy_target)
        else:
            paper_summary = {
                "skipped": True,
                "reason": (
                    primary_reason
                    if primary_market != "domestic"
                    else "no_domestic_candidate"
                ),
            }
            domestic_order = {
                "skipped": True,
                "reason": (
                    primary_reason
                    if primary_market != "domestic"
                    else "no_domestic_candidate"
                ),
            }

        if primary_market == "overseas" and overseas_exit_target is not None:
            exit_candidate, exit_position, exit_reason, exit_signal = overseas_exit_target
            overseas_order = await self._place_overseas_sell_order(
                exit_candidate,
                exit_position,
                exit_reason,
                signal_snapshot=exit_signal,
            )
        elif (
            primary_market == "overseas"
            and overseas_buy_target is not None
            and us_orderable_in_profile
        ):
            overseas_order = await self._manage_overseas_position(
                candidate=overseas_buy_target,
                held_positions=overseas_positions,
            )
        elif (
            primary_market == "overseas"
            and overseas_buy_target is not None
            and not us_orderable_in_profile
        ):
            overseas_order = await self._record_virtual_overseas_buy(overseas_buy_target)
        else:
            overseas_order = {
                "skipped": True,
                "reason": (
                    primary_reason
                    if primary_market != "overseas"
                    else "no_overseas_candidate"
                ),
            }

        report = LiquidityLabReport(
            scanned_at=format_kst(now) or "",
            krx_market_open=krx_open,
            us_market_open=us_open,
            us_market_session=us_session,
            us_orderable_in_profile=us_orderable_in_profile,
            primary_market=primary_market,
            primary_target=primary_target,
            primary_selection_reason=primary_reason,
            domestic_ranked=domestic_ranked,
            overseas_ranked=overseas_ranked,
            domestic_excluded=self._domestic_excluded,
            overseas_excluded=self._overseas_excluded,
            domestic_positions=domestic_positions,
            overseas_positions=overseas_positions,
            watch_targets=domestic_watch_targets if krx_open else overseas_watch_targets,
            estimated_api_calls_per_cycle=self._estimate_api_calls_per_cycle(
                krx_open=krx_open,
                us_open=us_open,
                domestic_watch_count=len(domestic_watch_targets),
                overseas_watch_count=len(overseas_watch_targets),
                include_domestic_order=bool(domestic_exit_target or domestic_buy_target),
                include_overseas_order=bool(overseas_exit_target or overseas_buy_target),
            ),
            paper_run=paper_summary,
            domestic_order=domestic_order,
            overseas_order=overseas_order,
        )
        await self._send_summary(report)
        return report

    async def scan_domestic(self) -> list[DomesticScanResult]:
        config = self.config.liquidity_lab
        quote_results: list[DomesticScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for stock_code in config.domestic_candidates:
            try:
                candidate = await self._scan_single_domestic_quote(stock_code)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            reasons = self._domestic_quote_speculative_reasons(candidate)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=stock_code,
                        reasons=reasons,
                        snapshot=asdict(candidate),
                    )
                )
            else:
                quote_results.append(candidate)
            await asyncio.sleep(0.05)
        self._domestic_excluded = excluded
        if not quote_results:
            return []

        quote_results.sort(key=lambda item: item.activity_score, reverse=True)
        refine_n = min(len(quote_results), max(config.domestic_top_n * 2, 3))
        refined: list[DomesticScanResult] = []
        for candidate in quote_results[:refine_n]:
            try:
                full_candidate = await self._scan_single_domestic(candidate.stock_code)
            except Exception:
                refined.append(candidate)
                await asyncio.sleep(0.05)
                continue
            reasons = self._domestic_speculative_reasons(full_candidate)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="domestic",
                        code=full_candidate.stock_code,
                        reasons=reasons,
                        snapshot=asdict(full_candidate),
                    )
                )
            else:
                refined.append(full_candidate)
            await asyncio.sleep(0.05)

        self._domestic_excluded = excluded
        remaining = quote_results[refine_n:]
        return sorted(refined + remaining, key=lambda item: item.activity_score, reverse=True)

    async def _scan_single_domestic_quote(self, stock_code: str) -> DomesticScanResult:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        intraday_turnover = int(current.get("turnover_krw", 0) or 0)
        spread_pct = float(orderbook.get("spread_pct", 0.0) or 0.0)
        liquidity_score = math.log10(max(intraday_turnover, 1)) * 8.0
        spread_penalty = spread_pct * 3000.0
        turnover_surge_bonus = 0.0
        if intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 3:
            turnover_surge_bonus = 4.0
        elif intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 1.5:
            turnover_surge_bonus = 2.0

        activity_score = liquidity_score + turnover_surge_bonus - spread_penalty
        return DomesticScanResult(
            stock_code=stock_code,
            current_price=int(current["current_price"]),
            best_ask=int(orderbook["best_ask"]),
            best_bid=int(orderbook["best_bid"]),
            spread_pct=spread_pct,
            minute_change_pct=0.0,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=0,
            activity_score=round(activity_score, 4),
        )

    async def scan_overseas(self) -> tuple[list[OverseasScanResult], set[str]]:
        """
        Scan the overseas universe in a single pass per cycle.

        Step 1: fetch quotes for all candidates and compute activity score.
        Step 2: select top-N activity symbols plus any held symbol for signal loading.
        Step 3: load chart-based signals only for that reduced set and cache them.
        """
        config = self.config.liquidity_lab
        quote_results: list[OverseasScanResult] = []
        excluded: list[ExcludedCandidate] = []

        for candidate in config.overseas_candidates:
            try:
                scan_result = await self._scan_single_overseas(candidate)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            reasons = self._overseas_speculative_reasons(scan_result)
            if reasons:
                excluded.append(
                    ExcludedCandidate(
                        market="overseas",
                        code=candidate.symbol,
                        reasons=reasons,
                        snapshot=asdict(scan_result),
                    )
                )
            else:
                quote_results.append(scan_result)
            await asyncio.sleep(0.05)

        self._overseas_excluded = excluded
        if not quote_results:
            self._signal_cache.clear()
            return [], set()

        quote_results.sort(key=lambda item: item.activity_score, reverse=True)
        held_symbols = await self._get_held_symbols()
        top_n = max(1, config.overseas_scan_top_n)

        signal_symbols: set[str] = set()
        for result in quote_results:
            symbol = result.symbol.upper()
            if symbol in held_symbols:
                signal_symbols.add(symbol)

        remaining_slots = max(0, top_n - len(signal_symbols))
        for result in quote_results:
            if remaining_slots <= 0:
                break
            symbol = result.symbol.upper()
            if symbol in signal_symbols:
                continue
            signal_symbols.add(symbol)
            remaining_slots -= 1

        for result in quote_results:
            symbol = result.symbol.upper()
            if symbol not in signal_symbols:
                continue
            self._signal_cache[symbol] = await self._load_overseas_signal(result)
            await asyncio.sleep(0.05)

        for symbol in list(self._signal_cache.keys()):
            if symbol not in signal_symbols:
                del self._signal_cache[symbol]

        return quote_results, held_symbols

    async def _get_held_symbols(self) -> set[str]:
        """
        Return overseas symbols currently held.

        On API failure, fall back to the previous cycle cache so exit scans still include
        existing positions.
        """
        try:
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in self.config.liquidity_lab.overseas_candidates
            }
            held: set[str] = set(self._get_virtual_held_symbols())
            for exchange_code in sorted(exchange_codes):
                balance = await self.client.get_overseas_balance(
                    exchange_code=exchange_code,
                    currency_code="USD",
                )
                for row in balance.get("positions", []):
                    qty = int(float(str(row.get("ovrs_cblc_qty", 0) or 0)))
                    if qty <= 0:
                        continue
                    symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                    if symbol:
                        held.add(symbol)
            self._last_held_symbols = held
            return held
        except Exception:
            return self._last_held_symbols or self._get_virtual_held_symbols()

    def _get_virtual_held_symbols(self) -> set[str]:
        manager = getattr(self, "virtual_trades", None)
        if manager is None:
            return set()
        return {
            position.symbol.upper()
            for position in manager.list_positions("overseas")
            if position.qty > 0
        }

    async def _scan_single_domestic(self, stock_code: str) -> DomesticScanResult:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)
        target_date = datetime.now(timezone.utc).astimezone(KST).strftime("%Y%m%d")
        bars = await self.client.get_time_daily_chart(
            stock_code=stock_code,
            target_date=target_date,
            market_code=self.config.trading.market_code,
        )
        limited_bars = bars[: min(8, len(bars))]
        closes = [parse_kis_number(row.get("stck_prpr")) for row in limited_bars]
        volumes = [parse_kis_number(row.get("cntg_vol")) for row in limited_bars]
        earliest = closes[-1] if closes else 0
        latest = closes[0] if closes else current["current_price"]
        minute_change_pct = 0.0 if earliest <= 0 else (latest - earliest) / earliest
        intraday_turnover = int(current.get("turnover_krw", 0) or 0)
        volume_sum = sum(volumes)
        spread_pct = float(orderbook.get("spread_pct", 0.0) or 0.0)
        liquidity_score = math.log10(max(intraday_turnover, 1)) * 8.0
        volume_score = math.log10(max(volume_sum, 1)) * 4.0
        momentum_score = minute_change_pct * 300.0
        spread_penalty = spread_pct * 3000.0
        turnover_surge_bonus = 0.0
        if intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 3:
            turnover_surge_bonus = 4.0
        elif intraday_turnover >= self.config.liquidity_lab.domestic_min_intraday_turnover_krw * 1.5:
            turnover_surge_bonus = 2.0
        activity_score = (
            liquidity_score
            + volume_score
            + momentum_score
            + turnover_surge_bonus
            - spread_penalty
        )
        return DomesticScanResult(
            stock_code=stock_code,
            current_price=int(current["current_price"]),
            best_ask=int(orderbook["best_ask"]),
            best_bid=int(orderbook["best_bid"]),
            spread_pct=spread_pct,
            minute_change_pct=minute_change_pct,
            intraday_turnover_krw=intraday_turnover,
            volume_sum=volume_sum,
            activity_score=round(activity_score, 4),
        )

    async def _scan_single_overseas(
        self,
        candidate: OverseasCandidateConfig,
    ) -> OverseasScanResult:
        quote = await self.client.get_overseas_price(candidate.symbol, candidate.exchange_code)
        last_price = self._parse_float(quote.get("last_price"))
        bid = self._parse_float(quote.get("bid"))
        ask = self._parse_float(quote.get("ask"))
        volume = parse_kis_number(quote.get("volume"))
        change_rate = self._parse_float(quote.get("change_rate"))
        mid_price = (bid + ask) / 2 if bid > 0 and ask > 0 else float(last_price)
        spread_pct = 0.0
        if bid > 0 and ask > 0 and mid_price > 0:
            spread_pct = (ask - bid) / mid_price
        liquidity_score = math.log10(max(volume, 1)) * 6.0
        momentum_score = change_rate * 2.5
        spread_penalty = spread_pct * 2500.0
        volume_surge_bonus = 0.0
        if volume >= self.config.liquidity_lab.overseas_min_volume * 5:
            volume_surge_bonus = 3.0
        elif volume >= self.config.liquidity_lab.overseas_min_volume * 2:
            volume_surge_bonus = 1.5
        tight_spread_bonus = 1.0 if spread_pct < 0.001 else 0.0
        activity_score = (
            liquidity_score
            + momentum_score
            + volume_surge_bonus
            + tight_spread_bonus
            - spread_penalty
        )
        return OverseasScanResult(
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            change_rate_pct=change_rate,
            volume=volume,
            orderable_qty=0,
            fx_rate_krw=0.0,
            activity_score=round(activity_score, 4),
        )

    async def _load_overseas_positions(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_symbols_cache: set[str] | None = None,
    ) -> list[OverseasHeldPosition]:
        if not overseas_ranked:
            return []

        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        candidate_symbols = set(quote_map.keys())
        exchange_codes = {item.exchange_code.upper() for item in overseas_ranked}
        positions_by_key: dict[tuple[str, str], OverseasHeldPosition] = {}

        for exchange_code in sorted(exchange_codes):
            try:
                balance = await self.client.get_overseas_balance(
                    exchange_code=exchange_code,
                    currency_code="USD",
                )
            except Exception:
                continue

            for row in balance.get("positions", []):
                symbol = str(row.get("ovrs_pdno", "")).strip().upper()
                if symbol not in candidate_symbols:
                    continue
                row_exchange_code = str(row.get("ovrs_excg_cd", "")).strip().upper() or exchange_code
                quantity = parse_kis_number(row.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue
                orderable_qty = parse_kis_number(row.get("ord_psbl_qty"))
                avg_price = self._parse_float(row.get("pchs_avg_pric"))
                quote = quote_map.get(symbol)
                if quote is None or avg_price <= 0:
                    continue
                current_price = quote.last_price
                pnl_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
                positions_by_key[(symbol, row_exchange_code)] = OverseasHeldPosition(
                    symbol=symbol,
                    exchange_code=row_exchange_code,
                    quantity=quantity,
                    orderable_qty=orderable_qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    is_virtual=False,
                )

        return list(positions_by_key.values())

    def _load_virtual_overseas_positions(
        self,
        overseas_ranked: list[OverseasScanResult],
    ) -> list[OverseasHeldPosition]:
        manager = getattr(self, "virtual_trades", None)
        if not overseas_ranked or manager is None:
            return []

        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        positions: list[OverseasHeldPosition] = []
        for position in manager.list_positions("overseas"):
            quote = quote_map.get(position.symbol.upper())
            if quote is None or position.qty <= 0:
                continue
            pnl_pct = (
                (quote.last_price - position.avg_price) / position.avg_price
                if position.avg_price > 0
                else 0.0
            )
            positions.append(
                OverseasHeldPosition(
                    symbol=position.symbol.upper(),
                    exchange_code=(position.exchange_code or quote.exchange_code).upper(),
                    quantity=position.qty,
                    orderable_qty=position.qty,
                    avg_price=position.avg_price,
                    current_price=quote.last_price,
                    pnl_pct=pnl_pct,
                    is_virtual=True,
                )
            )
        return positions

    async def _load_domestic_positions(
        self,
        domestic_ranked: list[DomesticScanResult],
    ) -> list[DomesticHeldPosition]:
        if not domestic_ranked:
            return []

        quote_map = {item.stock_code: item for item in domestic_ranked}
        try:
            balance = await self.client.get_balance()
        except Exception:
            return []

        positions: list[DomesticHeldPosition] = []
        rows = balance.get("positions", []) or balance.get("output1", [])
        for row in rows:
            qty = int(float(str(row.get("hldg_qty", 0) or 0)))
            if qty <= 0:
                continue
            stock_code = str(row.get("pdno", "")).strip()
            if not stock_code:
                continue
            avg_price = self._parse_float(row.get("pchs_avg_pric"))
            orderable_qty = int(float(str(row.get("ord_psbl_qty", qty) or qty)))
            quote = quote_map.get(stock_code)
            current_price = quote.current_price if quote is not None else avg_price
            pnl_pct = (
                (current_price - avg_price) / avg_price
                if avg_price > 0
                else 0.0
            )
            positions.append(
                DomesticHeldPosition(
                    stock_code=stock_code,
                    quantity=qty,
                    orderable_qty=orderable_qty,
                    avg_price=avg_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                )
            )
        return positions

    async def _select_overseas_exit_target(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> tuple[OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot | None] | None:
        if not overseas_ranked or not held_positions:
            return None

        config = self.config.liquidity_lab
        quote_map = {item.symbol.upper(): item for item in overseas_ranked}
        selected: tuple[
            tuple[int, float],
            OverseasScanResult,
            OverseasHeldPosition,
            str,
            MovingAverageSnapshot | None,
        ] | None = None

        for held in held_positions:
            quote = quote_map.get(held.symbol.upper())
            if quote is None:
                continue
            if held.orderable_qty <= 0:
                continue

            exit_reason: str | None = None
            priority: tuple[int, float] | None = None
            if held.pnl_pct <= -config.overseas_stop_loss_pct:
                exit_reason = "stop_loss"
                priority = (0, held.pnl_pct)
            elif held.pnl_pct >= config.overseas_take_profit_pct:
                exit_reason = "take_profit"
                priority = (1, -held.pnl_pct)

            if exit_reason is None or priority is None:
                continue
            if selected is None or priority < selected[0]:
                selected = (priority, quote, held, exit_reason, None)

        if selected is not None:
            _, quote, held, exit_reason, signal_snapshot = selected
            return quote, held, exit_reason, signal_snapshot

        signal_candidates: list[tuple[float, OverseasScanResult, OverseasHeldPosition, str, MovingAverageSnapshot]] = []
        for held in held_positions:
            quote = quote_map.get(held.symbol.upper())
            if quote is None or held.orderable_qty <= 0:
                continue
            signal_snapshot = self._signal_cache.get(held.symbol.upper())
            if signal_snapshot is None:
                continue
            should_exit, exit_reason = self._should_exit_overseas_position(signal_snapshot, held)
            if not should_exit:
                continue
            signal_candidates.append((held.pnl_pct, quote, held, exit_reason, signal_snapshot))

        if not signal_candidates:
            return None

        signal_candidates.sort(key=lambda item: item[0])
        _, quote, held, exit_reason, signal_snapshot = signal_candidates[0]
        return quote, held, exit_reason, signal_snapshot

    def _domestic_speculative_reasons(self, candidate: DomesticScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        if candidate.current_price < config.domestic_min_price_krw:
            reasons.append("low_price_krw")
        if candidate.intraday_turnover_krw < config.domestic_min_intraday_turnover_krw:
            reasons.append("thin_intraday_turnover")
        if candidate.volume_sum < config.domestic_min_volume_sum:
            reasons.append("thin_recent_volume")
        if candidate.spread_pct > config.domestic_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    def _domestic_quote_speculative_reasons(self, candidate: DomesticScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        if candidate.current_price < config.domestic_min_price_krw:
            reasons.append("low_price_krw")
        if candidate.intraday_turnover_krw < config.domestic_min_intraday_turnover_krw:
            reasons.append("thin_intraday_turnover")
        if candidate.spread_pct > config.domestic_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    def _overseas_speculative_reasons(self, candidate: OverseasScanResult) -> list[str]:
        config = self.config.liquidity_lab
        reasons: list[str] = []
        if candidate.last_price < config.overseas_min_price_usd:
            reasons.append("low_price_usd")
        if candidate.volume < config.overseas_min_volume:
            reasons.append("thin_volume")
        if candidate.spread_pct > config.overseas_max_spread_pct:
            reasons.append("wide_spread")
        return reasons

    async def _build_domestic_watch_targets(
        self,
        domestic_ranked: list[DomesticScanResult],
        held_positions: list[DomesticHeldPosition],
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        held_map = {position.stock_code: position for position in held_positions}
        for candidate in domestic_ranked[: self.config.liquidity_lab.domestic_top_n]:
            signal_snapshot = await self._load_domestic_signal(candidate)
            held = held_map.get(candidate.stock_code)
            watch_targets.append(
                self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=0 if held is None else held.quantity,
                )
            )
            await asyncio.sleep(0.05)

        watched_codes = {watch_target.code for watch_target in watch_targets}
        for held in held_positions:
            if held.stock_code in watched_codes:
                continue
            candidate = next(
                (item for item in domestic_ranked if item.stock_code == held.stock_code),
                None,
            )
            if candidate is None:
                continue
            signal_snapshot = await self._load_domestic_signal(candidate)
            watch_targets.append(
                self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=held.quantity,
                )
            )
            await asyncio.sleep(0.05)
        return watch_targets

    async def _build_overseas_watch_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        held_map = {position.symbol.upper(): position for position in held_positions}
        cached_symbols = set(self._signal_cache.keys())

        for candidate in overseas_ranked:
            if candidate.symbol.upper() not in cached_symbols:
                continue
            signal_snapshot = self._signal_cache.get(candidate.symbol.upper())
            held = held_map.get(candidate.symbol.upper())
            watch_targets.append(
                self._build_watch_target_status(
                    market="overseas",
                    code=candidate.symbol,
                    exchange_code=candidate.exchange_code,
                    price=candidate.last_price,
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    held_position=held,
                    holding_qty=0 if held is None else held.quantity,
                )
            )
        return watch_targets

    def _build_watch_target_status(
        self,
        *,
        market: str,
        code: str,
        exchange_code: str | None,
        price: float,
        activity_score: float,
        signal_snapshot: MovingAverageSnapshot | None,
        held_position: OverseasHeldPosition | DomesticHeldPosition | None = None,
        holding_qty: int = 0,
    ) -> WatchTargetStatus:
        if signal_snapshot is None:
            return WatchTargetStatus(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=0.0,
                action_bias="WAIT",
                signal_state="WARMUP",
                ma_summary="-",
                note="signal_unavailable",
                holding_qty=holding_qty,
            )

        if held_position is not None:
            exit_setup = self._build_exit_setup(signal_snapshot, held_position.pnl_pct, holding_qty)
            if exit_setup.action in {"sell", "sell_partial"}:
                return WatchTargetStatus(
                    market=market,
                    code=code,
                    exchange_code=exchange_code,
                    price=price,
                    activity_score=activity_score,
                    signal_score=0.0,
                    action_bias="SELL",
                    signal_state="SELL_READY",
                    ma_summary=self._ma_relation_summary(signal_snapshot),
                    note=exit_setup.reason,
                    holding_qty=holding_qty,
                )
            return WatchTargetStatus(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=0.0,
                action_bias="WAIT",
                signal_state="HOLD",
                ma_summary=self._ma_relation_summary(signal_snapshot),
                note=exit_setup.note,
                holding_qty=holding_qty,
            )

        entry_setup = evaluate_entry_setup(self.config.auto_trade, signal_snapshot)
        if entry_setup.ready:
            return WatchTargetStatus(
                market=market,
                code=code,
                exchange_code=exchange_code,
                price=price,
                activity_score=activity_score,
                signal_score=entry_setup.score,
                action_bias="BUY",
                signal_state=entry_setup.state,
                ma_summary=self._ma_relation_summary(signal_snapshot),
                note=entry_setup.reason,
                holding_qty=holding_qty,
            )
        signal_state, note = derive_watch_state(self.config.auto_trade, signal_snapshot)
        return WatchTargetStatus(
            market=market,
            code=code,
            exchange_code=exchange_code,
            price=price,
            activity_score=activity_score,
            signal_score=entry_setup.score,
            action_bias="WAIT",
            signal_state=signal_state,
            ma_summary=self._ma_relation_summary(signal_snapshot),
            note=note,
            holding_qty=holding_qty,
        )

    def _select_domestic_buy_target(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
    ) -> DomesticScanResult | None:
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic" and watch_target.action_bias == "BUY"
        ]
        if not ready_targets:
            return None
        best_target = max(
            ready_targets,
            key=lambda item: (item.signal_score, item.activity_score),
        )
        return candidate_map.get(best_target.code)

    def _select_domestic_exit_target(
        self,
        domestic_ranked: list[DomesticScanResult],
        watch_targets: list[WatchTargetStatus],
        held_positions: list[DomesticHeldPosition],
    ) -> tuple[DomesticScanResult, DomesticHeldPosition, str, MovingAverageSnapshot | None] | None:
        candidate_map = {candidate.stock_code: candidate for candidate in domestic_ranked}
        held_map = {position.stock_code: position for position in held_positions}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "domestic" and watch_target.action_bias == "SELL"
        ]
        if not ready_targets:
            return None
        best_target = min(
            ready_targets,
            key=lambda item: held_map.get(item.code).pnl_pct if item.code in held_map else 0.0,
        )
        candidate = candidate_map.get(best_target.code)
        held = held_map.get(best_target.code)
        if candidate is None or held is None:
            return None
        return candidate, held, best_target.note, None

    def _select_overseas_buy_target(
        self,
        overseas_ranked: list[OverseasScanResult],
        watch_targets: list[WatchTargetStatus],
    ) -> OverseasScanResult | None:
        candidate_map = {candidate.symbol: candidate for candidate in overseas_ranked}
        ready_targets = [
            watch_target
            for watch_target in watch_targets
            if watch_target.market == "overseas" and watch_target.action_bias == "BUY"
        ]
        if not ready_targets:
            return None
        best_target = max(
            ready_targets,
            key=lambda item: (item.signal_score, item.activity_score),
        )
        return candidate_map.get(best_target.code)

    @staticmethod
    def _select_primary_target(
        *,
        krx_open: bool,
        us_open: bool,
        us_orderable_in_profile: bool,
        domestic_ranked: list[DomesticScanResult],
        overseas_ranked: list[OverseasScanResult],
    ) -> tuple[str, str | None, str]:
        if krx_open and domestic_ranked:
            return "domestic", domestic_ranked[0].stock_code, "highest_current_activity_in_open_market"
        if us_orderable_in_profile and overseas_ranked:
            return "overseas", overseas_ranked[0].symbol, "highest_current_activity_in_open_market"
        if krx_open:
            return "domestic", None, "krx_open_but_no_candidate"
        if us_orderable_in_profile:
            return "overseas", None, "us_open_but_no_candidate"
        if us_open:
            return "none", None, "us_open_but_mock_session_not_supported"
        return "none", None, "no_supported_market_open"

    async def _run_domestic_paper_test(self, watchlist: list[str]) -> PaperRunState:
        service = PaperTradingService(self.config, self.client, self.repository, self.notifier)
        return await service.run(
            iterations=self.config.liquidity_lab.domestic_paper_iterations,
            interval_sec=self.config.liquidity_lab.domestic_paper_interval_sec,
            watchlist_override=watchlist,
        )

    async def _place_domestic_test_order(self, candidate: DomesticScanResult) -> dict:
        qty = self.config.liquidity_lab.domestic_test_order_qty
        if qty <= 0:
            return {"skipped": True, "reason": "domestic_test_order_qty_zero"}
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
            }
        try:
            response = await self.client.place_cash_order(
                side="buy",
                stock_code=candidate.stock_code,
                qty=qty,
                price=candidate.best_ask or candidate.current_price,
                order_division="00",
            )
        except KisApiError as exc:
            return {
                "submitted": False,
                "skipped": True,
                "market": "domestic",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "order_rejected",
                "error": str(exc),
            }
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][LIQUIDITY_LAB]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"시장={format_market_korean('domestic')}",
                    f"종목={candidate.stock_code}",
                    f"동작={format_side_korean('buy')}",
                    f"가격={int(candidate.best_ask or candidate.current_price):,}원",
                    f"수량={qty}주",
                    "지표=RSI -, 거래량 -",
                    "사유=거래량 돌파 진입",
                ]
            )
        )
        return {
            "submitted": True,
            "market": "domestic",
            "side": "buy",
            "candidate": asdict(candidate),
            "qty": qty,
            "response": response,
        }

    async def _place_domestic_sell_order(
        self,
        candidate: DomesticScanResult,
        held: DomesticHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "market": "domestic",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "reason": "dry_run_enabled",
                "exit_reason": exit_reason,
            }

        try:
            sell_qty = min(held.quantity, max(held.orderable_qty, 0))
            response = await self.client.place_cash_order(
                side="sell",
                stock_code=candidate.stock_code,
                qty=sell_qty,
                price=candidate.best_bid or candidate.current_price,
                order_division="00",
            )
        except KisApiError as exc:
            return {
                "submitted": False,
                "skipped": True,
                "market": "domestic",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "reason": "order_rejected",
                "error": str(exc),
            }

        lines = [
            "[KIS][LAB_SELL]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"시장={format_market_korean('domestic')}",
            f"종목={candidate.stock_code}",
            "구분=매도",
            f"가격={format_krw(candidate.current_price)}",
            f"수량={sell_qty}주",
            f"사유={format_reason_korean(exit_reason)}",
        ]
        if held.avg_price > 0:
            gross_pnl = (candidate.current_price - held.avg_price) * sell_qty
            pnl_pct = (candidate.current_price - held.avg_price) / held.avg_price
            lines.append(f"매입가={format_krw(held.avg_price)}")
            lines.append(f"손익={format_krw(gross_pnl)}")
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("매입가=알수없음")
            lines.append("손익=알수없음")
        await self.notifier.send("\n".join(lines))

        return {
            "submitted": True,
            "market": "domestic",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": sell_qty,
            "exit_reason": exit_reason,
            "response": response,
        }

    async def _place_overseas_test_order(self, candidate: OverseasScanResult) -> dict:
        signal_snapshot = self._signal_cache.get(candidate.symbol.upper())
        if signal_snapshot is None:
            signal_snapshot = await self._load_overseas_signal(candidate)
            self._signal_cache[candidate.symbol.upper()] = signal_snapshot
        if signal_snapshot is None:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "wait",
                "candidate": asdict(candidate),
                "reason": "signal_snapshot_unavailable",
            }

        should_buy, buy_reason = self._should_buy_overseas_candidate(signal_snapshot)
        if not should_buy:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "wait",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
                "reason": buy_reason,
            }

        qty = self.config.liquidity_lab.overseas_test_order_qty
        if qty <= 0:
            return {"skipped": True, "reason": "overseas_test_order_qty_zero"}
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "side": "buy",
                "reason": "dry_run_enabled",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
            }
        try:
            response = await self.client.place_overseas_order_for_current_session(
                side="buy",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=qty,
                price=f"{candidate.last_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            if self._is_mock_us_session_blocked_error(str(exc)):
                return await self._record_virtual_overseas_buy(
                    candidate,
                    signal_snapshot=signal_snapshot,
                    rejected_error=str(exc),
                )
            return {
                "submitted": False,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "signal_snapshot": asdict(signal_snapshot),
                "qty": qty,
                "error": str(exc),
            }
        return {
            "submitted": True,
            "market": "overseas",
            "side": "buy",
            "candidate": asdict(candidate),
            "signal_snapshot": asdict(signal_snapshot),
            "qty": qty,
            "reason": buy_reason,
            "response": response,
        }

    async def _manage_overseas_position(
        self,
        *,
        candidate: OverseasScanResult,
        held_positions: list[OverseasHeldPosition],
    ) -> dict:
        config = self.config.liquidity_lab
        held_map = {item.symbol.upper(): item for item in held_positions}
        held = held_map.get(candidate.symbol.upper())

        if held is not None:
            if held.orderable_qty <= 0:
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "hold",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "reason": "pending_exit_order",
                }
            if held.quantity >= config.overseas_max_position_qty:
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "hold",
                    "candidate": asdict(candidate),
                    "held_position": asdict(held),
                    "reason": "already_holding_max_qty_waiting_for_exit",
                }

        return await self._place_overseas_test_order(candidate)

    async def _record_virtual_overseas_buy(
        self,
        candidate: OverseasScanResult,
        *,
        signal_snapshot: MovingAverageSnapshot | None = None,
        rejected_error: str | None = None,
    ) -> dict:
        qty = int(self.config.liquidity_lab.overseas_test_order_qty)
        if qty <= 0:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "overseas_test_order_qty_zero",
            }

        now = datetime.now(timezone.utc)
        session = get_us_trading_session(now)
        created_at = format_kst(now) or now.isoformat()
        snapshot = signal_snapshot or self._signal_cache.get(candidate.symbol.upper())
        position = self.virtual_trades.record_buy(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            qty=qty,
            fill_price=candidate.last_price,
            currency="USD",
            session=session,
            reason="session_not_orderable_in_profile",
            created_at=created_at,
        )
        if position is None:
            return {
                "submitted": False,
                "market": "overseas",
                "side": "buy",
                "candidate": asdict(candidate),
                "reason": "virtual_buy_record_failed",
            }

        lines = [
            "[KIS][VIRTUAL_TRADE]",
            f"시각={format_kst_korean(now)}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol} (virtual)",
            "구분=매수 (virtual)",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={qty}주",
            f"세션={session}",
            "사유=거래불가 세션 가상체결",
        ]
        if rejected_error:
            lines.append(f"참고={rejected_error}")
        await self.notifier.send("\n".join(lines))
        return {
            "submitted": True,
            "virtual": True,
            "market": "overseas",
            "side": "buy",
            "candidate": asdict(candidate),
            "signal_snapshot": None if snapshot is None else asdict(snapshot),
            "qty": qty,
            "reason": "session_not_orderable_in_profile",
            "session": session,
            "virtual_position": asdict(position),
        }

    async def _place_overseas_sell_order(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
        if held.is_virtual:
            return await self._record_virtual_overseas_sell(
                candidate,
                held,
                exit_reason,
                signal_snapshot=signal_snapshot,
            )
        if self.config.credentials.dry_run:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "reason": "dry_run_enabled",
                "exit_reason": exit_reason,
            }

        try:
            sell_qty = min(held.quantity, max(held.orderable_qty, 0))
            response = await self.client.place_overseas_order_for_current_session(
                side="sell",
                symbol=candidate.symbol,
                exchange_code=candidate.exchange_code,
                qty=sell_qty,
                price=f"{candidate.last_price:.4f}",
                order_division="00",
            )
        except KisApiError as exc:
            is_session_blocked = self._is_mock_us_session_blocked_error(str(exc))
            if is_session_blocked and is_us_regular_session(datetime.now(timezone.utc)):
                return await self._record_virtual_overseas_sell(
                    candidate,
                    held,
                    exit_reason,
                    signal_snapshot=signal_snapshot,
                    rejected_error=str(exc),
                )
            return {
                "submitted": False,
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "reason": "session_not_orderable_in_profile" if is_session_blocked else "order_rejected",
                "error": str(exc),
            }

        lines = [
            "[KIS][LAB_SELL]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol}",
            "구분=매도",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={sell_qty}주",
            f"사유={format_reason_korean(exit_reason)}",
        ]
        if held.avg_price > 0:
            gross_pnl = (candidate.last_price - held.avg_price) * sell_qty
            pnl_pct = (candidate.last_price - held.avg_price) / held.avg_price
            lines.append(f"매입가={format_usd(held.avg_price)}")
            lines.append(f"손익={format_usd(gross_pnl)}")
            lines.append(f"수익률={format_pct(pnl_pct)}")
        else:
            lines.append("매입가=알수없음")
            lines.append("손익=알수없음")
            lines.append("수익률=알수없음")
        await self.notifier.send("\n".join(lines))

        return {
            "submitted": True,
            "market": "overseas",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": sell_qty,
            "exit_reason": exit_reason,
            "response": response,
        }

    async def _record_virtual_overseas_sell(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        *,
        signal_snapshot: MovingAverageSnapshot | None = None,
        rejected_error: str | None = None,
    ) -> dict:
        sell_qty = min(held.quantity, max(held.orderable_qty, 0))
        if sell_qty <= 0:
            return {
                "skipped": True,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "reason": "no_orderable_qty",
                "virtual": True,
            }

        now = datetime.now(timezone.utc)
        session = get_us_trading_session(now)
        created_at = format_kst(now) or now.isoformat()
        realized_pnl, realized_pnl_pct = self.virtual_trades.record_sell(
            market="overseas",
            symbol=candidate.symbol,
            exchange_code=candidate.exchange_code,
            qty=sell_qty,
            fill_price=candidate.last_price,
            currency="USD",
            session=session,
            reason=exit_reason,
            created_at=created_at,
            seed_avg_price=None if held.is_virtual else held.avg_price,
            seed_qty=None if held.is_virtual else held.quantity,
        )

        lines = [
            "[KIS][VIRTUAL_TRADE]",
            f"시각={format_kst_korean(now)}",
            f"시장={format_market_korean('overseas')}",
            f"종목={candidate.symbol} (virtual)",
            "구분=매도 (virtual)",
            f"가격={format_usd(candidate.last_price)}",
            f"수량={sell_qty}주",
            f"사유={format_reason_korean(exit_reason)}",
            f"손익={format_usd(realized_pnl)}",
            f"수익률={format_pct(realized_pnl_pct)}",
        ]
        if rejected_error:
            lines.append("참고=실매도거부를 가상체결로 전환")
        await self.notifier.send("\n".join(lines))
        return {
            "submitted": True,
            "virtual": True,
            "market": "overseas",
            "side": "sell",
            "candidate": asdict(candidate),
            "held_position": asdict(held),
            "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
            "qty": sell_qty,
            "exit_reason": exit_reason,
            "reason": "session_not_orderable_in_profile" if rejected_error else exit_reason,
            "session": session,
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
        }

    async def _load_overseas_signal(
        self,
        candidate: OverseasScanResult,
    ) -> MovingAverageSnapshot | None:
        try:
            daily_rows = await self.client.get_overseas_daily_prices(
                candidate.symbol,
                candidate.exchange_code,
                adjusted_price=True,
            )
            minute_rows = await self.client.get_overseas_minute_chart(
                candidate.symbol,
                candidate.exchange_code,
                interval_minutes=self.config.auto_trade.intraday_bar_minutes,
                include_previous_day=True,
                record_count=max(
                    self.config.auto_trade.intraday_slow_window + 8,
                    self.config.auto_trade.breakout_lookback_bars + 6,
                    self.config.auto_trade.bollinger_window + 4,
                    self.config.auto_trade.atr_window + 4,
                    40,
                ),
            )
        except KisApiError:
            return None

        daily_series = extract_price_series(daily_rows, close_fields=("clos", "close", "last"))
        minute_series = extract_price_series(
            minute_rows,
            close_fields=("last", "clos", "close"),
            high_fields=("high",),
            low_fields=("low",),
            volume_fields=("evol", "volume"),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        if (
            len(daily_closes) < self.config.auto_trade.daily_slow_window
            or len(minute_closes) < self.config.auto_trade.intraday_slow_window
        ):
            return None

        return build_moving_average_snapshot(
            price=candidate.last_price,
            bid=candidate.bid,
            ask=candidate.ask,
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=self.config.auto_trade.daily_fast_window,
            daily_slow_window=self.config.auto_trade.daily_slow_window,
            intraday_fast_window=self.config.auto_trade.intraday_fast_window,
            intraday_slow_window=self.config.auto_trade.intraday_slow_window,
            volatility_window=self.config.auto_trade.volatility_window,
            momentum_window=self.config.auto_trade.momentum_window,
            volume_window=self.config.auto_trade.volume_window,
            breakout_lookback_bars=self.config.auto_trade.breakout_lookback_bars,
            bollinger_window=self.config.auto_trade.bollinger_window,
            bollinger_stddev=self.config.auto_trade.bollinger_stddev,
            atr_window=self.config.auto_trade.atr_window,
        )

    async def _load_domestic_signal(
        self,
        candidate: DomesticScanResult,
    ) -> MovingAverageSnapshot | None:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        target_date = now_kst.strftime("%Y%m%d")
        start_date = (now_kst - timedelta(days=200)).strftime("%Y%m%d")
        try:
            daily_rows = await self.client.get_daily_chart(
                stock_code=candidate.stock_code,
                start_date=start_date,
                end_date=target_date,
                market_code=self.config.trading.market_code,
            )
            minute_rows = await self.client.get_time_daily_chart(
                stock_code=candidate.stock_code,
                target_date=target_date,
                market_code=self.config.trading.market_code,
            )
        except KisApiError:
            return None

        daily_series = extract_price_series(
            daily_rows,
            close_fields=("stck_clpr", "stck_prpr"),
        )
        minute_series = extract_price_series(
            minute_rows,
            close_fields=("stck_prpr",),
            high_fields=("stck_hgpr",),
            low_fields=("stck_lwpr",),
            volume_fields=("cntg_vol",),
        )
        daily_closes = daily_series.closes
        minute_closes = minute_series.closes
        if (
            len(daily_closes) < self.config.auto_trade.daily_slow_window
            or len(minute_closes) < self.config.auto_trade.intraday_slow_window
        ):
            return None

        return build_moving_average_snapshot(
            price=float(candidate.current_price),
            bid=float(candidate.best_bid),
            ask=float(candidate.best_ask),
            daily_closes=daily_closes,
            minute_closes=minute_closes,
            minute_highs=minute_series.highs,
            minute_lows=minute_series.lows,
            minute_volumes=minute_series.volumes,
            daily_fast_window=self.config.auto_trade.daily_fast_window,
            daily_slow_window=self.config.auto_trade.daily_slow_window,
            intraday_fast_window=self.config.auto_trade.intraday_fast_window,
            intraday_slow_window=self.config.auto_trade.intraday_slow_window,
            volatility_window=self.config.auto_trade.volatility_window,
            momentum_window=self.config.auto_trade.momentum_window,
            volume_window=self.config.auto_trade.volume_window,
            breakout_lookback_bars=self.config.auto_trade.breakout_lookback_bars,
            bollinger_window=self.config.auto_trade.bollinger_window,
            bollinger_stddev=self.config.auto_trade.bollinger_stddev,
            atr_window=self.config.auto_trade.atr_window,
        )

    def _should_buy_overseas_candidate(
        self,
        snapshot: MovingAverageSnapshot,
    ) -> tuple[bool, str]:
        return self._should_buy_signal(snapshot)

    def _should_buy_signal(
        self,
        snapshot: MovingAverageSnapshot,
    ) -> tuple[bool, str]:
        _override = compute_adaptive_override(self.config.auto_trade, snapshot)
        effective_config = apply_override(self.config.auto_trade, _override)
        entry_setup = evaluate_entry_setup(effective_config, snapshot)
        return entry_setup.ready, entry_setup.reason

    def _should_exit_overseas_position(
        self,
        snapshot: MovingAverageSnapshot,
        held: OverseasHeldPosition,
    ) -> tuple[bool, str]:
        return self._should_exit_position(snapshot, held.pnl_pct)

    def _should_exit_position(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
    ) -> tuple[bool, str]:
        exit_setup = self._build_exit_setup(snapshot, pnl_pct, 1)
        return exit_setup.action in {"sell", "sell_partial"}, exit_setup.reason

    @staticmethod
    def _is_mock_us_session_blocked_error(message: str) -> bool:
        return (
            "미국주식 주간거래는 제공하지 않습니다" in message
            or "KIS mock currently supports US order tests only during the US regular session" in message
            or "does not support US daytime trading" in message
        )

    async def _send_summary(self, report: LiquidityLabReport) -> None:
        action = self._build_action_summary(report)
        if action["action_raw"] in {"WAIT", "VIRTUAL_BUY", "VIRTUAL_SELL"}:
            return
        session_note = ""
        if report.primary_market == "overseas" and not report.us_orderable_in_profile:
            session_note = " (거래불가 세션)"
        lines = [
            "[KIS][LIQUIDITY_LAB]",
            f"시각={self._format_report_time(report.scanned_at)}",
            f"시장={format_market_korean(report.primary_market)}{session_note}",
            f"종목={report.primary_target or '-'}",
            f"동작={action['action']}",
            f"가격={action['price']}",
            f"수량={action['qty']}",
            f"지표={action['indicator']}",
            f"사유={action['reason']}",
        ]
        if action["action_raw"] == "SELL_REJECTED":
            lines.append("참고=주문이 거부되어 실제로 체결되지 않았습니다")
        await self.notifier.send("\n".join(lines))

    def _build_action_summary(self, report: LiquidityLabReport) -> dict[str, str]:
        overseas_order = report.overseas_order or {}
        domestic_order = report.domestic_order or {}
        if overseas_order.get("submitted"):
            return self._format_order_summary(overseas_order, currency="USD")
        if domestic_order.get("submitted"):
            return self._format_order_summary(domestic_order, currency="KRW")
        if report.primary_market == "overseas" and (
            overseas_order.get("skipped") or overseas_order.get("error")
        ):
            return self._format_order_summary(overseas_order, currency="USD")
        if report.primary_market == "domestic" and (
            domestic_order.get("skipped") or domestic_order.get("error")
        ):
            return self._format_order_summary(domestic_order, currency="KRW")
        return {
            "action_raw": "WAIT",
            "action": format_side_korean("WAIT"),
            "price": "-",
            "qty": "-",
            "indicator": "-",
            "reason": report.primary_selection_reason,
        }

    def _format_order_summary(self, order: dict, *, currency: str) -> dict[str, str]:
        candidate = order.get("candidate") or {}
        held = order.get("held_position") or {}
        signal_snapshot = order.get("signal_snapshot") or {}
        side = str(order.get("side", "wait")).upper()
        if order.get("virtual") and side == "BUY":
            action = "VIRTUAL_BUY"
        elif order.get("virtual") and side == "SELL":
            action = "VIRTUAL_SELL"
        else:
            action = side if side not in {"HOLD", "WAIT"} else "WAIT"
        if order.get("skipped"):
            action = "WAIT"
            if side == "BUY" and str(order.get("reason")) == "dry_run_enabled":
                action = "BUY_SETUP"
            elif side == "SELL" and str(order.get("reason")) == "dry_run_enabled":
                action = "SELL_SETUP"
            elif side == "SELL" and order.get("error"):
                action = "SELL_REJECTED"
        price_value = candidate.get("last_price") or candidate.get("current_price") or held.get("current_price")
        qty_value = order.get("qty") or held.get("quantity") or "-"

        indicator_parts: list[str] = []
        if signal_snapshot:
            snapshot = MovingAverageSnapshot(**signal_snapshot)
            if snapshot.rsi14 is not None:
                indicator_parts.append(f"RSI {snapshot.rsi14:.1f}")
            if snapshot.volume_ratio > 0:
                indicator_parts.append(f"거래량 {snapshot.volume_ratio:.1f}x")
            if snapshot.minute_ma_fast and snapshot.minute_ma_slow:
                relation = "상방" if snapshot.minute_ma_fast >= snapshot.minute_ma_slow else "하방"
                indicator_parts.append(f"분봉 {relation}")
        elif "pnl_pct" in held:
            indicator_parts.append(f"손익 {float(held['pnl_pct']) * 100:+.2f}%")
        elif "change_rate_pct" in candidate:
            indicator_parts.append(f"등락 {float(candidate['change_rate_pct']):+.2f}%")
        elif "minute_change_pct" in candidate:
            indicator_parts.append(f"등락 {float(candidate['minute_change_pct']) * 100:+.2f}%")

        if price_value in (None, "", "-"):
            price = "-"
        elif currency == "USD":
            price = f"${float(price_value):.4f}"
        else:
            price = f"{int(float(price_value)):,}원"

        return {
            "action_raw": action,
            "action": format_side_korean(action),
            "price": price,
            "qty": str(qty_value),
            "indicator": ", ".join(indicator_parts) if indicator_parts else "-",
            "reason": format_reason_korean(
                str(
                    order.get("exit_reason")
                    or order.get("reason")
                    or order.get("error")
                    or "watching"
                )
            ),
        }

    def _ma_relation_summary(self, snapshot: MovingAverageSnapshot) -> str:
        auto = self.config.auto_trade
        if not snapshot.has_required_context:
            return "-"
        daily_relation = ">" if (snapshot.daily_ma_fast or 0) >= (snapshot.daily_ma_slow or 0) else "<"
        minute_relation = ">" if (snapshot.minute_ma_fast or 0) >= (snapshot.minute_ma_slow or 0) else "<"
        return (
            f"{auto.daily_fast_window}d{daily_relation}{auto.daily_slow_window}d "
            f"{auto.intraday_fast_window}{minute_relation}{auto.intraday_slow_window}"
        )

    @staticmethod
    def _wait_state(snapshot: MovingAverageSnapshot) -> str:
        mapping = {
            "trend_up": "SETUP",
            "momentum_breakout": "SPIKE",
            "momentum_setup": "SETUP",
            "recovery": "SETUP",
            "breakout_test": "BREAK",
            "pullback": "PULLBACK",
            "trend_down": "DOWNTREND",
            "breakdown": "DOWNTREND",
            "range": "RANGE",
            "warmup": "WARMUP",
        }
        return mapping.get(snapshot.regime, "WAIT")

    def _estimate_api_calls_per_cycle(
        self,
        *,
        krx_open: bool,
        us_open: bool,
        domestic_watch_count: int,
        overseas_watch_count: int,
        include_domestic_order: bool | None = None,
        include_domestic_paper: bool | None = None,
        include_overseas_order: bool,
    ) -> int:
        if include_domestic_order is None:
            include_domestic_order = bool(include_domestic_paper)
        estimated_calls = 0
        if krx_open:
            domestic_candidates = len(self.config.liquidity_lab.domestic_candidates)
            refine_n = min(
                domestic_candidates,
                max(self.config.liquidity_lab.domestic_top_n * 2, 3),
            )
            estimated_calls += domestic_candidates * 2
            estimated_calls += refine_n
            estimated_calls += domestic_watch_count * 2
            estimated_calls += 1
            if include_domestic_order:
                estimated_calls += 1
        if us_open:
            config = self.config.liquidity_lab
            n_candidates = len(config.overseas_candidates)
            top_n = config.overseas_scan_top_n
            held_n = len(self._last_held_symbols)
            estimated_calls += n_candidates
            signal_n = top_n + max(0, held_n - top_n)
            estimated_calls += min(signal_n, n_candidates) * 2
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in config.overseas_candidates
            }
            estimated_calls += len(exchange_codes)
            if include_overseas_order:
                estimated_calls += 1
        return estimated_calls

    @staticmethod
    def _format_report_time(value: str) -> str:
        if not value:
            return "-"
        parts = value.split()
        if len(parts) >= 2:
            date_part = parts[0]
            time_part = parts[1]
            try:
                year, month, day = [int(chunk) for chunk in date_part.split("-")]
                hour, minute, _ = [int(chunk) for chunk in time_part.split(":")]
            except ValueError:
                return value
            return f"{month}월 {day}일 {hour:02d}:{minute:02d}"
        return value

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)

    def _build_exit_setup(
        self,
        snapshot: MovingAverageSnapshot,
        pnl_pct: float,
        position_qty: int,
    ):
        return evaluate_exit_setup(
            self.config.auto_trade,
            snapshot,
            pnl_pct,
            drawdown_from_peak=0.0,
            hold_cycles=0,
            position_qty=position_qty,
            partial_exit_done=False,
        )
