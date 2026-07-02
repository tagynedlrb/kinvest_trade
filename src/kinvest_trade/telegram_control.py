from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeAlias

from .client import KisRestClient
from .config import AppConfig
from .liquidity_lab import LiquidityLabReport, LiquidityLabService, VirtualTradeManager
from .market_sessions import (
    determine_loop_interval_sec,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    minutes_until_next_tradeable_session,
)
from .message_format import format_market_korean, format_pct, format_reason_korean, format_side_korean
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .time_utils import format_display_times, format_kst, format_kst_korean, parse_datetime


HELP_MESSAGE = "\n".join(
    [
        "[KIS][TELEGRAM_CONTROL_HELP]",
        "/lab_start - 거래 루프 시작",
        "/lab_pause - 현재 사이클 종료 후 일시정지",
        "/lab_resume - 일시정지 해제",
        "/lab_stop - 즉시 중지 후 세션 요약",
        "/lab_terminate - 강제 종료 후 대기",
        "/lab_service_restart - 텔레그램 제어 서비스 재시작",
        "/lab_status - 현재 상태",
        "/lab_watchlist - 감시 종목 요약",
        "/lab_log - 최근 매매 내역 조회",
        "/lab_portfolio - 보유현황 통합 (실보유·가상·성과)",
        "/lab_paper_test <종목코드> - 수동 페이퍼 테스트",
        "/lab_help - 명령 목록",
    ]
)

BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "lab_start", "description": "거래 루프 시작"},
    {"command": "lab_pause", "description": "사이클 종료 후 일시정지"},
    {"command": "lab_resume", "description": "일시정지 해제"},
    {"command": "lab_stop", "description": "즉시 중지 후 세션 요약"},
    {"command": "lab_terminate", "description": "강제 종료 후 대기"},
    {"command": "lab_service_restart", "description": "제어 서비스 재시작"},
    {"command": "lab_status", "description": "현재 상태 조회"},
    {"command": "lab_watchlist", "description": "감시 종목 요약"},
    {"command": "lab_log", "description": "최근 매매 내역"},
    {"command": "lab_portfolio", "description": "보유현황 통합 보기"},
    {"command": "lab_paper_test", "description": "페이퍼 테스트(종목코드 필요)"},
    {"command": "lab_help", "description": "명령 목록 보기"},
]

ParsedCommand: TypeAlias = str | tuple[str, str | None]
SERVICE_UNIT_NAME = "kinvest-telegram-control.service"


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
        self.active_session_id: str = ""

    async def run(self) -> None:
        if not self.notifier.enabled:
            raise RuntimeError("Telegram bot token/chat id are required for telegram-control.")
        self._restore_runtime_state()
        self._write_runtime_state()
        try:
            await self.notifier.set_commands(BOT_COMMANDS)
        except Exception:  # noqa: BLE001
            pass
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
                self._write_runtime_state()
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

        parsed_command = self.parse_command(text)
        if parsed_command is None:
            return

        command_name = parsed_command if isinstance(parsed_command, str) else parsed_command[0]
        self.last_command = command_name
        self.last_command_at = datetime.now(timezone.utc)

        if command_name == "help":
            await self.notifier.send(HELP_MESSAGE)
            return
        if command_name == "status":
            await self.notifier.send(self._build_status_message())
            return
        if command_name == "watchlist":
            await self.notifier.send(self._build_watchlist_message())
            return
        if command_name == "portfolio":
            await self._send_portfolio_message()
            return
        if command_name == "log":
            await self._send_recent_trade_log()
            return
        if command_name == "paper_test":
            stock_code = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._handle_paper_test(stock_code)
            return
        if command_name == "service_restart":
            await self._handle_service_restart()
            return
        if command_name == "start":
            await self._handle_start_like_command("running", "started")
            return
        if command_name == "resume":
            await self._handle_start_like_command("running", "resumed")
            return
        if command_name == "pause":
            await self._handle_pause()
            return
        if command_name == "stop":
            await self._handle_stop()
            return
        if command_name == "terminate":
            await self._handle_terminate()

    async def _handle_paper_test(self, stock_code: str | None) -> None:
        if not stock_code:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][PAPER_TEST]",
                        "실행실패=종목코드를 함께 보내주세요",
                        "예시=/lab_paper_test 005930",
                        "참고=메뉴에서 누르면 종목코드가 빠지니 직접 입력해주세요",
                    ]
                )
            )
            return

        code = stock_code.strip().upper()
        try:
            async with KisRestClient(self.config.credentials) as client:
                service = LiquidityLabService(self.config, client, self.repository, self.notifier)
                state = await service._run_domestic_paper_test([code])
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][PAPER_TEST]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        f"종목={code}",
                        f"상태=실패",
                        f"사유={exc}",
                    ]
                )
            )
            return

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][PAPER_TEST]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"종목={code}",
                    "상태=완료",
                    f"반복={self.config.liquidity_lab.domestic_paper_iterations}회",
                    f"실현손익={int(state.realized_pnl_krw):,}원",
                    f"현금={int(state.cash_krw):,}원",
                ]
            )
        )

    async def _handle_service_restart(self) -> None:
        if not self._service_restart_supported():
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][SERVICE]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        f"서비스={SERVICE_UNIT_NAME}",
                        "상태=실패",
                        "사유=systemd 사용자 서비스가 확인되지 않음",
                    ]
                )
            )
            return

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][SERVICE]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"서비스={SERVICE_UNIT_NAME}",
                    "동작=재시작",
                    "상태=요청접수",
                ]
            )
        )
        self._write_runtime_state()
        asyncio.create_task(self._restart_service_soon())

    async def _restart_service_soon(self) -> None:
        await asyncio.sleep(0.5)
        subprocess.Popen(
            ["systemctl", "--user", "restart", SERVICE_UNIT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _service_restart_supported() -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "status", SERVICE_UNIT_NAME, "--no-pager"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    async def _handle_start_like_command(self, target_mode: str, verb: str) -> None:
        if self.mode == "stopped":
            self.session_performance = SessionPerformance(started_at=datetime.now(timezone.utc))
            self.active_session_id = uuid.uuid4().hex[:12]
        elif not self.active_session_id:
            self.active_session_id = uuid.uuid4().hex[:12]
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
        summaries = self._finalize_session_summary(command="stop")
        self._write_runtime_state()
        for summary in summaries:
            await self.notifier.send(summary)

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
        summaries = self._finalize_session_summary(command="terminate")
        self._write_runtime_state()
        for summary in summaries:
            await self.notifier.send(summary)

    async def _run_cycle(self, cycle_no: int) -> None:
        """
        Execute a single liquidity-lab cycle without auto-stopping on market close.
        """
        try:
            async with KisRestClient(self.config.credentials) as client:
                service = LiquidityLabService(self.config, client, self.repository, self.notifier)
                if self.active_session_id:
                    service._session_id = self.active_session_id
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
            "telegram_update_offset": self.update_offset,
            "watch_targets": (self.last_report_summary or {}).get("watch_targets", []),
            "last_error": self.last_error,
            "notes": [
                "telegram-control daemon manages liquidity-lab loop state.",
                "Use Telegram commands to start, pause, resume, stop, or terminate.",
            ],
            "telegram_control": self._snapshot().to_dict(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _restore_runtime_state(self) -> None:
        path = self.config.storage.runtime_state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        stored_offset = payload.get("telegram_update_offset")
        if isinstance(stored_offset, int) and stored_offset >= 0:
            self.update_offset = stored_offset

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
                f"시각={format_kst_korean(now)}",
                f"모드={snapshot.mode}",
                f"사이클={snapshot.current_cycle_no}",
                f"시장상태={market_status}",
                f"다음실행={self._short_time(snapshot.next_run_at)}",
                f"다음간격={next_interval}초",
                f"최근명령={snapshot.last_command or '-'}",
                f"최근완료={self._short_time(snapshot.last_completed_at)}",
                f"최근타겟={last_report.get('primary_target') or '-'}",
                f"확정손익={int(session.get('domestic_paper_realized_pnl_krw', 0) or 0):,}원",
                f"추정청산손익={int(session.get('estimated_overseas_realized_pnl_krw', 0) or 0):,}원",
                f"감시수={len(last_report.get('watch_targets') or [])}",
                f"오류연속={self._consecutive_errors}",
                f"최근오류={snapshot.last_error or '-'}",
            ]
        )

    def _build_watchlist_message(self) -> str:
        last_report = self.last_report_summary or {}
        watch_targets = last_report.get("watch_targets") or []
        positions = self._combined_positions(last_report)
        pnl_map: dict[str, float] = {}
        for pos in positions:
            code = str(pos.get("symbol") or pos.get("stock_code") or "").upper()
            if code:
                pnl_map[code] = float(pos.get("pnl_pct", 0) or 0)
        lines = [
            "[KIS][TELEGRAM_CONTROL_WATCHLIST]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"모드={self.mode}",
            f"사이클={self.current_cycle_no}",
            f"예상호출={last_report.get('estimated_api_calls_per_cycle', '-')}",
        ]
        if not watch_targets:
            lines.append("감시종목=없음")
            if positions:
                lines.append(self._build_positions_message())
            return "\n".join(lines)

        for watch_target in watch_targets:
            symbol = str(watch_target.get("code", "")).upper()
            lines.append(self._format_watch_target_line(watch_target, pnl_pct=pnl_map.get(symbol)))
        return "\n".join(lines)

    def _build_positions_message(self) -> str:
        last_report = self.last_report_summary or {}
        positions = self._combined_positions(last_report)

        lines = [
            "[KIS][TELEGRAM_CONTROL_POSITIONS]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"사이클={self.current_cycle_no}",
        ]

        if not positions:
            lines.append("보유종목=없음")
            return "\n".join(lines)

        total_pnl_pct_sum = 0.0
        for pos in positions:
            symbol = str(pos.get("symbol") or pos.get("stock_code") or "-")
            market = format_market_korean(str(pos.get("market", "overseas")))
            qty = int(pos.get("quantity", 0) or 0)
            avg_price = float(pos.get("avg_price", 0) or 0)
            current_price = float(pos.get("current_price", 0) or 0)
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            total_pnl_pct_sum += pnl_pct
            currency = str(pos.get("currency", "USD"))
            pnl_text = format_pct(pnl_pct)
            price_text = self._format_price(current_price, currency)
            avg_text = self._format_price(avg_price, currency)
            lines.append(
                f"{market} {symbol} 수량={qty} 매입={avg_text} 현재={price_text} 손익={pnl_text}"
            )

        avg_pnl = total_pnl_pct_sum / len(positions)
        lines.append(f"평균손익={format_pct(avg_pnl)}")
        return "\n".join(lines)

    async def _send_positions_message(self) -> None:
        await self.notifier.send(self._build_positions_message())

    def _build_virtual_portfolio_message(self) -> str:
        manager = VirtualTradeManager(self.repository)
        positions = manager.list_positions()
        pending_sells = self.repository.list_virtual_sell_pending(market="overseas")
        summary = manager.performance_summary()
        now = datetime.now(timezone.utc)
        lines = [
            "[KIS][VIRTUAL_PORTFOLIO]",
            f"시각={format_kst_korean(now)}",
        ]

        lines.append("--- 보유 종목 (virtual) ---")
        if not positions:
            lines.append("보유종목=없음")
        else:
            for position in positions:
                market = format_market_korean(position.market)
                price_text = self._format_price(position.avg_price, position.currency)
                symbol_label = f"{position.symbol} (virtual)"
                lines.append(
                    f"{market} {symbol_label} 수량={position.qty} 평균단가={price_text}"
                )

        lines.append("--- 정산 대기 매도 (virtual) ---")
        if not pending_sells:
            lines.append("정산대기=없음")
        else:
            for row in pending_sells:
                market = format_market_korean(str(row.get("market", "overseas")))
                symbol = str(row.get("symbol", "-"))
                qty = int(row.get("qty", 0) or 0)
                avg_sell_price = float(row.get("avg_sell_price", 0.0) or 0.0)
                currency = str(row.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol} (virtual) 수량=-{qty} 가상매도가={self._format_price(avg_sell_price, currency)}"
                )

        lines.append("--- 누적 성과 (virtual) ---")
        if not summary:
            lines.append("성과=없음")
        else:
            for key in sorted(summary):
                item = summary[key]
                market = format_market_korean(str(item.get("market", "overseas")))
                currency = str(item.get("currency", "USD"))
                trade_count = int(item.get("trade_count", 0) or 0)
                win_count = int(item.get("win_count", 0) or 0)
                total_pnl = float(item.get("total_pnl", 0.0) or 0.0)
                win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
                pnl_text = self._format_price(total_pnl, currency)
                lines.append(
                    f"{market} 체결={trade_count} 승률={format_pct(win_rate)} 실현손익={pnl_text}"
                )
        return "\n".join(lines)

    async def _send_virtual_portfolio_message(self) -> None:
        await self.notifier.send(self._build_virtual_portfolio_message())

    def _build_portfolio_message(self) -> str:
        now = datetime.now(timezone.utc)
        lines = [
            "[KIS][포트폴리오]",
            f"시각={format_kst_korean(now)}",
        ]

        last_report = self.last_report_summary or {}
        real_positions = self._combined_positions(last_report)
        lines.append("─── 실보유 종목 ───")
        if not real_positions:
            lines.append("보유종목=없음")
        else:
            for pos in real_positions:
                symbol = str(pos.get("symbol") or pos.get("stock_code") or "-")
                market_key = str(
                    pos.get(
                        "market",
                        "domestic" if pos.get("stock_code") else "overseas",
                    )
                )
                market = format_market_korean(market_key)
                qty = int(pos.get("quantity", 0) or 0)
                avg_price = float(pos.get("avg_price", 0) or 0)
                current_price = float(pos.get("current_price", 0) or 0)
                pnl_pct = float(pos.get("pnl_pct", 0) or 0)
                currency = str(pos.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol} "
                    f"수량={qty} "
                    f"매입={self._format_price(avg_price, currency)} "
                    f"현재={self._format_price(current_price, currency)} "
                    f"손익={format_pct(pnl_pct)}"
                )

        manager = VirtualTradeManager(self.repository)
        effective_positions = self._build_effective_positions(last_report)
        lines.append("─── 가상보유 종목 ───")
        if not effective_positions:
            lines.append("가상보유=없음")
        else:
            for position in effective_positions:
                market = format_market_korean(str(position["market"]))
                price_text = self._format_price(float(position["avg_price"]), str(position["currency"]))
                lines.append(
                    f"{market} {position['symbol']} "
                    f"수량={int(position['qty'])} "
                    f"평균단가={price_text}"
                )

        pending_sells = self.repository.list_virtual_sell_pending(market="overseas")
        lines.append("─── 정산 대기 매도 ───")
        if not pending_sells:
            lines.append("정산대기=없음")
        else:
            for row in pending_sells:
                market = format_market_korean(str(row.get("market", "overseas")))
                symbol = str(row.get("symbol", "-"))
                qty = int(row.get("qty", 0) or 0)
                avg_sell_price = float(row.get("avg_sell_price", 0.0) or 0.0)
                currency = str(row.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol}(v) "
                    f"수량=-{qty} "
                    f"가상매도가={self._format_price(avg_sell_price, currency)}"
                )

        summary = manager.performance_summary()
        lines.append("─── 누적 성과 (virtual) ───")
        if not summary:
            lines.append("성과=없음")
        else:
            for key in sorted(summary):
                item = summary[key]
                market = format_market_korean(str(item.get("market", "overseas")))
                currency = str(item.get("currency", "USD"))
                trade_count = int(item.get("trade_count", 0) or 0)
                win_count = int(item.get("win_count", 0) or 0)
                total_pnl = float(item.get("total_pnl", 0.0) or 0.0)
                win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
                pnl_text = self._format_price(total_pnl, currency)
                lines.append(
                    f"{market} 체결={trade_count} "
                    f"승률={format_pct(win_rate)} "
                    f"실현손익={pnl_text}"
                )

        return "\n".join(lines)

    async def _send_portfolio_message(self) -> None:
        await self.notifier.send(self._build_portfolio_message())

    async def _send_recent_trade_log(self) -> None:
        started_at = (
            self.session_performance.started_at.isoformat()
            if getattr(self.session_performance, "started_at", None)
            else ""
        )
        await self.notifier.send(
            self._build_session_pnl_message(
                started_at=started_at,
                session_id=self.active_session_id,
            )
        )

    def _build_session_pnl_message(
        self,
        *,
        started_at: str = "",
        session_id: str = "",
    ) -> str:
        is_prod = self.config.credentials.env == "prod"
        include_virtual = not is_prod
        summary = self.repository.get_session_pnl_summary(
            session_id=session_id,
            include_virtual=include_virtual,
            after_logged_at=started_at,
        )
        parsed_started = parse_datetime(started_at)
        period_label = (format_kst(parsed_started) or "")[:16] if parsed_started else "전체"
        lines = [
            "[KIS][손익요약]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"환경={'실거래' if is_prod else '모의투자'}",
            f"기간={period_label}~",
        ]

        real = summary.get("real", {})
        total_real_trades = sum(int(item.get("trade_count", 0) or 0) for item in real.values())
        total_real_wins = sum(int(item.get("win_count", 0) or 0) for item in real.values())
        total_pnl_krw = sum(float(item.get("total_pnl_krw") or 0.0) for item in real.values())
        total_pnl_usd = sum(float(item.get("total_pnl_usd") or 0.0) for item in real.values())
        if total_real_trades > 0:
            win_rate = (total_real_wins / total_real_trades) * 100.0
            lines.append("─── 실거래 ───")
            lines.append(f"거래={total_real_trades}건 (승률 {win_rate:.0f}%)")
            if abs(total_pnl_usd) > 1e-9:
                usd_sign = "+" if total_pnl_usd >= 0 else ""
                lines.append(f"해외손익={usd_sign}${total_pnl_usd:,.2f}")
            krw_sign = "+" if total_pnl_krw >= 0 else ""
            lines.append(f"환산손익={krw_sign}{int(round(total_pnl_krw)):,}원")
            for market, stats in sorted(real.items()):
                trade_count = int(stats.get("trade_count", 0) or 0)
                win_count = int(stats.get("win_count", 0) or 0)
                market_win_rate = (win_count / trade_count * 100.0) if trade_count else 0.0
                lines.append(f"{market}: {trade_count}건 승률{market_win_rate:.0f}%")
        else:
            lines.append("실거래 내역 없음")

        if include_virtual:
            virtual = summary.get("virtual", {})
            total_virtual_trades = sum(int(item.get("trade_count", 0) or 0) for item in virtual.values())
            if total_virtual_trades > 0:
                lines.append("─── 가상거래(virtual) ───")
                for key, stats in sorted(virtual.items()):
                    trade_count = int(stats.get("trade_count", 0) or 0)
                    win_count = int(stats.get("win_count", 0) or 0)
                    pnl = float(stats.get("total_pnl") or 0.0)
                    currency_suffix = "원" if "KRW" in key else "$"
                    sign = "+" if pnl >= 0 else ""
                    win_rate = (win_count / trade_count * 100.0) if trade_count else 0.0
                    lines.append(
                        f"{key}: {trade_count}건 승률{win_rate:.0f}% 손익{sign}{pnl:,.2f}{currency_suffix}"
                    )
            else:
                lines.append("가상거래 내역 없음")
        return "\n".join(lines)

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
        batched_orders = order_result.get("batched_orders")
        if isinstance(batched_orders, list) and batched_orders:
            for item in batched_orders:
                self._accumulate_order_stats(item, market=market)
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

    def _finalize_session_summary(self, *, command: str) -> list[str]:
        ended_at = datetime.now(timezone.utc)
        started_at_iso = (
            self.session_performance.started_at.isoformat()
            if self.session_performance.started_at is not None
            else ""
        )
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
        lines = [
            "[KIS][TELEGRAM_CONTROL_SESSION_SUMMARY]",
            f"기록={record_id}",
            "모드=stopped",
            f"명령={command}",
            f"시작={summary['started_at']}",
            f"종료={summary['ended_at']}",
            f"사이클={summary['cycles_completed']}",
            f"확정손익={int(summary['domestic_paper_realized_pnl_krw']):,}원",
            f"추정청산손익={int(summary['estimated_overseas_realized_pnl_krw']):,}원",
            (
                "주문=국내 "
                f"{summary['domestic_orders_submitted']}/{summary['domestic_orders_failed']}, "
                "해외 "
                f"{summary['overseas_orders_submitted']}/{summary['overseas_orders_failed']}"
            ),
            f"종목통계={self._format_symbol_stats_inline(summary.get('symbol_stats') or {})}",
            f"최근오류={summary['last_error'] or '-'}",
        ]
        pnl_message = self._build_session_pnl_message(
            started_at=started_at_iso,
            session_id=self.active_session_id,
        )
        self.session_performance = SessionPerformance()
        self.active_session_id = ""
        combined = "\n".join([*lines, "", pnl_message])
        if len(combined) <= 3900:
            return [combined]
        return ["\n".join(lines), pnl_message]

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
                "domestic_positions": [
                    {
                        "market": "domestic",
                        "stock_code": pos.stock_code,
                        "quantity": pos.quantity,
                        "avg_price": pos.avg_price,
                        "current_price": pos.current_price,
                        "pnl_pct": pos.pnl_pct,
                        "currency": "KRW",
                    }
                    for pos in report.domestic_positions
                ],
                "overseas_positions": [
                    {
                        "market": "overseas",
                        "symbol": pos.symbol,
                        "quantity": pos.quantity,
                        "avg_price": pos.avg_price,
                        "current_price": pos.current_price,
                        "pnl_pct": pos.pnl_pct,
                        "exchange_code": pos.exchange_code,
                        "currency": "USD",
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
    def parse_command(text: str) -> ParsedCommand | None:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.lower().startswith("/lab_paper_test"):
            parts = stripped.split(maxsplit=1)
            return ("paper_test", parts[1].strip() if len(parts) > 1 else None)

        normalized = stripped.split()[0].lower()
        mapping = {
            "/lab_start": "start",
            "/lab_pause": "pause",
            "/lab_resume": "resume",
            "/lab_stop": "stop",
            "/lab_terminate": "terminate",
            "/lab_service_restart": "service_restart",
            "/lab_status": "status",
            "/lab_watchlist": "watchlist",
            "/lab_log": "log",
            "/lab_portfolio": "portfolio",
            "/lab_help": "help",
            "/start": "help",
            "/help": "help",
        }
        return mapping.get(normalized)

    @staticmethod
    def _format_watch_target_line(watch_target: dict, pnl_pct: float | None = None) -> str:
        market = format_market_korean(str(watch_target.get("market", "overseas")))
        code = str(watch_target.get("code", "-"))
        action_bias = str(watch_target.get("action_bias", "WAIT")).upper()
        strategy_flag = str(watch_target.get("strategy_flag", "") or "")
        note_raw = str(watch_target.get("note", "-"))
        note = format_reason_korean(note_raw)
        price = watch_target.get("price", "-")
        if isinstance(price, (int, float)):
            if float(price) >= 1000:
                price_text = f"{int(price):,}원"
            else:
                price_text = f"${float(price):.4f}"
        else:
            price_text = str(price)
        holding_qty = int(watch_target.get("holding_qty", 0) or 0)
        if action_bias == "HOLD" and holding_qty > 0:
            parts = [
                f"{market} {code}",
                f"상태={format_side_korean('HOLD')}",
                f"보유={holding_qty}주",
                f"전략={strategy_flag or '-'}",
            ]
            if pnl_pct is not None:
                parts.append(f"손익={format_pct(pnl_pct)}")
            return " ".join(parts)

        status_map = {
            "BUY": "매수신호",
            "SELL": "매도신호",
            "HOLD": format_side_korean("HOLD"),
            "WAIT": format_side_korean("WAIT"),
        }
        parts = [
            f"{market} {code}",
            f"상태={status_map.get(action_bias, action_bias)}",
            f"전략={strategy_flag or '-'}",
            f"가격={price_text}",
        ]
        if holding_qty > 0 and pnl_pct is not None:
            parts.append(f"보유={holding_qty}주")
            parts.append(f"손익={format_pct(pnl_pct)}")
        elif note != note_raw:
            parts.append(f"사유={note}")
        return " ".join(parts)

    @staticmethod
    def _format_price(value: float, currency: str) -> str:
        if currency == "KRW":
            return f"{int(round(value)):,}원"
        return f"${value:.4f}"

    @staticmethod
    def _short_time(value: str | None) -> str:
        if not value:
            return "-"
        parts = value.split()
        if len(parts) < 2:
            return value
        date_part = parts[0]
        time_part = parts[1]
        try:
            _, month, day = [int(chunk) for chunk in date_part.split("-")]
            hour, minute, _ = [int(chunk) for chunk in time_part.split(":")]
        except ValueError:
            return value
        return f"{month}월 {day}일 {hour:02d}:{minute:02d}"

    @staticmethod
    def _combined_positions(last_report: dict) -> list[dict]:
        return [
            *(last_report.get("domestic_positions") or []),
            *(last_report.get("overseas_positions") or []),
        ]

    def _build_effective_positions(self, last_report: dict) -> list[dict[str, object]]:
        positions = self._combined_positions(last_report)
        effective: dict[tuple[str, str], dict[str, object]] = {}

        for pos in positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            symbol = str(pos.get("symbol") or pos.get("stock_code") or "-").upper()
            currency = str(pos.get("currency", "USD"))
            key = (market, symbol)
            effective[key] = {
                "market": market,
                "symbol": symbol,
                "qty": int(pos.get("quantity", 0) or 0),
                "avg_price": float(pos.get("avg_price", 0) or 0.0),
                "currency": currency,
            }

        manager = VirtualTradeManager(self.repository)
        for position in manager.list_positions():
            key = (position.market, position.symbol.upper())
            existing = effective.get(key)
            if existing is None:
                effective[key] = {
                    "market": position.market,
                    "symbol": position.symbol.upper(),
                    "qty": position.qty,
                    "avg_price": position.avg_price,
                    "currency": position.currency,
                }
                continue
            base_qty = int(existing["qty"])
            base_avg = float(existing["avg_price"])
            next_qty = base_qty + position.qty
            next_avg = position.avg_price
            if next_qty > 0:
                next_avg = ((base_avg * base_qty) + (position.avg_price * position.qty)) / next_qty
            existing["qty"] = next_qty
            existing["avg_price"] = next_avg

        for row in self.repository.list_virtual_sell_pending(market="overseas"):
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "-")).upper()
            key = (market, symbol)
            existing = effective.get(key)
            if existing is None:
                continue
            existing["qty"] = int(existing["qty"]) - int(row.get("qty", 0) or 0)

        result = [item for item in effective.values() if int(item["qty"]) > 0]
        result.sort(key=lambda item: (str(item["market"]), str(item["symbol"])))
        return result

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)
