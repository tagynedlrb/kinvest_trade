from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .client import parse_kis_number
from .inverse_policy import INVERSE_BENCHMARK_ALIGNMENT_VERSION
from .liquidity_lab import VirtualTradeManager
from .market_policy import (
    get_market_auto_trade_config,
    get_market_strategy_guard_policy,
)
from .market_sessions import (
    NEW_YORK,
    determine_loop_interval_sec,
    minutes_until_regular_session_close,
    minutes_until_next_tradeable_session,
    us_holiday_date_for_kis_session,
)
from .message_format import (
    format_krw,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_usd,
)
from .time_utils import KST, ensure_timezone, format_kst_korean, parse_datetime
from .trade_analysis import (
    compare_before_after,
    summarize_exit_forward_performance,
    summarize_market_regime_performance,
    summarize_wait_bottlenecks,
    summarize_wait_forward_performance,
)

if TYPE_CHECKING:
    from .client import KisRestClient
    from .liquidity_lab import LiquidityLabService
    from .telegram_control import TelegramLiquidityLabController

_logger = logging.getLogger(__name__)


class ReportHelper:
    """Status, watchlist, portfolio, and performance report building for telegram control."""

    def __init__(self, controller: "TelegramLiquidityLabController") -> None:
        self.controller = controller

    def loop_mode_notice(self) -> str:
        controller = self.controller
        if controller.mode == "running":
            return "실행중"
        if controller.mode == "paused":
            return "일시정지됨 (/lab_resume 가능)"
        return "중지됨 (/lab_start 필요)"

    def report_freshness_notice(self, now: datetime | None = None) -> str:
        controller = self.controller
        if not controller.last_report_summary:
            return "감시데이터=없음 (/lab_start 후 생성)"

        age_min = controller._last_report_age_minutes(now)
        if age_min is None:
            return "감시데이터=저장상태(시각불명)"

        age_text = "방금" if age_min <= 0 else f"{age_min}분 전"
        if controller.mode != "running":
            mode_text = "일시정지" if controller.mode == "paused" else "중지"
            return f"감시데이터={age_text} (저장값·루프 {mode_text})"

        if age_min >= controller._status_stale_threshold_min():
            return f"감시데이터={age_text} (지연)"
        return f"감시데이터={age_text}"

    def last_report_age_minutes(self, now: datetime | None = None) -> int | None:
        controller = self.controller
        if not controller.last_report_summary:
            return None
        ref_time = getattr(controller, "last_completed_at", None)
        if ref_time is None:
            ref_time = parse_datetime(str(controller.last_report_summary.get("scanned_at") or ""))
        if ref_time is None:
            return None

        current = now or datetime.now(timezone.utc)
        return int(max((current - ensure_timezone(ref_time)).total_seconds(), 0.0) // 60)

    def status_stale_threshold_min(self) -> int:
        controller = self.controller
        config = getattr(controller, "config", None)
        liquidity_lab = getattr(config, "liquidity_lab", None)
        interval_sec = max(int(getattr(liquidity_lab, "loop_interval_sec", 20) or 20), 1)
        return max(5, int((interval_sec * 6) // 60))

    def estimated_pnl_suffix(self, now: datetime | None = None) -> str:
        controller = self.controller
        if not controller.last_report_summary:
            return ""
        if controller.mode != "running":
            return " (저장값)"
        age_min = controller._last_report_age_minutes(now)
        if age_min is None:
            return " (저장값)"
        if age_min >= controller._status_stale_threshold_min():
            return " (지연값)"
        return ""

    async def send_status_message(self) -> None:
        controller = self.controller
        domestic_open_count: int | None = None
        overseas_open_count: int | None = None
        open_order_error = ""
        try:
            domestic_orders, overseas_orders = await asyncio.wait_for(
                asyncio.gather(
                    controller._load_live_open_domestic_orders(limit=20),
                    controller._load_live_open_overseas_orders(limit=20),
                ),
                timeout=8.0,
            )
            domestic_open_count = len(domestic_orders)
            overseas_open_count = len(overseas_orders)
        except Exception as exc:  # noqa: BLE001
            open_order_error = str(exc)[:80]
        await controller.notifier.send(
            controller._build_status_message(
                domestic_open_count=domestic_open_count,
                overseas_open_count=overseas_open_count,
                open_order_error=open_order_error,
            )
        )

    def build_status_message(
        self,
        *,
        domestic_open_count: int | None = None,
        overseas_open_count: int | None = None,
        open_order_error: str = "",
    ) -> str:
        controller = self.controller
        from . import telegram_control as _tc

        snapshot = controller._snapshot()
        session = snapshot.session_performance or {}
        last_report = snapshot.last_report_summary or {}
        now = datetime.now(timezone.utc)
        krx_holiday = bool(
            getattr(controller.config, "skip_holiday_domestic", True)
            and _tc.is_krx_holiday(now.astimezone(KST).date())
        )
        nyse_holiday = bool(
            getattr(controller.config, "skip_holiday_overseas", True)
            and _tc.is_nyse_holiday(us_holiday_date_for_kis_session(now))
        )
        krx_open = _tc.is_krx_regular_session(now) and not krx_holiday
        us_session = _tc.get_us_trading_session(now)
        us_tradeable = _tc.is_us_orderable_session_for_env(now, controller.config.credentials.env) and not nyse_holiday
        us_watchable = us_session != "closed" and not nyse_holiday
        if krx_open:
            market_status = "KRX 정규장 ✓"
        elif us_tradeable:
            market_status = f"US {us_session} ✓"
        elif us_watchable and us_session in {"daytime", "premarket", "aftermarket"}:
            env = str(getattr(controller.config.credentials, "env", "vps") or "vps")
            if env == "prod":
                market_status = f"US {us_session} (감시중)"
            else:
                market_status = f"US {us_session} (모의 주문불가·감시만)"
        elif krx_holiday and nyse_holiday:
            market_status = "KRX/US 휴장"
        elif krx_holiday:
            market_status = "KRX 휴장"
        elif nyse_holiday:
            market_status = "US 휴장"
        else:
            mins = minutes_until_next_tradeable_session(now, controller.config.credentials.env)
            hours, minutes = divmod(mins, 60)
            market_status = f"양쪽 장 닫힘 — 다음 개장까지 {hours}h{minutes:02d}m"

        next_interval = determine_loop_interval_sec(
            now,
            controller.config.credentials.env,
            controller._consecutive_errors,
        )
        next_run_text = "-" if snapshot.mode != "running" else controller._short_time(snapshot.next_run_at)
        next_interval_text = "-" if snapshot.mode != "running" else f"{next_interval}초"
        watch_count_text = controller._watch_target_count_text(last_report)
        stopped_market_warning = controller._build_stopped_open_market_warning(
            krx_open=krx_open,
            us_watchable=us_watchable,
            last_report=last_report,
        )
        lines = [
            "[KIS][TELEGRAM_CONTROL_STATUS]",
            f"시각={format_kst_korean(now)}",
            f"모드={snapshot.mode}",
            f"거래루프={controller._loop_mode_notice()}",
            f"사이클={snapshot.current_cycle_no}",
            f"시장상태={market_status}",
            controller._report_freshness_notice(now),
            f"다음실행={next_run_text}",
            f"다음간격={next_interval_text}",
            f"최근명령={snapshot.last_command or '-'}",
            f"최근완료={controller._short_time(snapshot.last_completed_at)}",
            f"최근타겟={last_report.get('primary_target') or '-'}",
            f"확정손익={int(session.get('domestic_paper_realized_pnl_krw', 0) or 0):,}원",
            "추정청산손익="
            f"{int(session.get('estimated_overseas_realized_pnl_krw', 0) or 0):,}원"
            f"{controller._estimated_pnl_suffix(now)}",
            f"감시수={watch_count_text}",
        ]
        if stopped_market_warning:
            lines.append(stopped_market_warning)
        signal_cache_status = controller._build_signal_cache_status_line(last_report)
        if signal_cache_status:
            lines.append(signal_cache_status)
        virtual_exposure_status = controller._build_virtual_exposure_status_line()
        if virtual_exposure_status:
            lines.append(virtual_exposure_status)
        sell_block_status = controller._build_recent_sell_block_status_line()
        if sell_block_status:
            lines.append(sell_block_status)
        if open_order_error:
            lines.append(f"미체결=조회실패 ({open_order_error})")
        elif domestic_open_count is not None or overseas_open_count is not None:
            domestic_count = 0 if domestic_open_count is None else domestic_open_count
            overseas_count = 0 if overseas_open_count is None else overseas_open_count
            lines.append(f"미체결=국내 {domestic_count} / 해외 {overseas_count}")
            if domestic_count or overseas_count:
                lines.append("미체결확인=/lab_orders")
                lines.append("30분 이상 미체결은 자동으로 취소됩니다")
        lines.extend(
            [
                f"오류연속={controller._consecutive_errors}",
                f"최근오류={snapshot.last_error or '-'}",
            ]
        )
        return "\n".join(lines)

    def build_stopped_open_market_warning(
        self,
        *,
        krx_open: bool,
        us_watchable: bool,
        last_report: dict,
    ) -> str:
        controller = self.controller
        if controller.mode == "running" or not (krx_open or us_watchable):
            return ""

        held_keys: set[tuple[str, str]] = set()
        for position in last_report.get("domestic_positions") or []:
            code = str(position.get("stock_code") or position.get("symbol") or "").strip().upper()
            if code:
                held_keys.add(("domestic", code))
        for position in last_report.get("overseas_positions") or []:
            symbol = str(position.get("symbol") or "").strip().upper()
            if symbol:
                held_keys.add(("overseas", symbol))

        repository = getattr(controller, "repository", None)
        if repository is not None and hasattr(repository, "list_virtual_positions"):
            try:
                for row in repository.list_virtual_positions():
                    qty = int(row.get("qty", 0) or 0)
                    symbol = str(row.get("symbol", "") or "").strip().upper()
                    market = str(row.get("market", "overseas") or "overseas").strip().lower()
                    if qty > 0 and symbol:
                        held_keys.add((market, symbol))
            except Exception as exc:  # noqa: BLE001
                _logger.warning("status_stopped_market_warning_failed error=%s", exc)

        if not held_keys:
            return ""
        market_text = "KRX/US" if krx_open and us_watchable else "KRX" if krx_open else "US"
        state_text = "일시정지" if controller.mode == "paused" else "중지"
        return (
            f"주의={market_text} 장열림·보유 {len(held_keys)}종목, "
            f"자동감시 {state_text} 조치=/lab_start"
        )

    def build_virtual_exposure_status_line(self) -> str:
        controller = self.controller
        repository = getattr(controller, "repository", None)
        if repository is None or not hasattr(repository, "list_virtual_positions"):
            return ""
        try:
            rows = repository.list_virtual_positions()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("status_virtual_exposure_failed error=%s", exc)
            return ""

        by_market_currency = controller._group_virtual_positions_by_market_currency(rows)
        if not by_market_currency:
            return ""

        lab = controller.lab_service
        last_available_usd = (
            None
            if lab is None
            else getattr(lab, "_last_overseas_available_usd", None)
        )
        max_pct = float(
            getattr(controller.config.liquidity_lab, "max_virtual_exposure_pct", 1.0) or 1.0
        )
        max_overseas_positions = controller._max_concurrent_overseas_positions()
        parts: list[str] = []
        status = ""
        for (market, currency), item in sorted(by_market_currency.items()):
            count = int(item["count"])
            notional = float(item["notional"])
            parts.append(
                f"{format_market_korean(market)} "
                f"{controller._format_notional_price(notional, currency)} "
                f"{count}종목"
            )
            if (
                not status
                and market == "overseas"
                and currency == "USD"
                and last_available_usd is not None
                and float(last_available_usd) > 0
            ):
                limit = float(last_available_usd) * max_pct
                status = "초과" if notional > limit else "정상"

        suffix: list[str] = []
        position_cap_exceeded = False
        if status:
            suffix.append(f"상태={status}")
            if status == "초과" and controller.mode != "running":
                suffix.append("감시=중지")
        if max_overseas_positions > 0:
            overseas_count = sum(
                int(item["count"])
                for (market, currency), item in by_market_currency.items()
                if market == "overseas" and currency == "USD"
            )
            if overseas_count > 0:
                position_cap_exceeded = overseas_count > max_overseas_positions
                cap_status = "초과" if position_cap_exceeded else "정상"
                suffix.append(
                    f"포지션한도={overseas_count}/{max_overseas_positions} {cap_status}"
                )
        if position_cap_exceeded and controller.mode != "running":
            if "감시=중지" not in suffix:
                suffix.append("감시=중지")
            suffix.append("조치=/lab_trim_virtual 또는 /lab_start")
        suffix.append("확인=/lab_portfolio")
        return f"가상노출={' / '.join(parts)} {' '.join(suffix)}"

    def build_signal_cache_status_line(self, last_report: dict) -> str:
        controller = self.controller
        raw_watch_targets = last_report.get("watch_targets") or []
        watch_targets = [
            target
            for target in raw_watch_targets
            if not controller._is_closed_stale_watch_target(target)
        ]
        hidden_count = len(raw_watch_targets) - len(watch_targets)
        if not watch_targets:
            if hidden_count > 0:
                return f"신호캐시=숨김 정리잔상{hidden_count} 확인=/lab_watchlist"
            return ""
        stale_count = sum(
            1
            for target in watch_targets
            if "stale_signal_cache" in str(target.get("note", ""))
        )
        if stale_count <= 0:
            return ""
        total = len(watch_targets)
        hidden_text = f" 숨김=정리잔상{hidden_count}" if hidden_count > 0 else ""
        if stale_count == total:
            return f"신호캐시={stale_count}/{total} 전체 캐시{hidden_text} 확인=/lab_watchlist"
        return f"신호캐시={stale_count}/{total} 일부 캐시{hidden_text} 확인=/lab_watchlist"

    def watch_target_count_text(self, last_report: dict) -> str:
        controller = self.controller
        raw_watch_targets = last_report.get("watch_targets") or []
        hidden_count = sum(
            1 for target in raw_watch_targets if controller._is_closed_stale_watch_target(target)
        )
        visible_count = max(0, len(raw_watch_targets) - hidden_count)
        if hidden_count <= 0:
            return str(visible_count)
        return f"{visible_count} (숨김 {hidden_count})"

    @staticmethod
    def format_recent_age_text(then: datetime | None, *, now: datetime | None = None) -> str:
        from . import telegram_control as _tc

        if then is None:
            return ""
        current = now or datetime.now(timezone.utc)
        age_min = int(max((current - ensure_timezone(then)).total_seconds(), 0.0) // 60)
        return _tc.TelegramLiquidityLabController._format_saved_price_age(age_min)

    @staticmethod
    def format_holding_age_text(
        then: datetime | None,
        *,
        now: datetime | None = None,
    ) -> str:
        if then is None:
            return ""
        current = now or datetime.now(timezone.utc)
        age_min = int(
            max((current - ensure_timezone(then)).total_seconds(), 0.0) // 60
        )
        if age_min < 60:
            return f"{age_min}분"
        age_hours = age_min / 60
        if age_hours < 48:
            return f"{age_hours:.1f}시간"
        return f"{age_hours / 24:.1f}일"

    def build_recent_sell_block_status_line(self, *, lookback_hours: int = 12) -> str:
        controller = self.controller
        repository = getattr(controller, "repository", None)
        if repository is None or not hasattr(repository, "list_event_log"):
            return ""
        try:
            rows = repository.list_event_log(event_type="trade_skip", limit=300)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("status_sell_block_summary_failed error=%s", exc)
            return ""

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
        stats: dict[tuple[str, str, str], dict[str, int | datetime | None]] = {}
        for row in rows:
            logged_at = parse_datetime(str(row.get("logged_at") or ""))
            if logged_at is not None and logged_at < cutoff:
                continue
            detail_raw = row.get("detail", "")
            try:
                detail = json.loads(detail_raw) if isinstance(detail_raw, str) else {}
            except json.JSONDecodeError:
                detail = {}
            reason = str(detail.get("reason") or "")
            if reason not in {"no_orderable_qty", "order_rejected"}:
                continue
            side = str(detail.get("side") or "").lower()
            if side and side != "sell":
                continue
            market = str(row.get("market") or "")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            key = (market, symbol, reason)
            item = stats.setdefault(key, {"count": 0, "latest": None})
            item["count"] = int(item["count"]) + 1
            if logged_at is not None:
                latest = item.get("latest")
                if latest is None or logged_at > latest:
                    item["latest"] = logged_at

        if not stats:
            return ""

        reason_labels = {
            "no_orderable_qty": "매도가능0",
            "order_rejected": "주문거부",
        }
        ranked = sorted(
            stats.items(),
            key=lambda item: (
                -int(item[1]["count"]),
                item[1]["latest"] or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        parts: list[str] = []
        for (market, symbol, reason), item in ranked[:3]:
            count = int(item["count"])
            latest_text = controller._format_recent_age_text(
                item["latest"] if isinstance(item.get("latest"), datetime) else None
            )
            latest_suffix = f" 최근={latest_text}" if latest_text else ""
            parts.append(
                f"{format_market_korean(market)} {symbol} "
                f"{reason_labels.get(reason, reason)} {count}회{latest_suffix}"
            )
        return f"매도장애({lookback_hours}h)={' / '.join(parts)} 확인=/lab_orders"

    def build_watchlist_message(self) -> str:
        controller = self.controller
        last_report = controller.last_report_summary or {}
        watch_targets = last_report.get("watch_targets") or []
        positions = controller._combined_positions(last_report)
        pnl_map: dict[tuple[str, str], float] = {}
        for pos in positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            code = str(pos.get("symbol") or pos.get("stock_code") or "").upper()
            if code:
                pnl_map[(market, code)] = float(pos.get("pnl_pct", 0) or 0)
        lab = controller.lab_service
        if lab is not None:
            balance_cache = getattr(lab, "_overseas_balance_cache", {})
            for balance in balance_cache.get("data", {}).values():
                for row in balance.get("positions", []):
                    qty_raw = row.get("ovrs_cblc_qty") or "0"
                    try:
                        qty = int(float(str(qty_raw).replace(",", "")))
                    except (ValueError, TypeError):
                        qty = 0
                    if qty <= 0:
                        continue
                    sym = str(row.get("ovrs_pdno", "")).strip().upper()
                    key = ("overseas", sym)
                    if sym and key not in pnl_map:
                        try:
                            avg = float(str(row.get("pchs_avg_pric", "0") or "0").replace(",", ""))
                            cur = float(str(row.get("now_pric2", "0") or "0").replace(",", ""))
                            pnl_map[key] = (cur - avg) / avg if avg > 0 else 0.0
                        except (ValueError, TypeError):
                            pass
        repository = getattr(controller, "repository", None)
        if repository is not None:
            for row in repository.list_lab_symbol_states(only_positions=True):
                market = str(row.get("market", "overseas"))
                sym = str(row.get("symbol", "")).strip().upper()
                if not sym:
                    continue
                pnl_map[(market, sym)] = float(row.get("pnl_pct", 0) or 0)
        lines = [
            "[KIS][TELEGRAM_CONTROL_WATCHLIST]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"모드={controller.mode}",
            f"사이클={controller.current_cycle_no}",
            controller._report_freshness_notice(),
            f"예상호출={last_report.get('estimated_api_calls_per_cycle', '-')}",
            f"미장스캔={last_report.get('overseas_scan_scope', '-')}",
        ]
        if controller.mode != "running":
            lines.append("주의=루프가 실행 중이 아니므로 아래 목록은 마지막 저장 감시데이터")
        if not watch_targets:
            lines.append("감시종목=없음")
            if positions:
                lines.append(controller._build_positions_message())
            return "\n".join(lines)

        hidden_closed_count = 0
        visible_count = 0
        for watch_target in watch_targets:
            if controller._is_closed_stale_watch_target(watch_target):
                hidden_closed_count += 1
                continue
            display_target = controller._watch_target_with_persisted_position(watch_target)
            market = str(display_target.get("market", "overseas"))
            symbol = str(display_target.get("code", "")).upper()
            lines.append(
                controller._format_watch_target_line(
                    display_target,
                    pnl_pct=pnl_map.get((market, symbol)),
                    symbol_label=controller._format_symbol_label(
                        market,
                        symbol,
                        last_report=last_report,
                    ),
                )
            )
            visible_count += 1
        if visible_count <= 0:
            lines.append("감시종목=없음")
        if hidden_closed_count > 0:
            lines.append(f"숨김=정리된 보유잔상 {hidden_closed_count}개")
        return "\n".join(lines)

    def watch_target_with_persisted_position(self, watch_target: dict) -> dict:
        controller = self.controller
        repository = getattr(controller, "repository", None)
        if repository is None or not hasattr(repository, "get_lab_symbol_state"):
            return watch_target
        try:
            holding_qty = int(float(str(watch_target.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            holding_qty = 0
        if holding_qty <= 0:
            return watch_target
        market = str(watch_target.get("market", "overseas") or "overseas").strip().lower()
        symbol = str(watch_target.get("code", "") or "").strip().upper()
        if not market or not symbol:
            return watch_target
        state = repository.get_lab_symbol_state(market, symbol)
        if state is None:
            return watch_target
        try:
            has_position = int(state.get("has_position", 0) or 0)
        except (TypeError, ValueError):
            has_position = 0
        if has_position <= 0:
            return watch_target
        display_target = dict(watch_target)
        try:
            state_qty = int(float(str(state.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            state_qty = 0
        if state_qty > 0:
            display_target["holding_qty"] = state_qty
        try:
            last_price = float(state.get("last_price", 0) or 0)
        except (TypeError, ValueError):
            last_price = 0.0
        if last_price > 0:
            display_target["price"] = last_price
        return display_target

    def is_closed_stale_watch_target(self, watch_target: dict) -> bool:
        controller = self.controller
        try:
            holding_qty = int(float(str(watch_target.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            holding_qty = 0
        if holding_qty <= 0:
            return False
        repository = getattr(controller, "repository", None)
        if repository is None or not hasattr(repository, "get_lab_symbol_state"):
            return False
        market = str(watch_target.get("market", "overseas") or "overseas").strip().lower()
        symbol = str(watch_target.get("code", "") or "").strip().upper()
        if not market or not symbol:
            return False
        state = repository.get_lab_symbol_state(market, symbol)
        if state is None:
            return False
        try:
            has_position = int(state.get("has_position", 0) or 0)
        except (TypeError, ValueError):
            has_position = 0
        return has_position <= 0

    def build_positions_message(self) -> str:
        controller = self.controller
        last_report = controller.last_report_summary or {}
        positions = controller._combined_positions(last_report)

        lines = [
            "[KIS][TELEGRAM_CONTROL_POSITIONS]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"사이클={controller.current_cycle_no}",
        ]

        if not positions:
            lines.append("보유종목=없음")
            return "\n".join(lines)

        total_pnl_pct_sum = 0.0
        for pos in positions:
            market_key = str(pos.get("market", "overseas"))
            symbol = controller._format_symbol_label(
                market_key,
                str(pos.get("symbol") or pos.get("stock_code") or "-"),
                last_report=last_report,
            )
            market = format_market_korean(market_key)
            qty = int(pos.get("quantity", 0) or 0)
            avg_price = float(pos.get("avg_price", 0) or 0)
            current_price = float(pos.get("current_price", 0) or 0)
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            total_pnl_pct_sum += pnl_pct
            currency = str(pos.get("currency", "USD"))
            pnl_text = format_pct(pnl_pct)
            price_text = controller._format_price(current_price, currency)
            avg_text = controller._format_price(avg_price, currency)
            lines.append(
                f"{market} {symbol} 수량={qty} 매입={avg_text} 현재={price_text} 손익={pnl_text}"
            )

        avg_pnl = total_pnl_pct_sum / len(positions)
        lines.append(f"평균손익={format_pct(avg_pnl)}")
        return "\n".join(lines)

    def build_position_capacity_lines(
        self,
        real_positions: list[dict],
        virtual_manager: VirtualTradeManager,
    ) -> list[str]:
        controller = self.controller
        open_keys = {
            (
                str(
                    position.get(
                        "market",
                        "domestic" if position.get("stock_code") else "overseas",
                    )
                ).strip().lower(),
                str(
                    position.get("symbol")
                    or position.get("stock_code")
                    or ""
                ).strip().upper(),
            )
            for position in real_positions
            if int(position.get("quantity", 0) or 0) > 0
        }
        open_keys.update(
            (position.market.strip().lower(), position.symbol.strip().upper())
            for position in virtual_manager.list_positions()
            if position.qty > 0
        )
        open_keys = {
            (market, symbol)
            for market, symbol in open_keys
            if market and symbol
        }

        overseas_count = sum(1 for market, _ in open_keys if market == "overseas")
        total_count = len(open_keys)
        max_overseas = controller._max_concurrent_overseas_positions()
        max_total = int(
            getattr(
                controller.config.liquidity_lab,
                "max_concurrent_total_positions",
                0,
            )
            or 0
        )

        def cap_status(count: int, limit: int) -> str:
            if count > limit:
                return "초과"
            if count == limit:
                return "가득참 신규진입=차단"
            return f"여유={limit - count}"

        lines: list[str] = []
        if max_overseas > 0:
            lines.append(
                f"해외포지션={overseas_count}/{max_overseas} "
                f"상태={cap_status(overseas_count, max_overseas)}"
            )
        if max_total > 0:
            lines.append(
                f"합산포지션={total_count}/{max_total} "
                f"상태={cap_status(total_count, max_total)}"
            )

        pending_sells = controller.repository.list_virtual_sell_pending()
        pending_symbols = {
            (
                str(row.get("market", "")).strip().lower(),
                str(row.get("symbol", "")).strip().upper(),
            )
            for row in pending_sells
            if int(row.get("qty", 0) or 0) > 0
        }
        if pending_symbols:
            lines.append(
                f"정산대기매도={len(pending_symbols)}종목 "
                "주의=KIS 체결확정 전까지 한도 점유"
            )
        return lines

    def detect_holding_mismatch_lines(
        self,
        real_positions: list[dict],
        *,
        virtual_manager: VirtualTradeManager | None = None,
    ) -> list[str]:
        """
        Flag symbols the trading loop still tracks as a real, held position
        (lab_symbol_state.has_position=1, not a virtual entry) but that don't
        appear in the currently displayed real-position list. This catches
        cases where a live balance refresh silently fails or returns a stale/
        partial result and the portfolio view falls back to cached data that
        no longer matches what the strategy is actually acting on.
        """
        controller = self.controller
        repository = getattr(controller, "repository", None)
        if repository is None:
            return []
        displayed = {
            (
                str(
                    pos.get("market", "domestic" if pos.get("stock_code") else "overseas")
                ).strip().lower(),
                str(pos.get("symbol") or pos.get("stock_code") or "").strip().upper(),
            )
            for pos in real_positions
        }
        virtual_keys: set[tuple[str, str]] = set()
        if virtual_manager is not None:
            for position in virtual_manager.list_positions():
                if position.qty > 0:
                    virtual_keys.add(
                        (str(position.market).strip().lower(), str(position.symbol).strip().upper())
                    )
        mismatches: list[str] = []
        try:
            rows = repository.list_lab_symbol_states(only_positions=True)
        except Exception:  # noqa: BLE001
            return []
        for row in rows:
            market = str(row.get("market", "overseas")).strip().lower()
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            key = (market, symbol)
            if key in virtual_keys or key in displayed:
                continue
            holding_qty = int(row.get("holding_qty", 0) or 0)
            if holding_qty <= 0:
                continue
            mismatches.append(
                f"{format_market_korean(market)} {symbol} 내부기록={holding_qty}주 "
                f"조회결과=없음 조치=재조회 또는 /lab_orders 확인"
            )
        return mismatches

    def build_portfolio_message(
        self,
        real_positions_override: list[dict] | None = None,
        price_lookup_override: dict[tuple[str, str], float] | None = None,
        virtual_exposure_available_usd: float | None = None,
    ) -> str:
        controller = self.controller
        now = datetime.now(timezone.utc)
        lines = [
            "[KIS][포트폴리오]",
            f"시각={format_kst_korean(now)}",
        ]
        if controller.mode != "running":
            lines.append(f"거래루프={controller._loop_mode_notice()}")

        last_report = controller.last_report_summary or {}
        real_positions = (
            real_positions_override
            if real_positions_override is not None
            else controller._combined_positions(last_report)
        )
        price_lookup: dict[tuple[str, str], float] = {}
        for wt in last_report.get("watch_targets", []):
            market = str(wt.get("market", "overseas"))
            code = str(wt.get("code", "")).upper()
            price = float(wt.get("price", 0) or 0)
            if code and price > 0:
                price_lookup[(market, code)] = price
        for pos in real_positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            code = str(pos.get("symbol") or pos.get("stock_code") or "").upper()
            current_price = float(pos.get("current_price", 0) or 0)
            key = (market, code)
            if code and current_price > 0 and key not in price_lookup:
                price_lookup[key] = current_price
        lab = controller.lab_service
        if lab is not None:
            balance_cache = getattr(lab, "_overseas_balance_cache", {})
            for balance in balance_cache.get("data", {}).values():
                for row in balance.get("positions", []):
                    sym = str(row.get("ovrs_pdno", "")).strip().upper()
                    key = ("overseas", sym)
                    if sym:
                        try:
                            cur = float(str(row.get("now_pric2", "0") or "0").replace(",", ""))
                            if cur > 0:
                                price_lookup[key] = cur
                        except (ValueError, TypeError):
                            pass
        repository = getattr(controller, "repository", None)
        if repository is not None:
            for row in repository.list_lab_symbol_states(only_positions=True):
                market = str(row.get("market", "overseas"))
                symbol = str(row.get("symbol", "")).strip().upper()
                key = (market, symbol)
                if not symbol:
                    continue
                last_price = float(row.get("last_price", 0) or 0)
                if last_price > 0:
                    price_lookup[key] = last_price
        if price_lookup_override:
            price_lookup.update(
                {
                    (market, symbol): price
                    for (market, symbol), price in price_lookup_override.items()
                    if symbol and price > 0
                }
            )
        lines.append("─── 실보유 종목 ───")
        if not real_positions:
            lines.append("보유종목=없음")
        else:
            for pos in real_positions:
                market_key = str(
                    pos.get(
                        "market",
                        "domestic" if pos.get("stock_code") else "overseas",
                    )
                )
                raw_symbol = str(pos.get("symbol") or pos.get("stock_code") or "-").upper()
                symbol = controller._format_symbol_label(
                    market_key,
                    raw_symbol,
                    last_report=last_report,
                )
                market = format_market_korean(market_key)
                qty = int(pos.get("quantity", 0) or 0)
                avg_price = float(pos.get("avg_price", 0) or 0)
                current_price = price_lookup.get(
                    (market_key, raw_symbol),
                    float(pos.get("current_price", 0) or 0),
                )
                pnl_pct = (
                    (current_price - avg_price) / avg_price
                    if avg_price > 0 and current_price > 0
                    else float(pos.get("pnl_pct", 0) or 0)
                )
                currency = str(pos.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol} "
                    f"수량={qty} "
                    f"매입={controller._format_price(avg_price, currency)} "
                    f"현재={controller._format_price(current_price, currency)} "
                    f"손익={format_pct(pnl_pct)}"
                )
            risk_lines = controller._build_real_position_risk_lines(
                real_positions,
                last_report=last_report,
            )
            if risk_lines:
                lines.append("─── 실보유 리스크 ───")
                lines.extend(risk_lines)

        manager = VirtualTradeManager(controller.repository)
        mismatch_lines = self.detect_holding_mismatch_lines(
            real_positions,
            virtual_manager=manager,
        )
        if mismatch_lines:
            lines.append("─── 보유상태 불일치 ───")
            lines.extend(mismatch_lines)

        capacity_lines = self.build_position_capacity_lines(
            real_positions,
            manager,
        )
        if capacity_lines:
            lines.append("─── 포지션 한도 ───")
            lines.extend(capacity_lines)

        effective_positions = controller._build_effective_positions(
            last_report,
            real_positions_override=real_positions,
        )
        lines.append("─── 전략 유효보유 ───")
        if not effective_positions:
            lines.append("유효보유=없음")
        else:
            for position in effective_positions:
                market_key = str(position["market"])
                market = format_market_korean(market_key)
                symbol = controller._format_symbol_label(
                    market_key,
                    str(position["symbol"]).upper(),
                    last_report=last_report,
                )
                currency = str(position["currency"])
                avg_price = float(position["avg_price"])
                qty = int(position["qty"])
                entry_time = parse_datetime(
                    controller.repository.get_latest_position_entry_time(
                        market_key,
                        str(position["symbol"]).upper(),
                    )
                )
                age_text = self.format_holding_age_text(
                    entry_time,
                    now=now,
                )
                age_suffix = f" 보유={age_text}" if age_text else ""
                cur_price = price_lookup.get(
                    (market_key, str(position["symbol"]).upper()),
                    0.0,
                )

                avg_text = controller._format_price(avg_price, currency)
                if cur_price > 0 and avg_price > 0:
                    pnl_pct = (cur_price - avg_price) / avg_price
                    cur_text = controller._format_price(cur_price, currency)
                    lines.append(
                        f"{market} {symbol} "
                        f"수량={qty} "
                        f"매입={avg_text} "
                        f"현재={cur_text} "
                        f"손익={format_pct(pnl_pct)}"
                        f"{age_suffix}"
                    )
                else:
                    lines.append(
                        f"{market} {symbol} "
                        f"수량={qty} "
                        f"평균단가={avg_text} "
                        f"(현재가 없음)"
                        f"{age_suffix}"
                    )

            virtual_risk_lines = controller._build_virtual_position_risk_lines(
                effective_positions,
                price_lookup,
                last_report=last_report,
            )
            if virtual_risk_lines:
                lines.append("─── 가상보유 리스크 ───")
                lines.extend(virtual_risk_lines)
            cleanup_lines = controller._build_virtual_position_cleanup_lines(
                effective_positions,
                price_lookup,
                last_report=last_report,
            )
            if cleanup_lines:
                lines.append("─── 가상보유 정리 후보 ───")
                lines.extend(cleanup_lines)

        exposure_lines = controller._build_virtual_exposure_lines(
            available_usd_override=virtual_exposure_available_usd
        )
        if exposure_lines:
            lines.append("─── 가상 노출 ───")
            lines.extend(exposure_lines)

        pending_sells = controller.repository.list_virtual_sell_pending(market="overseas")
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
                    f"가상매도가={controller._format_price(avg_sell_price, currency)}"
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
                pnl_text = controller._format_price(total_pnl, currency)
                lines.append(
                    f"{market} 체결={trade_count} "
                    f"승률={format_pct(win_rate)} "
                    f"실현손익={pnl_text}"
                )

        return "\n".join(lines)

    def build_real_position_risk_lines(
        self,
        real_positions: list[dict],
        *,
        last_report: dict,
    ) -> list[str]:
        controller = self.controller
        if not real_positions:
            return []
        domestic_policy = get_market_auto_trade_config(controller.config, "domestic")
        domestic_threshold = float(
            getattr(domestic_policy, "hard_stop_loss_pct", 0.01) or 0.01
        )
        overseas_threshold = float(
            getattr(getattr(controller.config, "liquidity_lab", None), "overseas_stop_loss_pct", 0.01)
            or 0.01
        )
        risk_lines: list[str] = []
        for pos in real_positions:
            market_key = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            threshold = domestic_threshold if market_key == "domestic" else overseas_threshold
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            if pnl_pct > -threshold:
                continue
            symbol = controller._format_symbol_label(
                market_key,
                str(pos.get("symbol") or pos.get("stock_code") or "-"),
                last_report=last_report,
            )
            qty = int(pos.get("quantity", 0) or 0)
            market = format_market_korean(market_key)
            state = "감시중" if controller.mode == "running" else "감시중지"
            risk_lines.append(
                f"{market} {symbol} 손익={format_pct(pnl_pct)} "
                f"기준={format_pct(-threshold)} 수량={qty} 상태={state}"
            )
        if risk_lines and controller.mode != "running":
            risk_lines.append("주의=거래루프가 중지되어 자동 청산 감시가 동작하지 않습니다")
        return risk_lines

    def build_virtual_position_risk_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        controller = self.controller
        if not effective_positions:
            return []
        domestic_policy = get_market_auto_trade_config(controller.config, "domestic")
        domestic_threshold = float(
            getattr(domestic_policy, "hard_stop_loss_pct", 0.01) or 0.01
        )
        overseas_threshold = float(
            getattr(getattr(controller.config, "liquidity_lab", None), "overseas_stop_loss_pct", 0.01)
            or 0.01
        )
        risk_lines: list[str] = []
        for position in effective_positions:
            market_key = str(position["market"])
            symbol_raw = str(position["symbol"]).upper()
            avg_price = float(position["avg_price"])
            qty = int(position["qty"])
            current_price = float(price_lookup.get((market_key, symbol_raw), 0.0) or 0.0)
            threshold = domestic_threshold if market_key == "domestic" else overseas_threshold
            if avg_price <= 0 or current_price <= 0:
                continue
            pnl_pct = (current_price - avg_price) / avg_price
            if pnl_pct > -threshold:
                continue
            symbol = controller._format_symbol_label(
                market_key,
                symbol_raw,
                last_report=last_report,
            )
            market = format_market_korean(market_key)
            state = "감시중" if controller.mode == "running" else "감시중지"
            risk_lines.append(
                f"{market} {symbol} 손익={format_pct(pnl_pct)} "
                f"기준={format_pct(-threshold)} 수량={qty} 상태={state}"
            )
        if risk_lines and controller.mode != "running":
            risk_lines.append("주의=거래루프가 중지되어 가상 포지션 청산 감시가 동작하지 않습니다")
        return risk_lines

    def build_virtual_position_cleanup_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        controller = self.controller
        max_overseas_positions = controller._max_concurrent_overseas_positions()
        if max_overseas_positions <= 0:
            return []
        overseas_positions = [
            position
            for position in effective_positions
            if str(position.get("market")) == "overseas" and int(position.get("qty", 0) or 0) > 0
        ]
        excess_count = len(overseas_positions) - max_overseas_positions
        if excess_count <= 0:
            return []

        opened_lookup: dict[tuple[str, str], datetime] = {}
        for row in controller.repository.list_virtual_positions():
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "")).strip().upper()
            parsed = parse_datetime(row.get("opened_at"))
            if market and symbol and parsed is not None:
                opened_lookup[(market, symbol)] = ensure_timezone(parsed)

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, object]] = []
        for position in overseas_positions:
            market_key = str(position["market"])
            symbol_raw = str(position["symbol"]).strip().upper()
            qty = int(position["qty"])
            avg_price = float(position["avg_price"])
            currency = str(position["currency"])
            current_price = float(price_lookup.get((market_key, symbol_raw), 0.0) or 0.0)
            pnl_pct = (
                (current_price - avg_price) / avg_price
                if avg_price > 0 and current_price > 0
                else None
            )
            opened_at = opened_lookup.get((market_key, symbol_raw))
            age_hours = (
                max(0.0, (now - opened_at).total_seconds() / 3600)
                if opened_at is not None
                else 0.0
            )
            candidates.append(
                {
                    "market": market_key,
                    "symbol": symbol_raw,
                    "label": controller._format_symbol_label(
                        market_key,
                        symbol_raw,
                        last_report=last_report,
                    ),
                    "qty": qty,
                    "currency": currency,
                    "notional": max(0.0, qty * avg_price),
                    "pnl_pct": pnl_pct,
                    "age_hours": age_hours,
                }
            )

        candidates.sort(
            key=lambda item: (
                float(item["pnl_pct"]) if item["pnl_pct"] is not None else 0.0,
                -float(item["age_hours"]),
                -float(item["notional"]),
            )
        )
        lines = [
            f"초과={len(overseas_positions)}/{max_overseas_positions} "
            f"정리필요={excess_count}종목",
        ]
        for item in candidates[: min(3, len(candidates))]:
            pnl_text = (
                format_pct(float(item["pnl_pct"]))
                if item["pnl_pct"] is not None
                else "현재가없음"
            )
            age_hours = float(item["age_hours"])
            age_text = f"{age_hours:.1f}h" if age_hours < 48 else f"{age_hours / 24:.1f}d"
            lines.append(
                f"{format_market_korean(str(item['market']))} {item['label']} "
                f"손익={pnl_text} "
                f"노출={controller._format_notional_price(float(item['notional']), str(item['currency']))} "
                f"보유={age_text}"
            )
        return lines

    def build_virtual_exposure_lines(
        self,
        *,
        available_usd_override: float | None = None,
    ) -> list[str]:
        controller = self.controller
        rows = controller.repository.list_virtual_positions()
        if not rows:
            return []
        by_market_currency = controller._group_virtual_positions_by_market_currency(rows)

        if not by_market_currency:
            return []

        max_pct = float(
            getattr(controller.config.liquidity_lab, "max_virtual_exposure_pct", 1.0) or 1.0
        )
        max_overseas_positions = controller._max_concurrent_overseas_positions()
        lab = controller.lab_service
        last_available_usd = available_usd_override
        if last_available_usd is None:
            last_available_usd = (
                None
                if lab is None
                else getattr(lab, "_last_overseas_available_usd", None)
            )
        lines: list[str] = []
        position_cap_exceeded = False
        for (market, currency), item in sorted(by_market_currency.items()):
            count = int(item["count"])
            notional = float(item["notional"])
            parts = [
                f"{format_market_korean(market)} 가상매수노출={controller._format_notional_price(notional, currency)}",
                f"{count}종목",
            ]
            if market == "overseas" and currency == "USD":
                parts.append(f"한도=주문가능USD x{max_pct * 100:.0f}%")
                if last_available_usd is not None and float(last_available_usd) > 0:
                    limit = float(last_available_usd) * max_pct
                    status = "초과" if notional > limit else "정상"
                    parts.append(f"최근한도={controller._format_notional_price(limit, currency)}")
                    parts.append(f"상태={status}")
                    if status == "초과" and controller.mode != "running":
                        parts.append("감시=중지")
                if max_overseas_positions > 0:
                    cap_exceeded = count > max_overseas_positions
                    if cap_exceeded:
                        position_cap_exceeded = True
                    cap_status = "초과" if cap_exceeded else "정상"
                    parts.append(f"포지션한도={count}/{max_overseas_positions} {cap_status}")
                    if cap_exceeded and controller.mode != "running":
                        parts.append("감시=중지")
            lines.append(" ".join(parts))
        if any("상태=초과 감시=중지" in line for line in lines):
            lines.append("주의=가상 노출 한도 초과 상태에서 거래루프가 중지되어 있습니다")
        if position_cap_exceeded and controller.mode != "running":
            lines.append("주의=가상 포지션 한도 초과 상태에서 거래루프가 중지되어 있습니다")
            lines.append("조치=/lab_trim_virtual 초과분 정리 또는 /lab_start 재개")
        return lines

    def _portfolio_cache_is_fresh(self) -> bool:
        # build_portfolio_message() already assembles a price_lookup from the
        # last completed cycle's watch_targets + lab_symbol_state (updated
        # every loop_interval_sec while running) before any live override is
        # applied. Re-fetching a live quote per virtual position on top of
        # that is redundant latency when that cached data is still fresh --
        # it costs one throttled API call per held symbol for a price that's
        # already been observed within the last cycle or two.
        controller = self.controller
        if controller.mode != "running":
            return False
        age_min = controller._last_report_age_minutes()
        if age_min is None:
            return False
        return age_min < controller._status_stale_threshold_min()

    async def send_portfolio_message(self) -> None:
        controller = self.controller
        from . import telegram_control as _tc

        live_real_positions = None
        live_virtual_prices: dict[tuple[str, str], float] = {}
        live_available_usd = None
        skip_live_quote_refresh = self._portfolio_cache_is_fresh()
        try:
            async with controller._new_kis_client(
                client_source="telegram_portfolio",
            ) as client:
                portfolio_lab = controller._build_portfolio_lab_service(client)
                live_real_positions = await controller._load_live_portfolio_positions(portfolio_lab)
                if not skip_live_quote_refresh:
                    live_virtual_prices = await controller._load_live_virtual_price_lookup(portfolio_lab)
                    live_available_usd = await controller._load_live_overseas_available_usd(
                        portfolio_lab,
                        real_positions=live_real_positions or [],
                        price_lookup=live_virtual_prices,
                    )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_refresh_failed error=%s", exc)
        await controller.notifier.send(
            controller._build_portfolio_message(
                real_positions_override=live_real_positions,
                price_lookup_override=live_virtual_prices,
                virtual_exposure_available_usd=live_available_usd,
            )
        )

    def build_portfolio_lab_service(self, client: KisRestClient) -> LiquidityLabService:
        controller = self.controller
        from . import telegram_control as _tc

        service = _tc.LiquidityLabService(controller.config, client, controller.repository, controller.notifier)
        existing = controller.lab_service
        if existing is not None:
            for attr in (
                "_dynamic_domestic_names",
                "_dynamic_overseas_pool",
                "_manual_overseas_pool",
                "_last_overseas_available_usd",
            ):
                if hasattr(existing, attr):
                    setattr(service, attr, getattr(existing, attr))
        return service

    async def load_live_overseas_available_usd(
        self,
        lab: LiquidityLabService,
        *,
        real_positions: list[dict],
        price_lookup: dict[tuple[str, str], float],
    ) -> float | None:
        controller = self.controller
        candidates: list[tuple[str, str, float]] = []
        for position in real_positions:
            if str(position.get("market", "")).lower() != "overseas":
                continue
            symbol = str(position.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            exchange_code = str(position.get("exchange_code") or "NASD").strip().upper()
            price = controller._parse_float(position.get("current_price"))
            if price > 0:
                candidates.append((symbol, exchange_code, price))

        if not candidates:
            manager = VirtualTradeManager(controller.repository)
            for position in manager.list_positions("overseas"):
                if int(position.qty) <= 0:
                    continue
                symbol = position.symbol.upper()
                price = price_lookup.get(("overseas", symbol), float(position.avg_price))
                if price > 0:
                    candidates.append((symbol, str(position.exchange_code or "NASD").upper(), price))
                if len(candidates) >= 3:
                    break

        for symbol, exchange_code, price in candidates[:3]:
            try:
                return await asyncio.wait_for(
                    lab._get_overseas_available_usd(
                        symbol=symbol,
                        exchange_code=exchange_code,
                        price=price,
                    ),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "portfolio_live_available_usd_failed symbol=%s error=%s",
                    symbol,
                    exc,
                )
        return None

    async def load_live_virtual_price_lookup(
        self,
        lab: LiquidityLabService | None = None,
    ) -> dict[tuple[str, str], float]:
        controller = self.controller
        lab = lab or controller.lab_service
        if lab is None:
            return {}

        result: dict[tuple[str, str], float] = {}
        manager = VirtualTradeManager(controller.repository)
        positions = [position for position in manager.list_positions("overseas") if position.qty > 0]

        async def fetch_price(position) -> tuple[tuple[str, str], float] | None:
            symbol = position.symbol.upper()
            exchange_code = str(position.exchange_code or "NASD").upper()
            try:
                quote = await asyncio.wait_for(
                    lab.client.get_overseas_price(symbol, exchange_code),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "portfolio_live_virtual_quote_failed symbol=%s error=%s",
                    symbol,
                    exc,
                )
                return None
            last_price = controller._parse_float(quote.get("last_price"))
            if last_price <= 0:
                bid = controller._parse_float(quote.get("bid"))
                ask = controller._parse_float(quote.get("ask"))
                if bid > 0 and ask > 0:
                    last_price = (bid + ask) / 2.0
                else:
                    last_price = max(bid, ask)
            if last_price > 0:
                return ("overseas", symbol), last_price
            return None

        fetched = []
        limited_positions = positions[:25]
        # KisRestClient._throttle() already serializes every call process-wide
        # to a safe minimum interval; an extra fixed sleep between batches here
        # was double-pacing on top of that and only added latency.
        batch_size = 2
        for start in range(0, len(limited_positions), batch_size):
            batch = limited_positions[start : start + batch_size]
            fetched.extend(await asyncio.gather(*(fetch_price(position) for position in batch)))
        for item in fetched:
            if item is None:
                continue
            key, price = item
            result[key] = price
        return result

    async def load_live_portfolio_positions(
        self,
        lab: LiquidityLabService | None = None,
    ) -> list[dict] | None:
        controller = self.controller
        lab = lab or controller.lab_service
        if lab is None:
            return None

        positions: list[dict] = []
        loaded_any = False

        try:
            balance = await lab.client.get_balance()
            loaded_any = True
            for row in balance.get("positions", []) or []:
                qty = int(parse_kis_number(row.get("hldg_qty")))
                if qty <= 0:
                    continue
                stock_code = str(row.get("pdno", "")).strip()
                if not stock_code:
                    continue
                avg_price = controller._parse_float(row.get("pchs_avg_pric"))
                current_price = (
                    controller._parse_float(row.get("prpr"))
                    or controller._parse_float(row.get("stck_prpr"))
                    or controller._parse_float(row.get("now_pric"))
                    or controller._parse_float(row.get("last_price"))
                    or avg_price
                )
                pnl_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
                positions.append(
                    {
                        "market": "domestic",
                        "stock_code": stock_code,
                        "quantity": qty,
                        "orderable_qty": int(parse_kis_number(row.get("ord_psbl_qty")) or qty),
                        "avg_price": avg_price,
                        "current_price": current_price,
                        "pnl_pct": pnl_pct,
                        "currency": "KRW",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_domestic_balance_failed error=%s", exc)

        try:
            overseas_positions = await lab._load_overseas_positions([])
            loaded_any = True
            for position in overseas_positions:
                item = asdict(position)
                item["market"] = "overseas"
                item["currency"] = "USD"
                positions.append(item)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_overseas_balance_failed error=%s", exc)

        if not loaded_any:
            return None
        return positions

    async def send_recent_trade_log(self) -> None:
        controller = self.controller
        started_at = (
            controller.session_performance.started_at.isoformat()
            if getattr(controller.session_performance, "started_at", None)
            else ""
        )
        await controller.notifier.send(
            controller._build_session_pnl_message(
                started_at=started_at,
                session_id=controller.active_session_id,
            )
        )

    async def send_performance_message(self, hours_text: str | None = None) -> None:
        controller = self.controller
        await controller.notifier.send(controller._build_performance_message(hours_text))

    async def send_report_message(self, report_args: str | None = None) -> None:
        controller = self.controller
        await controller.notifier.send(controller._build_report_message(report_args))

    async def send_guard_message(self) -> None:
        controller = self.controller
        await controller.notifier.send(controller._build_guard_message())

    def build_report_message(self, report_args: str | None = None) -> str:
        controller = self.controller
        now = datetime.now(timezone.utc)
        args = str(report_args or "").strip().split()
        usage = (
            "사용법=/lab_report compare 2026-07-10 또는 2026-07-10T18:00\n"
            "사용법=/lab_report wait 72\n"
            "사용법=/lab_report wait-forward 72\n"
            "사용법=/lab_report exit-forward 168\n"
            "사용법=/lab_report regime 30"
        )
        if not args:
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "실행실패=지원하지 않는 리포트 명령",
                    usage,
                ]
            )
        report_kind = args[0].lower()
        if report_kind == "compare" and len(args) == 2:
            cutoff_date = args[1]
            try:
                comparison = compare_before_after(controller.repository.db_path, cutoff_date)
            except Exception as exc:  # noqa: BLE001
                return "\n".join(
                    [
                        "[KIS][전략리포트]",
                        f"시각={format_kst_korean(now)}",
                        "실행실패=전략 비교 생성 실패",
                        f"오류={str(exc)[:120]}",
                        usage,
                    ]
                )
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "기준=세션소유 KIS 체결확정 SELL_REAL",
                    "주의=net은 기록 수수료 우선, 미기록 건은 0.5% 비용 추정",
                    comparison,
                    summarize_market_regime_performance(
                        controller.repository.db_path,
                        limit=6,
                    ),
                ]
            )
        if report_kind == "wait" and len(args) in {1, 2}:
            try:
                hours = int(args[1]) if len(args) == 2 else 72
                bottlenecks = summarize_wait_bottlenecks(
                    controller.repository.db_path,
                    hours=hours,
                    limit=12,
                )
            except Exception as exc:  # noqa: BLE001
                return "\n".join(
                    [
                        "[KIS][전략리포트]",
                        f"시각={format_kst_korean(now)}",
                        "실행실패=WAIT 병목 생성 실패",
                        f"오류={str(exc)[:120]}",
                        usage,
                    ]
                )
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "기준=cycle_log WAIT",
                    bottlenecks,
                ]
            )
        if report_kind in {"wait-forward", "wait_forward"} and len(args) in {1, 2}:
            try:
                hours = int(args[1]) if len(args) == 2 else 72
                forward_report = summarize_wait_forward_performance(
                    controller.repository.db_path,
                    hours=hours,
                    limit=4,
                    orderable_env=str(
                        getattr(controller.config.credentials, "env", "vps")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return "\n".join(
                    [
                        "[KIS][전략리포트]",
                        f"시각={format_kst_korean(now)}",
                        "실행실패=WAIT 선행성과 생성 실패",
                        f"오류={str(exc)[:120]}",
                        usage,
                    ]
                )
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "기준=cycle_log WAIT 에피소드·마감 시장레짐",
                    forward_report,
                ]
            )
        if report_kind in {"exit-forward", "exit_forward"} and len(args) in {1, 2}:
            try:
                hours = int(args[1]) if len(args) == 2 else 168
                forward_report = summarize_exit_forward_performance(
                    controller.repository.db_path,
                    hours=hours,
                    limit=6,
                    orderable_env=str(
                        getattr(controller.config.credentials, "env", "vps")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return "\n".join(
                    [
                        "[KIS][전략리포트]",
                        f"시각={format_kst_korean(now)}",
                        "실행실패=청산 후행성과 생성 실패",
                        f"오류={str(exc)[:120]}",
                        usage,
                    ]
                )
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "기준=체결확정 전략청산·마감 시장레짐",
                    forward_report,
                ]
            )
        if report_kind == "regime" and len(args) in {1, 2}:
            try:
                days = int(args[1]) if len(args) == 2 else 30
                regime_report = summarize_market_regime_performance(
                    controller.repository.db_path,
                    days=max(0, days),
                    limit=10,
                )
            except Exception as exc:  # noqa: BLE001
                return "\n".join(
                    [
                        "[KIS][전략리포트]",
                        f"시각={format_kst_korean(now)}",
                        "실행실패=시장 레짐 리포트 생성 실패",
                        f"오류={str(exc)[:120]}",
                        usage,
                    ]
                )
            return "\n".join(
                [
                    "[KIS][시장환경 리포트]",
                    f"시각={format_kst_korean(now)}",
                    regime_report,
                ]
            )
        return "\n".join(
            [
                "[KIS][전략리포트]",
                f"시각={format_kst_korean(now)}",
                "실행실패=지원하지 않는 리포트 명령",
                usage,
            ]
        )

    def build_guard_message(self) -> str:
        controller = self.controller
        now = datetime.now(timezone.utc)
        config = getattr(controller.config, "liquidity_lab", object())
        enabled = bool(getattr(config, "strategy_guard_enabled", False))
        guard_markets = {
            str(market).strip().lower()
            for market in getattr(config, "strategy_guard_markets", ["overseas"])
            if str(market).strip()
        }
        report_markets = ("domestic", "overseas")
        guard_policies = {
            market: get_market_strategy_guard_policy(
                controller.config,
                market,
            )
            for market in report_markets
        }
        in_scope_policies = [
            guard_policies[market]
            for market in report_markets
            if not guard_markets or market in guard_markets
        ]
        common_policy = (
            in_scope_policies[0]
            if in_scope_policies
            and all(
                policy == in_scope_policies[0]
                for policy in in_scope_policies[1:]
            )
            else None
        )
        lines = [
            "[KIS][전략가드]",
            f"시각={format_kst_korean(now)}",
            f"상태={'활성' if enabled else '비활성'}",
        ]
        if common_policy is not None:
            lines.extend(
                [
                    f"범위=최근 {common_policy.lookback_hours}시간",
                    (
                        f"차단조건={common_policy.min_trades}건 이상, "
                        "평균순손익 "
                        f"{format_pct(common_policy.max_avg_net_pnl_pct)} 이하 "
                        "또는 자본가중 "
                        f"{format_pct(common_policy.max_capital_weighted_net_pnl_pct)} 이하"
                    ),
                    (
                        f"감시대상={','.join(sorted(guard_markets))}:"
                        f"{','.join(sorted(common_policy.strategy_flags))}"
                    ),
                ]
            )
        else:
            lines.extend(["범위=시장별 정책", "차단조건=시장별 정책"])
            for market in report_markets:
                if guard_markets and market not in guard_markets:
                    continue
                policy = guard_policies[market]
                lines.append(
                    f"{format_market_korean(market)}정책="
                    f"{policy.lookback_hours}시간/{policy.min_trades}건/"
                    f"{format_pct(policy.max_avg_net_pnl_pct)} 이하/"
                    f"{','.join(sorted(policy.strategy_flags))}"
                    "(자본가중 "
                    f"{format_pct(policy.max_capital_weighted_net_pnl_pct)} 이하) "
                    f"해제={policy.min_final_sessions}세션"
                    + (
                        f"+회복{policy.release_min_trades}건/"
                        f"평균순>{format_pct(policy.release_min_avg_net_pnl_pct)}"
                        "/자본가중>"
                        f"{format_pct(policy.release_min_capital_weighted_net_pnl_pct)}"
                        if policy.release_requires_recovery
                        else ""
                    )
                )
        lines.append(
            "기준=세션소유 KIS 체결확정 SELL_REAL, "
            "미체결 확인=/lab_orders"
        )
        hard_blocks: list[str] = []
        if bool(getattr(config, "overseas_block_standalone_vwap", False)):
            hard_blocks.append("해외 VWAP단독")
        if bool(getattr(config, "overseas_block_standalone_rsi", False)):
            hard_blocks.append("해외 RSI단독")
        if bool(getattr(config, "overseas_block_standalone_vol", False)):
            hard_blocks.append("해외 VOL단독")
        if hard_blocks:
            basis_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if line.startswith("기준=")
                ),
                len(lines),
            )
            lines.insert(
                basis_index,
                f"고정차단={','.join(hard_blocks)}",
            )
        for market in report_markets:
            auto_trade = get_market_auto_trade_config(
                controller.config,
                market,
            )
            if auto_trade is None or not bool(
                getattr(auto_trade, "strategy_guard_probe_enabled", False)
            ):
                continue
            flags = [
                str(flag).strip().upper()
                for flag in getattr(
                    auto_trade,
                    "strategy_guard_probe_strategy_flags",
                    [],
                )
                if str(flag).strip()
            ]
            max_entries = max(
                0,
                int(
                    getattr(
                        auto_trade,
                        "strategy_guard_probe_max_entries_per_session",
                        0,
                    )
                    or 0
                ),
            )
            max_submissions = max(
                max_entries,
                int(
                    getattr(
                        auto_trade,
                        "strategy_guard_probe_max_submissions_per_session",
                        0,
                    )
                    or max_entries
                ),
            )
            session_date_resolver = getattr(
                controller.lab_service,
                "_market_session_date",
                None,
            )
            session_date = (
                session_date_resolver(market, now)
                if callable(session_date_resolver)
                else now.astimezone(
                    KST if market == "domestic" else NEW_YORK
                ).date().isoformat()
            )
            usage_reader = getattr(
                controller.repository,
                "get_strategy_guard_probe_usage",
                None,
            )
            if callable(usage_reader):
                usage = usage_reader(
                    market=market,
                    session_date=session_date,
                )
            else:
                submitted = (
                    controller.repository.count_strategy_guard_probe_submissions(
                        market=market,
                        session_date=session_date,
                    )
                    if hasattr(
                        controller.repository,
                        "count_strategy_guard_probe_submissions",
                    )
                    else 0
                )
                usage = {
                    "submission_attempts": submitted,
                    "effective_entries": submitted,
                    "filled_entries": 0,
                    "open_entries": submitted,
                    "no_fill_finalized": 0,
                }
            environment = str(
                getattr(controller.config.credentials, "env", "")
            ).strip().lower()
            environment_state = "허용" if environment == "vps" else "차단"
            slot_multiplier = float(
                getattr(
                    auto_trade,
                    "strategy_guard_probe_slot_multiplier",
                    0.0,
                )
                or 0.0
            )
            benchmark_floor = float(
                getattr(
                    auto_trade,
                    "strategy_guard_probe_benchmark_floor_pct",
                    0.0,
                )
                or 0.0
            )
            max_age_sec = int(
                getattr(
                    auto_trade,
                    "strategy_guard_probe_regime_max_age_sec",
                    600,
                )
                or 600
            )
            lines.append(
                f"검증진입={format_market_korean(market)} "
                f"{environment}전용({environment_state}) "
                f"전략={','.join(flags) or '-'} "
                f"진입={int(usage.get('effective_entries') or 0)}/{max_entries} "
                f"제출={int(usage.get('submission_attempts') or 0)}/{max_submissions} "
                f"체결={int(usage.get('filled_entries') or 0)} "
                f"열림={int(usage.get('open_entries') or 0)} "
                f"미체결종료={int(usage.get('no_fill_finalized') or 0)} "
                f"슬롯={slot_multiplier:.0%} "
                f"지수하한={benchmark_floor:+.2f}% "
                f"지표≤{max_age_sec}초"
            )
        reject_cb = getattr(controller.lab_service, "cb", None)
        reject_status = reject_cb.order_reject_status() if reject_cb is not None else {}
        active_rejects = {key: v for key, v in reject_status.items() if v.get("halted")}
        if active_rejects:
            parts = [f"{key}({v['count']}회)" for key, v in sorted(active_rejects.items())]
            lines.append(f"주문거부차단={','.join(parts)} 확인=/lab_cb_reset")
        if not enabled:
            return "\n".join(lines)
        if not hasattr(controller.repository, "get_recent_strategy_guard_performance"):
            lines.append("성과=조회불가")
            return "\n".join(lines)

        rows: list[dict] = []
        for market, guard_policy in guard_policies.items():
            rows.extend(
                controller.repository.get_recent_strategy_guard_performance(
                    after_logged_at=(
                        now
                        - timedelta(hours=guard_policy.lookback_hours)
                    ).isoformat(),
                    cost_pct=guard_policy.fallback_cost_pct,
                    market=market,
                )
            )
        rows.sort(
            key=lambda row: (
                min(
                    float(row.get("avg_net_pnl_pct") or 0.0),
                    float(
                        row.get("capital_weighted_net_pnl_pct")
                        if row.get("capital_weighted_net_pnl_pct") is not None
                        else row.get("avg_net_pnl_pct") or 0.0
                    ),
                ),
                -int(row.get("trade_count") or 0),
            )
        )
        active_states = (
            controller.repository.list_active_strategy_guard_states()
            if hasattr(
                controller.repository,
                "list_active_strategy_guard_states",
            )
            else []
        )
        active_by_key = {
            (
                str(row.get("market") or "").strip().lower(),
                str(row.get("strategy_flag") or "").strip().upper(),
            ): row
            for row in active_states
        }
        if not rows and not active_states:
            lines.append("성과=없음")
            return "\n".join(lines)

        seen_keys: set[tuple[str, str]] = set()
        for row in rows[:10]:
            market = str(row.get("market") or "").strip().lower()
            strategy = str(row.get("strategy_flag") or "").strip().upper()
            key = (market, strategy)
            seen_keys.add(key)
            trade_count = int(row.get("trade_count") or 0)
            win_count = int(row.get("win_count") or 0)
            avg_net = float(row.get("avg_net_pnl_pct") or 0.0)
            weighted_net = float(
                row.get("capital_weighted_net_pnl_pct")
                if row.get("capital_weighted_net_pnl_pct") is not None
                else avg_net
            )
            win_rate = (win_count / trade_count) if trade_count else 0.0
            guard_policy = guard_policies[market]
            monitored = (not guard_markets or market in guard_markets) and (
                not guard_policy.strategy_flags
                or strategy in guard_policy.strategy_flags
            )
            threshold_breached = (
                trade_count >= guard_policy.min_trades
                and (
                    avg_net <= guard_policy.max_avg_net_pnl_pct
                    or weighted_net
                    <= guard_policy.max_capital_weighted_net_pnl_pct
                )
            )
            blocked = monitored and threshold_breached
            persistent_state = active_by_key.get(key)
            if persistent_state is not None:
                min_final_sessions = guard_policy.min_final_sessions
                final_sessions = controller.repository.count_final_market_regimes(
                    market=market,
                    start_date=str(
                        persistent_state.get("activation_session_date") or ""
                    ),
                )
                state = (
                    f"차단(최종세션 {final_sessions}/{min_final_sessions})"
                )
            elif blocked:
                state = "차단"
            elif monitored:
                state = "감시"
            elif threshold_breached:
                state = "참고(범위밖·임계초과)"
            else:
                state = "참고"
            lines.append(
                f"{format_market_korean(market)} {strategy or '-'} "
                f"상태={state} {trade_count}건 승률={win_rate * 100:.0f}% "
                f"평균순={format_pct(avg_net)} "
                f"자본가중={format_pct(weighted_net)}"
            )
        for key, persistent_state in active_by_key.items():
            if key in seen_keys:
                continue
            market, strategy = key
            min_final_sessions = guard_policies[
                market
            ].min_final_sessions
            final_sessions = controller.repository.count_final_market_regimes(
                market=market,
                start_date=str(
                    persistent_state.get("activation_session_date") or ""
                ),
            )
            lines.append(
                f"{format_market_korean(market)} {strategy or '-'} "
                f"상태=차단(관찰유지) 최종세션="
                f"{final_sessions}/{min_final_sessions} "
                f"시작={persistent_state.get('activation_session_date') or '-'}"
            )
        return "\n".join(lines)

    @staticmethod
    def parse_performance_hours(hours_text: str | None) -> int:
        try:
            hours = int(float(str(hours_text or "24").strip()))
        except (TypeError, ValueError):
            hours = 24
        return min(max(hours, 1), 720)

    @staticmethod
    def format_mixed_pnl(*, usd: float, krw: float) -> str:
        parts: list[str] = []
        if abs(usd) > 1e-9:
            parts.append(format_usd(usd))
        if abs(krw) > 0.5:
            parts.append(format_krw(krw))
        return "/".join(parts) if parts else "0"

    @staticmethod
    def performance_row_score(row: dict) -> tuple[float, float]:
        return (
            float(row.get("total_net_pnl_krw") or 0.0),
            float(row.get("total_net_pnl_usd") or 0.0),
        )

    def format_performance_row(self, row: dict) -> str:
        controller = self.controller
        market = format_market_korean(str(row.get("market") or "-"))
        strategy = str(row.get("strategy_flag") or "-")
        entry_by = str(row.get("entry_by") or "-")
        exit_reason = str(row.get("exit_reason") or row.get("exit_by") or "-")
        exit_signal_by = str(row.get("exit_signal_by") or "")
        trade_count = int(row.get("trade_count") or 0)
        win_rate = float(row.get("win_rate") or 0.0)
        avg_pnl = float(row.get("avg_pnl_pct") or 0.0)
        pnl_label = controller._format_mixed_pnl(
            usd=float(row.get("total_net_pnl_usd") or 0.0),
            krw=float(row.get("total_net_pnl_krw") or 0.0),
        )
        exit_signal_label = ""
        if exit_signal_by not in {"", "-"} and exit_signal_by != exit_reason:
            exit_signal_label = f" 신호={format_reason_korean(exit_signal_by)}"
        return (
            f"{market} {strategy} "
            f"진입={entry_by} 청산={format_reason_korean(exit_reason)}"
            f"{exit_signal_label} "
            f"{trade_count}건 승률={win_rate * 100:.0f}% "
            f"평균={format_pct(avg_pnl)} 손익={pnl_label}"
        )

    @staticmethod
    def format_inverse_shadow_trade(row: dict) -> str:
        market = str(row.get("market") or "")
        symbol = str(row.get("symbol") or "-")
        status = str(row.get("status") or "").upper()
        context = row.get("context_json")
        context = context if isinstance(context, dict) else {}
        entry_regime = context.get("entry_inverse_benchmark_context")
        if not isinstance(entry_regime, dict):
            entry_regime = context.get("entry_market_regime")
        entry_regime = entry_regime if isinstance(entry_regime, dict) else {}
        exit_regime = context.get("exit_inverse_benchmark_context")
        if not isinstance(exit_regime, dict):
            exit_regime = context.get("exit_market_regime")
        exit_regime = exit_regime if isinstance(exit_regime, dict) else {}

        def optional_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        minutes_to_close = optional_float(
            entry_regime.get("minutes_to_regular_close")
        )
        if minutes_to_close is None:
            opened_at = parse_datetime(row.get("opened_at"))
            if opened_at is not None:
                minutes_to_close = minutes_until_regular_session_close(
                    market,
                    opened_at,
                )
        entry_return = optional_float(row.get("benchmark_return_pct"))
        rebound_from_low = optional_float(
            entry_regime.get("benchmark_rebound_from_low_pct")
        )
        range_position = optional_float(
            entry_regime.get("session_range_position")
        )
        exit_return = optional_float(exit_regime.get("return_pct"))
        entry_price = optional_float(row.get("entry_price"))
        peak_price = optional_float(row.get("peak_price"))
        trough_price = optional_float(row.get("trough_price"))
        exit_price = optional_float(row.get("exit_price"))

        parts = [
            f"최근 {format_market_korean(market)} {symbol}",
            "진행" if status == "OPEN" else "종료",
        ]
        if entry_return is not None:
            parts.append(f"진입지수={entry_return:+.2f}%")
        if rebound_from_low is not None:
            parts.append(f"저점반등={rebound_from_low:+.2f}%p")
        if range_position is not None:
            parts.append(f"범위위치={range_position * 100:.0f}%")
        if minutes_to_close is not None:
            parts.append(f"마감잔여={minutes_to_close:.0f}분")
        if entry_return is not None and exit_return is not None:
            parts.append(f"지수변화={exit_return - entry_return:+.2f}%p")
        if entry_price is not None and entry_price > 0:
            if peak_price is not None:
                parts.append(
                    f"MFE={format_pct((peak_price - entry_price) / entry_price)}"
                )
            if trough_price is not None:
                parts.append(
                    f"MAE={format_pct((trough_price - entry_price) / entry_price)}"
                )
            if status == "CLOSED" and peak_price is not None and exit_price is not None:
                parts.append(
                    "고점반납="
                    f"{format_pct((peak_price - exit_price) / entry_price)}"
                )
        if status == "CLOSED":
            parts.append(
                f"순수익={format_pct(float(row.get('net_pnl_pct') or 0.0))}"
            )
            parts.append(
                f"청산={format_reason_korean(str(row.get('exit_reason') or '-'))}"
            )
        benchmark_name = str(row.get("benchmark_name") or "-")
        if (
            context.get("benchmark_alignment_version")
            == INVERSE_BENCHMARK_ALIGNMENT_VERSION
        ):
            parts.append(f"기준={benchmark_name}(상품정확)")
        else:
            parts.append(f"기준={benchmark_name}(과거대용·평가제외)")
        return " ".join(parts)

    def build_performance_message(self, hours_text: str | None = None) -> str:
        controller = self.controller
        hours = controller._parse_performance_hours(hours_text)
        now = datetime.now(timezone.utc)
        after_logged_at = (now - timedelta(hours=hours)).isoformat()
        rows = controller.repository.get_realized_strategy_performance(
            after_logged_at=after_logged_at,
            limit=200,
        )
        shadow_rows = controller.repository.get_inverse_shadow_performance(
            after_opened_at=after_logged_at,
        )
        recent_shadow_rows = (
            controller.repository.list_inverse_shadow_trades(
                after_opened_at=after_logged_at,
                limit=3,
            )
            if hasattr(
                controller.repository,
                "list_inverse_shadow_trades",
            )
            else []
        )
        inverse_observations = (
            controller.repository.get_inverse_policy_observation_summary(
                after_logged_at=after_logged_at,
                limit=6,
            )
            if hasattr(
                controller.repository,
                "get_inverse_policy_observation_summary",
            )
            else []
        )
        lines = [
            "[KIS][전략성과]",
            f"시각={format_kst_korean(now)}",
            f"범위=최근 {hours}시간",
            "기준=세션소유 KIS 체결확정 SELL_REAL만 집계",
            "제외=외부보유·감시 신호 BUY/SELL/HOLD",
            "검증=주문번호별 체결원장, 확인=/lab_orders",
        ]
        if not rows:
            lines.append("성과=없음")
        else:
            total_trades = sum(int(row.get("trade_count") or 0) for row in rows)
            total_wins = sum(int(row.get("win_count") or 0) for row in rows)
            total_usd = sum(float(row.get("total_net_pnl_usd") or 0.0) for row in rows)
            total_krw = sum(float(row.get("total_net_pnl_krw") or 0.0) for row in rows)
            total_win_rate = (total_wins / total_trades) if total_trades else 0.0
            lines.append(
                "전체="
                f"{total_trades}건 승률={total_win_rate * 100:.0f}% "
                f"손익={controller._format_mixed_pnl(usd=total_usd, krw=total_krw)}"
            )
            best_rows = rows[:5]
            worst_rows = sorted(rows, key=controller._performance_row_score)[:5]
            lines.append("─── 상위 전략 ───")
            for row in best_rows:
                lines.append(controller._format_performance_row(row))
            lines.append("─── 하위 전략 ───")
            for row in worst_rows:
                lines.append(controller._format_performance_row(row))
        lines.append("─── 역방향 shadow ───")
        if shadow_rows:
            lines.append(
                "정책평가=상품정확 기준지수 "
                f"{INVERSE_BENCHMARK_ALIGNMENT_VERSION} 표본만 사용"
            )

            def append_shadow_summary(
                row: dict,
                *,
                prefix: str,
                label: str,
            ) -> None:
                def value(name: str) -> object:
                    return row.get(f"{prefix}{name}")

                closed_count = int(value("closed_count") or 0)
                win_count = int(value("win_count") or 0)
                win_rate = win_count / closed_count if closed_count else 0.0
                lines.append(
                    f"{format_market_korean(str(row.get('market') or ''))} "
                    f"{label} "
                    f"상품종료={closed_count} "
                    f"상품진행={int(value('open_count') or 0)} "
                    f"관찰세션={int(value('observed_session_count') or 0)} "
                    f"종료세션={int(value('closed_session_count') or 0)} "
                    f"승률={win_rate * 100:.0f}% "
                    "평균순수익="
                    f"{format_pct(float(value('avg_net_pnl_pct') or 0.0))}"
                    + (
                        " "
                        "평균MFE="
                        f"{format_pct(float(value('avg_mfe_pct') or 0.0))} "
                        "평균MAE="
                        f"{format_pct(float(value('avg_mae_pct') or 0.0))} "
                        "평균고점반납="
                        f"{format_pct(float(value('avg_peak_giveback_pct') or 0.0))}"
                        if closed_count
                        else ""
                    )
                )

            for row in shadow_rows:
                append_shadow_summary(
                    row,
                    prefix="comparable_",
                    label="상품정확",
                )
                if (
                    int(row.get("legacy_open_count") or 0)
                    or int(row.get("legacy_closed_count") or 0)
                ):
                    append_shadow_summary(
                        row,
                        prefix="legacy_",
                        label="과거대용지표(평가제외)",
                    )
            for row in recent_shadow_rows:
                lines.append(self.format_inverse_shadow_trade(row))
        else:
            lines.append(
                "상품종료=0 상품진행=0 관찰세션=0 진입표본=없음"
            )
        lines.append("─── 역방향 관측 ───")
        if not inverse_observations:
            lines.append("관측=없음(해당 기간 영구 로그 없음)")
        else:
            event_labels = {
                "inverse_regime_observed": "레짐",
                "inverse_symbol_regime_observed": "상품기준지수",
                "inverse_quote_failed": "조회실패",
                "inverse_quote_excluded": "후보제외",
                "inverse_product_blocked": "상품차단",
                "inverse_product_ready": "상품준비",
            }
            for row in inverse_observations:
                symbols = ",".join(row.get("symbols") or [])
                symbol_label = f" 종목={symbols}" if symbols else ""
                count_unit = "세션·종목" if symbols else "세션"
                lines.append(
                    f"{format_market_korean(str(row.get('market') or ''))} "
                    f"{event_labels.get(str(row.get('event_type') or ''), '관측')}="
                    f"{format_reason_korean(str(row.get('reason') or 'unknown'))} "
                    f"{int(row.get('observation_count') or 0)}{count_unit}"
                    f"{symbol_label}"
                )
        return "\n".join(lines)
