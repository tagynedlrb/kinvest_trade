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
    format_snapshot_indicator,
)
from .time_utils import format_kst


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
class OverseasHeldPosition:
    symbol: str
    exchange_code: str
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
            "overseas_positions": [asdict(item) for item in self.overseas_positions],
            "watch_targets": [asdict(item) for item in self.watch_targets],
            "estimated_api_calls_per_cycle": self.estimated_api_calls_per_cycle,
            "paper_run": self.paper_run,
            "domestic_order": self.domestic_order,
            "overseas_order": self.overseas_order,
        }


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
        self._domestic_excluded: list[ExcludedCandidate] = []
        self._overseas_excluded: list[ExcludedCandidate] = []
        self._active_pool: list[OverseasCandidateConfig] = []
        self._bench_pool: list[OverseasCandidateConfig] = []
        self._cycle_count: int = 0
        self._pool_initialized: bool = False
        self._bench_scanned_this_cycle: bool = False
        self._last_held_symbols: set[str] = set()

    async def run(self) -> LiquidityLabReport:
        now = datetime.now(timezone.utc)
        krx_open = is_krx_regular_session(now)
        us_open = is_us_regular_session(now)
        us_session = get_us_trading_session(now)
        us_orderable_in_profile = is_us_orderable_session_for_env(
            now,
            self.config.credentials.env,
        )

        domestic_ranked = await self.scan_domestic() if krx_open else []
        overseas_ranked = await self.scan_overseas() if us_open else []
        overseas_positions = await self._load_overseas_positions(overseas_ranked) if us_open else []
        domestic_watch_targets = (
            await self._build_domestic_watch_targets(domestic_ranked)
            if krx_open
            else []
        )
        overseas_watch_targets = (
            await self._build_overseas_watch_targets(overseas_ranked, overseas_positions)
            if us_open
            else []
        )
        overseas_exit_target = (
            await self._select_overseas_exit_target(overseas_ranked, overseas_positions)
            if us_orderable_in_profile
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
        if overseas_exit_target is not None:
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

        if primary_market == "domestic" and domestic_buy_target is not None:
            watchlist = [domestic_buy_target.stock_code]
            paper_state = await self._run_domestic_paper_test(watchlist)
            paper_summary = {
                "run_id": paper_state.run_id,
                "watchlist": watchlist,
                "ending_cash_krw": paper_state.cash_krw,
                "realized_pnl_krw": paper_state.realized_pnl_krw,
            }
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
        elif primary_market == "overseas" and overseas_buy_target is not None:
            overseas_order = await self._manage_overseas_position(
                candidate=overseas_buy_target,
                held_positions=overseas_positions,
            )
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
            overseas_positions=overseas_positions,
            watch_targets=domestic_watch_targets if krx_open else overseas_watch_targets,
            estimated_api_calls_per_cycle=self._estimate_api_calls_per_cycle(
                krx_open=krx_open,
                us_open=us_open,
                domestic_watch_count=len(domestic_watch_targets),
                overseas_watch_count=len(overseas_watch_targets),
                include_domestic_paper=domestic_buy_target is not None,
                include_overseas_order=bool(overseas_exit_target or overseas_buy_target),
            ),
            paper_run=paper_summary,
            domestic_order=domestic_order,
            overseas_order=overseas_order,
        )
        await self._send_summary(report)
        return report

    async def scan_domestic(self) -> list[DomesticScanResult]:
        results: list[DomesticScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for stock_code in self.config.liquidity_lab.domestic_candidates:
            try:
                candidate = await self._scan_single_domestic(stock_code)
            except Exception:
                continue
            reasons = self._domestic_speculative_reasons(candidate)
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
                results.append(candidate)
            await asyncio.sleep(0.2)
        self._domestic_excluded = excluded
        return sorted(results, key=lambda item: item.activity_score, reverse=True)

    async def scan_overseas(self) -> list[OverseasScanResult]:
        config = self.config.liquidity_lab
        self._cycle_count += 1
        should_bench_scan = (
            not self._pool_initialized
            or self._cycle_count % config.overseas_bench_scan_every == 0
        )
        self._bench_scanned_this_cycle = should_bench_scan
        if should_bench_scan:
            await self._run_bench_scan()
            self._pool_initialized = True

        if not self._active_pool:
            self._active_pool = list(
                config.overseas_candidates[: config.overseas_active_pool_size]
            )
            active_symbols = {candidate.symbol.upper() for candidate in self._active_pool}
            self._bench_pool = [
                candidate
                for candidate in config.overseas_candidates
                if candidate.symbol.upper() not in active_symbols
            ]

        # Keep held symbols in scan targets even when they fall out of the active pool.
        held_symbols = await self._get_held_symbols()
        scan_targets = list(self._active_pool)
        if held_symbols:
            active_syms = {candidate.symbol.upper() for candidate in scan_targets}
            for candidate in config.overseas_candidates:
                if (
                    candidate.symbol.upper() in held_symbols
                    and candidate.symbol.upper() not in active_syms
                ):
                    scan_targets.append(candidate)

        results: list[OverseasScanResult] = []
        excluded: list[ExcludedCandidate] = []
        for candidate in scan_targets:
            try:
                scan_result = await self._scan_single_overseas(candidate)
            except Exception:
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
                results.append(scan_result)
            await asyncio.sleep(0.1)
        self._overseas_excluded = excluded
        return sorted(results, key=lambda item: item.activity_score, reverse=True)

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
            held: set[str] = set()
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
            return self._last_held_symbols

    async def _run_bench_scan(self) -> None:
        config = self.config.liquidity_lab
        bench_results: list[OverseasScanResult] = []

        for candidate in config.overseas_candidates:
            try:
                scan_result = await self._scan_single_overseas(candidate)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            reasons = self._overseas_speculative_reasons(scan_result)
            if not reasons:
                bench_results.append(scan_result)
            await asyncio.sleep(0.1)

        if not bench_results:
            return

        bench_results.sort(key=lambda item: item.activity_score, reverse=True)
        top_symbols = {
            item.symbol.upper()
            for item in bench_results[: config.overseas_active_pool_size]
        }

        new_active: list[OverseasCandidateConfig] = []
        new_bench: list[OverseasCandidateConfig] = []
        for candidate in config.overseas_candidates:
            if candidate.symbol.upper() in top_symbols:
                new_active.append(candidate)
            else:
                new_bench.append(candidate)

        self._active_pool = new_active
        self._bench_pool = new_bench
        self.repository.save_heartbeat(
            "POOL_ROTATION",
            (
                f"cycle={self._cycle_count} "
                f"bench_scanned={len(config.overseas_candidates)} "
                f"passed_filter={len(bench_results)} "
                f"active_pool=[{','.join(candidate.symbol for candidate in new_active)}]"
            ),
        )

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
                )

        return list(positions_by_key.values())

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
            signal_snapshot = await self._load_overseas_signal(quote)
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
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        for candidate in domestic_ranked[: self.config.liquidity_lab.domestic_top_n]:
            signal_snapshot = await self._load_domestic_signal(candidate)
            watch_targets.append(
                self._build_watch_target_status(
                    market="domestic",
                    code=candidate.stock_code,
                    exchange_code=None,
                    price=float(candidate.current_price),
                    activity_score=candidate.activity_score,
                    signal_snapshot=signal_snapshot,
                    holding_qty=0,
                )
            )
            await asyncio.sleep(0.1)
        return watch_targets

    async def _build_overseas_watch_targets(
        self,
        overseas_ranked: list[OverseasScanResult],
        held_positions: list[OverseasHeldPosition],
    ) -> list[WatchTargetStatus]:
        watch_targets: list[WatchTargetStatus] = []
        held_map = {position.symbol.upper(): position for position in held_positions}
        selected_candidates = list(overseas_ranked[: self.config.liquidity_lab.overseas_top_n])
        selected_symbols = {candidate.symbol.upper() for candidate in selected_candidates}
        for candidate in overseas_ranked:
            if candidate.symbol.upper() in held_map and candidate.symbol.upper() not in selected_symbols:
                selected_candidates.append(candidate)
                selected_symbols.add(candidate.symbol.upper())

        for candidate in selected_candidates:
            signal_snapshot = await self._load_overseas_signal(candidate)
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
            await asyncio.sleep(0.1)
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
        held_position: OverseasHeldPosition | None = None,
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
                signal_state="BUY_READY",
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
        response = await self.client.place_cash_order(
            side="buy",
            stock_code=candidate.stock_code,
            qty=qty,
            price=candidate.best_ask or candidate.current_price,
            order_division="00",
        )
        return {
            "submitted": True,
            "market": "domestic",
            "side": "buy",
            "candidate": asdict(candidate),
            "qty": qty,
            "response": response,
        }

    async def _place_overseas_test_order(self, candidate: OverseasScanResult) -> dict:
        signal_snapshot = await self._load_overseas_signal(candidate)
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
            if (
                "미국주식 주간거래는 제공하지 않습니다" in str(exc)
                or "KIS mock currently supports US order tests only during the US regular session" in str(exc)
            ):
                return {
                    "skipped": True,
                    "market": "overseas",
                    "side": "buy",
                    "candidate": asdict(candidate),
                    "signal_snapshot": asdict(signal_snapshot),
                    "qty": qty,
                    "reason": "mock_us_session_not_supported",
                    "error": str(exc),
                }
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

    async def _place_overseas_sell_order(
        self,
        candidate: OverseasScanResult,
        held: OverseasHeldPosition,
        exit_reason: str,
        signal_snapshot: MovingAverageSnapshot | None = None,
    ) -> dict:
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
            return {
                "submitted": False,
                "market": "overseas",
                "side": "sell",
                "candidate": asdict(candidate),
                "held_position": asdict(held),
                "signal_snapshot": None if signal_snapshot is None else asdict(signal_snapshot),
                "exit_reason": exit_reason,
                "error": str(exc),
            }

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

    async def _send_summary(self, report: LiquidityLabReport) -> None:
        action = self._build_action_summary(report)
        if action["action"] == "WAIT":
            return
        lines = [
            "[KIS][LIQUIDITY_LAB]",
            f"time={report.scanned_at}",
            f"market={report.primary_market}",
            f"target={report.primary_target or '-'}",
            f"action={action['action']}",
            f"price={action['price']}",
            f"qty={action['qty']}",
            f"indicator={action['indicator']}",
            f"reason={action['reason']}",
            f"watching={len(report.watch_targets)}",
        ]
        if report.overseas_positions:
            lines.append(f"held_positions={len(report.overseas_positions)}")
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
            "action": "WAIT",
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
        action = side if side not in {"HOLD", "WAIT"} else "WAIT"
        if order.get("skipped"):
            action = "WAIT"
            if side == "BUY" and str(order.get("reason")) == "dry_run_enabled":
                action = "BUY_SETUP"
            elif side == "SELL" and str(order.get("reason")) == "dry_run_enabled":
                action = "SELL_SETUP"
        price_value = candidate.get("last_price") or candidate.get("current_price") or held.get("current_price")
        qty_value = order.get("qty") or held.get("quantity") or "-"

        indicator_parts: list[str] = []
        if signal_snapshot:
            indicator_parts.append(
                format_snapshot_indicator(
                    MovingAverageSnapshot(**signal_snapshot),
                    daily_fast_label=f"{self.config.auto_trade.daily_fast_window}d",
                    daily_slow_label=f"{self.config.auto_trade.daily_slow_window}d",
                )
            )
        elif "pnl_pct" in held:
            indicator_parts.append(f"pnl={float(held['pnl_pct']) * 100:.2f}%")
        elif "change_rate_pct" in candidate:
            indicator_parts.append(f"chg={float(candidate['change_rate_pct']):.2f}%")
        elif "minute_change_pct" in candidate:
            indicator_parts.append(f"chg={float(candidate['minute_change_pct']) * 100:.2f}%")
        if "spread_pct" in candidate:
            indicator_parts.append(f"spread={float(candidate['spread_pct']) * 100:.2f}%")

        if price_value in (None, "", "-"):
            price = "-"
        elif currency == "USD":
            price = f"{float(price_value):.4f} USD"
        else:
            price = f"{int(price_value)} KRW"

        return {
            "action": action,
            "price": price,
            "qty": str(qty_value),
            "indicator": ", ".join(indicator_parts) if indicator_parts else "-",
            "reason": str(
                order.get("exit_reason")
                or order.get("reason")
                or order.get("error")
                or "watching"
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
        include_domestic_paper: bool,
        include_overseas_order: bool,
    ) -> int:
        estimated_calls = 0
        if krx_open:
            estimated_calls += len(self.config.liquidity_lab.domestic_candidates) * 3
            estimated_calls += domestic_watch_count * 2
            if include_domestic_paper:
                estimated_calls += self.config.liquidity_lab.domestic_paper_iterations * 2
                estimated_calls += 1
        if us_open:
            config = self.config.liquidity_lab
            active_n = len(self._active_pool) if self._active_pool else config.overseas_active_pool_size
            estimated_calls += active_n
            estimated_calls += overseas_watch_count * 2
            if self._bench_scanned_this_cycle:
                estimated_calls += len(config.overseas_candidates)
            exchange_codes = {
                candidate.exchange_code.upper()
                for candidate in config.overseas_candidates
            }
            estimated_calls += len(exchange_codes) * 2
            if include_overseas_order:
                estimated_calls += 1
        return estimated_calls

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
