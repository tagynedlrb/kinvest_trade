from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .client import KisRestClient, parse_kis_number
from .config import AppConfig
from .indicators import IndicatorSummary, summarize_indicators
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .time_utils import format_kst


@dataclass(slots=True)
class WatchSnapshot:
    stock_code: str
    last_price: int
    best_bid: int
    best_ask: int
    bid_size: int
    ask_size: int
    spread_pct: float
    indicators: IndicatorSummary
    latest_bar_time: str | None
    status: str
    note: str = ""


class ConsoleWatchService:
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

    async def run(self) -> None:
        cycle = 0

        while True:
            cycle += 1
            now = datetime.now(timezone.utc)
            snapshots: list[WatchSnapshot] = []
            last_error = None

            for stock_code in self.config.trading.watchlist:
                try:
                    snapshots.append(await self._fetch_snapshot(stock_code))
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{stock_code}: {exc}"
                    snapshots.append(
                        WatchSnapshot(
                            stock_code=stock_code,
                            last_price=0,
                            best_bid=0,
                            best_ask=0,
                            bid_size=0,
                            ask_size=0,
                            spread_pct=0.0,
                            indicators=IndicatorSummary(
                                rsi14=None,
                                sma5=None,
                                sma20=None,
                                last_close=None,
                                change_pct_from_oldest=None,
                                volume_sum=0,
                                bar_count=0,
                            ),
                            latest_bar_time=None,
                            status="ERROR",
                            note=str(exc),
                        )
                    )

            self._write_runtime_state(now, snapshots, last_error)
            self._render_console(now, cycle, snapshots, last_error)

            if (
                self.notifier.enabled
                and self.config.watch.telegram_summary_every > 0
                and cycle % self.config.watch.telegram_summary_every == 0
            ):
                await self.notifier.send(self._build_telegram_summary(snapshots))

            max_cycles = self.config.watch.max_cycles
            if max_cycles > 0 and cycle >= max_cycles:
                break

            await asyncio.sleep(self.config.watch.poll_interval_sec)

    async def _fetch_snapshot(self, stock_code: str) -> WatchSnapshot:
        current = await self.client.get_current_price(stock_code, self.config.trading.market_code)
        orderbook = await self.client.get_orderbook(stock_code, self.config.trading.market_code)

        timeframe = self.config.watch.chart_timeframe
        if timeframe == "daily":
            end_date = datetime.now(timezone.utc).strftime("%Y%m%d")
            start_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y%m%d")
            rows = await self.client.get_daily_chart(
                stock_code=stock_code,
                start_date=start_date,
                end_date=end_date,
                market_code=self.config.trading.market_code,
            )
            closes = [parse_kis_number(row.get("stck_clpr")) for row in rows[: self.config.watch.chart_bar_limit]]
            volumes = [parse_kis_number(row.get("acml_vol")) for row in rows[: self.config.watch.chart_bar_limit]]
            latest_bar_time = rows[0].get("stck_bsop_date") if rows else None
            label = "daily"
        else:
            rows = await self.client.get_time_daily_chart(
                stock_code=stock_code,
                target_date=datetime.now(timezone.utc).strftime("%Y%m%d"),
                market_code=self.config.trading.market_code,
            )
            closes = [parse_kis_number(row.get("stck_prpr")) for row in rows[: self.config.watch.chart_bar_limit]]
            volumes = [parse_kis_number(row.get("cntg_vol")) for row in rows[: self.config.watch.chart_bar_limit]]
            latest_bar_time = rows[0].get("stck_cntg_hour") if rows else None
            label = "minute"

        indicators = summarize_indicators(closes, volumes)
        self.repository.save_indicator_check(
            stock_code=stock_code,
            timeframe=label,
            bar_count=indicators.bar_count,
            last_close=indicators.last_close,
            rsi14=indicators.rsi14,
            sma5=indicators.sma5,
            sma20=indicators.sma20,
            volume_sum=indicators.volume_sum,
            change_pct_from_oldest=indicators.change_pct_from_oldest,
            raw_payload=rows[: self.config.watch.chart_bar_limit],
        )

        return WatchSnapshot(
            stock_code=stock_code,
            last_price=current["current_price"],
            best_bid=orderbook["best_bid"],
            best_ask=orderbook["best_ask"],
            bid_size=orderbook["bid_size"],
            ask_size=orderbook["ask_size"],
            spread_pct=orderbook["spread_pct"],
            indicators=indicators,
            latest_bar_time=latest_bar_time,
            status="OK",
        )

    def _write_runtime_state(
        self,
        now: datetime,
        snapshots: list[WatchSnapshot],
        last_error: str | None,
    ) -> None:
        state_path = self.config.storage.runtime_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "running" if last_error is None else "degraded",
            "updated_at": format_kst(now),
            "linked_account": None,
            "watch_targets": [asdict(snapshot) for snapshot in snapshots],
            "last_error": last_error,
            "notes": [
                "This runtime mirrors the kiwoom_trade operator experience but uses KIS REST APIs.",
                "Current watch mode uses domestic quotation endpoints and virtual trading only.",
            ],
        }
        state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _render_console(
        self,
        now: datetime,
        cycle: int,
        snapshots: list[WatchSnapshot],
        last_error: str | None,
    ) -> None:
        if self.config.watch.clear_screen:
            print("\033[2J\033[H", end="")

        print("KIS LIVE WATCH")
        print(f"time: {format_kst(now)}")
        print(f"cycle: {cycle}")
        print(f"watchlist: {', '.join(self.config.trading.watchlist)}")
        print(f"runtime_state: {self.config.storage.runtime_state_path}")
        print("")
        print(
            "code       price      bid      ask   spread    rsi14     sma5    sma20    bar_time    status"
        )
        print(
            "----------------------------------------------------------------------------------------------"
        )
        for snapshot in snapshots:
            indicators = snapshot.indicators
            print(
                f"{snapshot.stock_code:<10}"
                f"{snapshot.last_price:>7}  "
                f"{snapshot.best_bid:>7}  "
                f"{snapshot.best_ask:>7}  "
                f"{snapshot.spread_pct:>6.3%}  "
                f"{self._fmt_float(indicators.rsi14):>7}  "
                f"{self._fmt_float(indicators.sma5):>7}  "
                f"{self._fmt_float(indicators.sma20):>7}  "
                f"{(snapshot.latest_bar_time or ''):>10}  "
                f"{snapshot.status}"
            )
            if snapshot.note:
                print(f"  note: {snapshot.note}")
        if last_error:
            print("")
            print(f"last_error: {last_error}")

    def _build_telegram_summary(self, snapshots: list[WatchSnapshot]) -> str:
        lines = ["[KIS][WATCH_SUMMARY]"]
        for snapshot in snapshots:
            lines.append(
                f"{snapshot.stock_code} price={snapshot.last_price} "
                f"bid={snapshot.best_bid} ask={snapshot.best_ask} "
                f"rsi14={self._fmt_float(snapshot.indicators.rsi14)} status={snapshot.status}"
            )
        return "\n".join(lines)

    @staticmethod
    def _fmt_float(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}"
