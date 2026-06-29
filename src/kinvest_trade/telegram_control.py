from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .client import KisRestClient
from .config import AppConfig
from .liquidity_lab import LiquidityLabReport, LiquidityLabService
from .market_sessions import (
    determine_loop_interval_sec,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    minutes_until_next_tradeable_session,
)
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .time_utils import format_display_times, format_kst


HELP_MESSAGE = "\n".join(
    [
        "[KIS][TELEGRAM_CONTROL_HELP]",
        "/lab_start - liquidity-lab loop start",
        "/lab_pause - finish current cycle, then pause",
        "/lab_resume - resume paused loop",
        "/lab_stop - cancel current cycle and stop",
        "/lab_terminate - force stop current liquidity-lab run and stay idle",
        "/lab_status - current status",
        "/lab_watchlist - current monitored symbols and MA state",
        "/lab_positions - current held positions and unrealized P&L",
        "/lab_help - command list",
    ]
)


@dataclass(slots=True)
class ControllerSnapshot:
    mode: str
    current_cycle_no: int
    active_cycle_started_at: str | None
    next_run_at: str | None
    last_command: str | None
    last_command_at: str | None
    last_completed_at: str | None
    last_report_summary: dict | None
    session_performance: dict | None
    last_error: str | None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "current_cycle_no": self.current_cycle_no,
            "active_cycle_started_at": self.active_cycle_started_at,
            "next_run_at": self.next_run_at,
            "last_command": self.last_command,
            "last_command_at": self.last_command_at,
            "last_completed_at": self.last_completed_at,
            "last_report_summary": self.last_report_summary,
            "session_performance": self.session_performance,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class SessionPerformance:
    started_at: datetime | None = None
    cycles_completed: int = 0
    domestic_paper_runs: int = 0
    domestic_paper_realized_pnl_krw: int = 0
    estimated_overseas_realized_pnl_krw: int = 0
    domestic_orders_submitted: int = 0
    overseas_orders_submitted: int = 0
    domestic_orders_failed: int = 0
    overseas_orders_failed: int = 0
    skip_reasons: dict[str, int] | None = None
    primary_targets: dict[str, int] | None = None
    symbol_stats: dict[str, dict[str, int]] | None = None

    def __post_init__(self) -> None:
        if self.skip_reasons is None:
            self.skip_reasons = {}
        if self.primary_targets is None:
            self.primary_targets = {}
        if self.symbol_stats is None:
            self.symbol_stats = {}

    def to_dict(self) -> dict:
        return {
            "started_at": format_kst(self.started_at),
            "cycles_completed": self.cycles_completed,
            "domestic_paper_runs": self.domestic_paper_runs,
            "domestic_paper_realized_pnl_krw": self.domestic_paper_realized_pnl_krw,
            "estimated_overseas_realized_pnl_krw": self.estimated_overseas_realized_pnl_krw,
            "domestic_orders_submitted": self.domestic_orders_submitted,
            "overseas_orders_submitted": self.overseas_orders_submitted,
            "domestic_orders_failed": self.domestic_orders_failed,
            "overseas_orders_failed": self.overseas_orders_failed,
            "skip_reasons": dict(sorted((self.skip_reasons or {}).items(), key=lambda item: (-item[1], item[0]))),
            "primary_targets": dict(sorted((self.primary_targets or {}).items(), key=lambda item: (-item[1], item[0]))),
            "symbol_stats": dict(sorted((self.symbol_stats or {}).items(), key=lambda item: item[0])),
        }


class TelegramLiquidityLabController:
    def __init__(
        self,
        config: AppConfig,
        repository: SqliteRepository,
        notifier: TelegramNotifier,
    ) -> None:
        self.config = config
        self.repository = repository
        self.notifier = notifier
        self.mode = "stopped"
        self.current_cycle_no = 0
        self.current_task: asyncio.Task[None] | None = None
        self.current_task_started_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_command: str | None = None
        self.last_command_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_report_summary: dict | None = None
        self.session_performance = SessionPerformance()
        self.last_error: str | None = None
        self.update_offset: int | None = None
        self._consecutive_errors: int = 0
        self._last_market_state: str = ""

    async def run(self) -> None:
        if not self.notifier.enabled:
            raise RuntimeError("Telegram bot token/chat id are required for telegram-control.")
        self._write_runtime_state()
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][TELEGRAM_CONTROL_START]",
                    f"profile={self.config.credentials.profile_name}",
                    f"loop_interval_sec={self.config.liquidity_lab.loop_interval_sec}",
                    "controller_mode=persistent_service",
                    "use /lab_help for commands",
                ]
            )
        )
        scheduler = asyncio.create_task(self._scheduler_loop())
        command_loop = asyncio.create_task(self._command_loop())
        try:
            await asyncio.gather(scheduler, command_loop)
        except asyncio.CancelledError:
            self.mode = "stopped"
        finally:
            if self.current_task is not None and not self.current_task.done():
                self.current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.current_task
            scheduler.cancel()
            command_loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            with contextlib.suppress(asyncio.CancelledError):
                await command_loop

    async def _scheduler_loop(self) -> None:
        while True:
            await self._drain_finished_cycle()
            if self.mode == "running" and self.current_task is None:
                now = datetime.now(timezone.utc)
                if self.next_run_at is None or now >= self.next_run_at:
                    self.current_cycle_no += 1
                    self.current_task_started_at = now
                    self.current_task = asyncio.create_task(self._run_cycle(self.current_cycle_no))
                    self._write_runtime_state()
            await asyncio.sleep(1.0)

    async def _command_loop(self) -> None:
        while True:
            updates = await self.notifier.get_updates(offset=self.update_offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.update_offset = update_id + 1
                await self._handle_update(update)
            await asyncio.sleep(0.2)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message", {}) if isinstance(update, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        chat_id = chat.get("id")
        text = str(message.get("text", "") or "").strip()
        if not text:
            return
        if not self.notifier.is_authorized_chat(chat_id):
            return

        command = self.parse_command(text)
        if command is None:
            return

        self.last_command = command
        self.last_command_at = datetime.now(timezone.utc)

        if command == "help":
            await self.notifier.send(HELP_MESSAGE)
            return
        if command == "status":
            await self.notifier.send(self._build_status_message())
            return
        if command == "watchlist":
            await self.notifier.send(self._build_watchlist_message())
            return
        if command == "positions":
            await self._send_positions_message()
            return
        if command == "start":
            await self._handle_start_like_command("running", "started")
            return
        if command == "resume":
            await self._handle_start_like_command("running", "resumed")
            return
        if command == "pause":
            await self._handle_pause()
            return
        if command == "stop":
            await self._handle_stop()
            return
        if command == "terminate":
            await self._handle_terminate()

    async def _handle_start_like_command(self, target_mode: str, verb: str) -> None:
        if self.mode == "stopped":
            self.session_performance = SessionPerformance(started_at=datetime.now(timezone.utc))
        self.mode = target_mode
        self.next_run_at = datetime.now(timezone.utc)
        self.last_error = None
        self._consecutive_errors = 0
        self._write_runtime_state()
        await self.notifier.send(
            f"[KIS][TELEGRAM_CONTROL]\nmode={self.mode}\ncommand={verb}\nnext_run=immediate"
        )

    async def _handle_pause(self) -> None:
        self.mode = "paused"
        self._write_runtime_state()
        await self.notifier.send(
            "[KIS][TELEGRAM_CONTROL]\nmode=paused\ncommand=pause\nnote=current_cycle_finishes_then_pause"
        )

    async def _handle_stop(self) -> None:
        self.mode = "stopped"
        self.next_run_at = None
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current_task
        self.current_task = None
        self.current_task_started_at = None
        self.next_run_at = None
        summary = self._finalize_session_summary(command="stop")
        self._write_runtime_state()
        await self.notifier.send(
            summary
        )

    async def _handle_terminate(self) -> None:
        self.mode = "stopped"
        self.next_run_at = None
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current_task
        self.current_task = None
        self.current_task_started_at = None
        self.next_run_at = None
        summary = self._finalize_session_summary(command="terminate")
        self._write_runtime_state()
        await self.notifier.send(
            summary
        )

    async def _run_cycle(self, cycle_no: int) -> None:
        """
        Execute a single liquidity-lab cycle without auto-stopping on market close.
        """
        try:
            async with KisRestClient(self.config.credentials) as client:
                service = LiquidityLabService(self.config, client, self.repository, self.notifier)
                report = await service.run()
            self.last_report_summary = self._summarize_report(report, cycle_no)
            self._accumulate_session_performance(report)
            self.last_completed_at = datetime.now(timezone.utc)
            self.last_error = None
            self._consecutive_errors = 0

            now_for_state = datetime.now(timezone.utc)
            krx_open = is_krx_regular_session(now_for_state)
            us_open = is_us_orderable_session_for_env(now_for_state, self.config.credentials.env)
            if krx_open:
                current_market_state = "krx_open"
            elif us_open:
                current_market_state = f"us_{get_us_trading_session(now_for_state)}"
            else:
                current_market_state = "both_closed"

            if self._last_market_state and self._last_market_state != current_market_state:
                await self.notifier.send(
                    f"[KIS][MARKET_STATE_CHANGE]\n"
                    f"from={self._last_market_state}\n"
                    f"to={current_market_state}\n"
                    f"time={format_kst(now_for_state)}"
                )
            self._last_market_state = current_market_state
        except asyncio.CancelledError:
            self.last_error = f"cycle_{cycle_no}_cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._consecutive_errors += 1
            await self.notifier.send(
                f"[KIS][TELEGRAM_CONTROL_ERROR]\ncycle={cycle_no}\nerror={exc}"
            )
            if self._consecutive_errors == 5:
                await self.notifier.send(
                    "[KIS][TELEGRAM_CONTROL_WARNING]\n"
                    f"consecutive_errors={self._consecutive_errors}\n"
                    "루프는 계속 실행 중. 수동 확인 권장."
                )
        finally:
            now = datetime.now(timezone.utc)
            interval_sec = determine_loop_interval_sec(
                now_utc=now,
                env=self.config.credentials.env,
                consecutive_errors=self._consecutive_errors,
            )
            base_time = self.current_task_started_at or now
            scheduled = base_time + timedelta(seconds=interval_sec)
            self.next_run_at = scheduled if scheduled > now else now
            self._write_runtime_state()

    async def _drain_finished_cycle(self) -> None:
        if self.current_task is None:
            return
        if not self.current_task.done():
            return

        try:
            await self.current_task
        except asyncio.CancelledError:
            pass
        self.current_task = None
        self.current_task_started_at = None
        self._write_runtime_state()

    def _write_runtime_state(self) -> None:
        path = self.config.storage.runtime_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": self.mode,
            "updated_at": format_kst(datetime.now(timezone.utc)),
            "linked_account": self.config.credentials.profile_name,
            "watch_targets": (self.last_report_summary or {}).get("watch_targets", []),
            "last_error": self.last_error,
            "notes": [
                "telegram-control daemon manages liquidity-lab loop state.",
                "Use Telegram commands to start, pause, resume, stop, or terminate.",
            ],
            "telegram_control": self._snapshot().to_dict(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _snapshot(self) -> ControllerSnapshot:
        return ControllerSnapshot(
            mode=self.mode,
            current_cycle_no=self.current_cycle_no,
            active_cycle_started_at=None
            if self.current_task_started_at is None
            else format_kst(self.current_task_started_at),
            next_run_at=None if self.next_run_at is None else format_kst(self.next_run_at),
            last_command=self.last_command,
            last_command_at=None if self.last_command_at is None else format_kst(self.last_command_at),
            last_completed_at=None
            if self.last_completed_at is None
            else format_kst(self.last_completed_at),
            last_report_summary=self.last_report_summary,
            session_performance=self.session_performance.to_dict(),
            last_error=self.last_error,
        )

    def _build_status_message(self) -> str:
        snapshot = self._snapshot()
        session = snapshot.session_performance or {}
        last_report = snapshot.last_report_summary or {}
        now = datetime.now(timezone.utc)
        krx_open = is_krx_regular_session(now)
        us_session = get_us_trading_session(now)
        us_tradeable = is_us_orderable_session_for_env(now, self.config.credentials.env)
        if krx_open:
            market_status = "KRX 정규장 ✓"
        elif us_tradeable:
            market_status = f"US {us_session} ✓"
        elif us_session in {"premarket", "aftermarket"}:
            market_status = f"US {us_session} (감시중)"
        else:
            mins = minutes_until_next_tradeable_session(now, self.config.credentials.env)
            hours, minutes = divmod(mins, 60)
            market_status = f"양쪽 장 닫힘 — 다음 개장까지 {hours}h{minutes:02d}m"

        next_interval = determine_loop_interval_sec(
            now,
            self.config.credentials.env,
            self._consecutive_errors,
        )
        return "\n".join(
            [
                "[KIS][TELEGRAM_CONTROL_STATUS]",
                f"time={format_kst(now)}",
                f"mode={snapshot.mode}",
                f"cycle={snapshot.current_cycle_no}",
                f"active_since={snapshot.active_cycle_started_at}",
                f"next_run={snapshot.next_run_at}",
                f"market={market_status}",
                f"next_loop_interval={next_interval}s",
                f"consecutive_errors={self._consecutive_errors}",
                f"last_command={snapshot.last_command}",
                f"last_completed={snapshot.last_completed_at}",
                f"last_target={last_report.get('primary_target') or '-'}",
                f"confirmed_pnl_krw={session.get('domestic_paper_realized_pnl_krw', 0)}",
                f"estimated_exit_pnl_krw={session.get('estimated_overseas_realized_pnl_krw', 0)}",
                f"watch_count={len(last_report.get('watch_targets') or [])}",
                f"symbols={self._format_symbol_stats_inline(session.get('symbol_stats') or {})}",
                f"last_error={snapshot.last_error}",
            ]
        )

    def _build_watchlist_message(self) -> str:
        last_report = self.last_report_summary or {}
        watch_targets = last_report.get("watch_targets") or []
        positions = last_report.get("overseas_positions") or []
        pnl_map: dict[str, float] = {
            str(pos.get("symbol", "")).upper(): float(pos.get("pnl_pct", 0) or 0)
            for pos in positions
        }
        lines = [
            "[KIS][TELEGRAM_CONTROL_WATCHLIST]",
            f"time={format_kst(datetime.now(timezone.utc))}",
            f"mode={self.mode}",
            f"cycle={self.current_cycle_no}",
            f"est_cycle_calls={last_report.get('estimated_api_calls_per_cycle', '-')}",
        ]
        if not watch_targets:
            lines.append("targets=-")
            if positions:
                lines.append(self._build_positions_message())
            return "\n".join(lines)

        for watch_target in watch_targets:
            symbol = str(watch_target.get("code", "")).upper()
            lines.append(self._format_watch_target_line(watch_target, pnl_pct=pnl_map.get(symbol)))
        return "\n".join(lines)

    def _build_positions_message(self) -> str:
        last_report = self.last_report_summary or {}
        positions = last_report.get("overseas_positions") or []

        lines = [
            "[KIS][TELEGRAM_CONTROL_POSITIONS]",
            f"time={format_kst(datetime.now(timezone.utc))}",
            f"cycle={self.current_cycle_no}",
        ]

        if not positions:
            lines.append("held=none")
            return "\n".join(lines)

        total_pnl_pct_sum = 0.0
        for pos in positions:
            symbol = str(pos.get("symbol", "-"))
            qty = int(pos.get("quantity", 0) or 0)
            avg_price = float(pos.get("avg_price", 0) or 0)
            current_price = float(pos.get("current_price", 0) or 0)
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            total_pnl_pct_sum += pnl_pct

            pnl_sign = "+" if pnl_pct >= 0 else ""
            pnl_text = f"{pnl_sign}{pnl_pct * 100:.2f}%"
            price_text = f"{int(current_price)}" if current_price >= 1000 else f"{current_price:.4f}"
            avg_text = f"{int(avg_price)}" if avg_price >= 1000 else f"{avg_price:.4f}"
            lines.append(
                f"{symbol} qty={qty} avg={avg_text} px={price_text} pnl={pnl_text}"
            )

        avg_pnl = total_pnl_pct_sum / len(positions)
        avg_sign = "+" if avg_pnl >= 0 else ""
        lines.append(f"avg_pnl={avg_sign}{avg_pnl * 100:.2f}%")
        return "\n".join(lines)

    async def _send_positions_message(self) -> None:
        await self.notifier.send(self._build_positions_message())

    def _accumulate_session_performance(self, report: LiquidityLabReport) -> None:
        perf = self.session_performance
        if perf.started_at is None:
            perf.started_at = datetime.now(timezone.utc)
        perf.cycles_completed += 1

        primary_target = str(report.primary_target or report.primary_market or "none")
        perf.primary_targets[primary_target] = perf.primary_targets.get(primary_target, 0) + 1

        if report.paper_run:
            if not report.paper_run.get("skipped", False):
                perf.domestic_paper_runs += 1
                perf.domestic_paper_realized_pnl_krw += int(report.paper_run.get("realized_pnl_krw", 0) or 0)
                self._accumulate_paper_symbol_stats(report.paper_run)
            else:
                reason = str(report.paper_run.get("reason", "paper_skipped"))
                perf.skip_reasons[reason] = perf.skip_reasons.get(reason, 0) + 1

        self._accumulate_order_stats(report.domestic_order, market="domestic")
        self._accumulate_order_stats(report.overseas_order, market="overseas")

        selection_reason = str(report.primary_selection_reason or "")
        if selection_reason:
            perf.skip_reasons[selection_reason] = perf.skip_reasons.get(selection_reason, 0) + 1

    def _accumulate_order_stats(self, order_result: dict | None, *, market: str) -> None:
        if not order_result:
            return
        perf = self.session_performance
        is_submitted = bool(order_result.get("submitted"))
        is_skipped = bool(order_result.get("skipped"))
        if market == "domestic":
            if is_submitted:
                perf.domestic_orders_submitted += 1
            elif not is_skipped:
                perf.domestic_orders_failed += 1
        else:
            if is_submitted:
                perf.overseas_orders_submitted += 1
                perf.estimated_overseas_realized_pnl_krw += self._estimate_overseas_realized_pnl_krw(order_result)
            elif not is_skipped:
                perf.overseas_orders_failed += 1

        self._accumulate_symbol_order_stats(order_result, market=market, submitted=is_submitted)

        if is_skipped:
            reason = str(order_result.get("reason", f"{market}_skipped"))
            perf.skip_reasons[reason] = perf.skip_reasons.get(reason, 0) + 1
        elif not is_submitted:
            reason = str(order_result.get("error", f"{market}_order_failed"))
            perf.skip_reasons[reason] = perf.skip_reasons.get(reason, 0) + 1

    def _accumulate_paper_symbol_stats(self, paper_run: dict) -> None:
        watchlist = paper_run.get("watchlist") or []
        if not watchlist:
            return
        symbol = str(watchlist[0]).strip().upper()
        stats = self._ensure_symbol_stats(symbol)
        stats["paper_runs"] += 1
        stats["confirmed_realized_pnl_krw"] += int(paper_run.get("realized_pnl_krw", 0) or 0)

    def _accumulate_symbol_order_stats(
        self,
        order_result: dict,
        *,
        market: str,
        submitted: bool,
    ) -> None:
        candidate = order_result.get("candidate") or {}
        symbol = str(candidate.get("symbol") or candidate.get("stock_code") or "").strip().upper()
        if not symbol:
            return

        stats = self._ensure_symbol_stats(symbol)
        side = str(order_result.get("side", "")).strip().lower()
        qty = int(order_result.get("qty", 0) or 0)
        if submitted and side == "buy":
            stats["buy_count"] += 1
            stats["buy_qty"] += qty
        elif submitted and side == "sell":
            stats["sell_count"] += 1
            stats["sell_qty"] += qty
            if market == "overseas":
                stats["estimated_realized_pnl_krw"] += self._estimate_overseas_realized_pnl_krw(order_result)

    def _estimate_overseas_realized_pnl_krw(self, order_result: dict) -> int:
        candidate = order_result.get("candidate") or {}
        held = order_result.get("held_position") or {}
        qty = int(order_result.get("qty", 0) or 0)
        current_price = self._parse_float(candidate.get("last_price") or held.get("current_price"))
        avg_price = self._parse_float(held.get("avg_price"))
        fx_rate = self._parse_float(candidate.get("fx_rate_krw")) or self.config.auto_trade.usd_krw_fallback_rate
        if qty <= 0 or current_price <= 0 or avg_price <= 0:
            return 0
        return int(round((current_price - avg_price) * qty * fx_rate))

    def _ensure_symbol_stats(self, symbol: str) -> dict[str, int]:
        stats = self.session_performance.symbol_stats.setdefault(
            symbol,
            {
                "buy_count": 0,
                "sell_count": 0,
                "buy_qty": 0,
                "sell_qty": 0,
                "paper_runs": 0,
                "confirmed_realized_pnl_krw": 0,
                "estimated_realized_pnl_krw": 0,
            },
        )
        return stats

    @staticmethod
    def _format_symbol_stats_inline(symbol_stats: dict[str, dict]) -> str:
        if not symbol_stats:
            return "-"
        chunks: list[str] = []
        for symbol, stats in sorted(symbol_stats.items()):
            chunks.append(
                (
                    f"{symbol}(buy={int(stats.get('buy_count', 0))},sell={int(stats.get('sell_count', 0))},"
                    f"paper={int(stats.get('paper_runs', 0))},"
                    f"pnl={int(stats.get('confirmed_realized_pnl_krw', 0))}/"
                    f"{int(stats.get('estimated_realized_pnl_krw', 0))})"
                )
            )
        return "; ".join(chunks)

    def _finalize_session_summary(self, *, command: str) -> str:
        ended_at = datetime.now(timezone.utc)
        summary = format_display_times(
            {
                "command": command,
                "profile": self.config.credentials.profile_name,
                "started_at": format_kst(self.session_performance.started_at),
                "ended_at": format_kst(ended_at),
                **self.session_performance.to_dict(),
                "last_error": self.last_error,
            }
        )
        record_id = self.repository.save_telegram_control_session(
            command=command,
            profile=self.config.credentials.profile_name,
            started_at=summary.get("started_at"),
            cycles_completed=int(summary.get("cycles_completed", 0) or 0),
            domestic_paper_runs=int(summary.get("domestic_paper_runs", 0) or 0),
            domestic_paper_realized_pnl_krw=int(summary.get("domestic_paper_realized_pnl_krw", 0) or 0),
            domestic_orders_submitted=int(summary.get("domestic_orders_submitted", 0) or 0),
            overseas_orders_submitted=int(summary.get("overseas_orders_submitted", 0) or 0),
            domestic_orders_failed=int(summary.get("domestic_orders_failed", 0) or 0),
            overseas_orders_failed=int(summary.get("overseas_orders_failed", 0) or 0),
            summary_json=summary,
        )
        self.repository.save_heartbeat(
            "TELEGRAM_CONTROL_SESSION_SUMMARY",
            (
                f"record_id={record_id} command={command} "
                f"cycles_completed={summary['cycles_completed']} "
                f"domestic_paper_realized_pnl_krw={summary['domestic_paper_realized_pnl_krw']}"
            ),
        )
        self.repository.save_risk_event(
            event_type="TELEGRAM_CONTROL_SESSION_SUMMARY",
            severity="INFO",
            message=(
                f"telegram control session finalized via {command}; "
                f"realized_pnl_krw={summary['domestic_paper_realized_pnl_krw']}"
            ),
            raw_payload=summary,
        )
        self.session_performance = SessionPerformance()
        lines = [
            "[KIS][TELEGRAM_CONTROL_SESSION_SUMMARY]",
                f"record_id={record_id}",
                f"mode=stopped",
                f"command={command}",
                f"started_at={summary['started_at']}",
                f"ended_at={summary['ended_at']}",
                f"cycles={summary['cycles_completed']}",
                f"confirmed_pnl_krw={summary['domestic_paper_realized_pnl_krw']}",
                f"estimated_exit_pnl_krw={summary['estimated_overseas_realized_pnl_krw']}",
                f"orders=domestic {summary['domestic_orders_submitted']}/{summary['domestic_orders_failed']}, overseas {summary['overseas_orders_submitted']}/{summary['overseas_orders_failed']}",
                f"symbols={self._format_symbol_stats_inline(summary.get('symbol_stats') or {})}",
                f"last_error={summary['last_error']}",
            ]
        return "\n".join(lines)

    @staticmethod
    def _summarize_report(report: LiquidityLabReport, cycle_no: int) -> dict:
        return format_display_times(
            {
            "cycle_no": cycle_no,
            "scanned_at": report.scanned_at,
            "primary_market": report.primary_market,
            "primary_target": report.primary_target,
            "primary_selection_reason": report.primary_selection_reason,
            "paper_run": report.paper_run,
            "domestic_order": report.domestic_order,
            "overseas_order": report.overseas_order,
            "watch_targets": report.to_dict().get("watch_targets", []),
            "overseas_positions": [
                {
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "avg_price": pos.avg_price,
                    "current_price": pos.current_price,
                    "pnl_pct": pos.pnl_pct,
                    "exchange_code": pos.exchange_code,
                }
                for pos in report.overseas_positions
            ],
            "estimated_api_calls_per_cycle": report.estimated_api_calls_per_cycle,
            "market_closed": (
                not report.krx_market_open and not report.us_market_open
            ),
            }
        )

    @staticmethod
    def parse_command(text: str) -> str | None:
        normalized = text.strip().split()[0].lower()
        mapping = {
            "/lab_start": "start",
            "/lab_pause": "pause",
            "/lab_resume": "resume",
            "/lab_stop": "stop",
            "/lab_terminate": "terminate",
            "/lab_status": "status",
            "/lab_watchlist": "watchlist",
            "/lab_positions": "positions",
            "/lab_help": "help",
            "/start": "help",
            "/help": "help",
        }
        return mapping.get(normalized)

    @staticmethod
    def _format_watch_target_line(watch_target: dict, pnl_pct: float | None = None) -> str:
        code = str(watch_target.get("code", "-"))
        signal_state = str(watch_target.get("signal_state", "WAIT"))
        ma_summary = str(watch_target.get("ma_summary", "-"))
        note = str(watch_target.get("note", "-"))
        price = watch_target.get("price", "-")
        if isinstance(price, (int, float)):
            if float(price) >= 1000:
                price_text = f"{int(price)}"
            else:
                price_text = f"{float(price):.4f}"
        else:
            price_text = str(price)
        holding_qty = int(watch_target.get("holding_qty", 0) or 0)
        holding_text = f" hold={holding_qty}" if holding_qty > 0 else ""
        pnl_text = ""
        if holding_qty > 0 and pnl_pct is not None:
            sign = "+" if pnl_pct >= 0 else ""
            pnl_text = f" pnl={sign}{pnl_pct * 100:.2f}%"
        return f"{code} {signal_state} {ma_summary} {note} px={price_text}{holding_text}{pnl_text}"

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)
