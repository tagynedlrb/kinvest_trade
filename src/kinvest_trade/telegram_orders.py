from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .client import parse_kis_number
from .market_sessions import (
    is_us_orderable_session_for_env,
    us_holiday_date_for_kis_session,
)
from .message_format import format_market_korean, format_reason_korean
from .time_utils import KST, format_kst_korean, parse_datetime

if TYPE_CHECKING:
    from .telegram_control import TelegramLiquidityLabController

_logger = logging.getLogger(__name__)


class OrderAdminHelper:
    """Stale-order cancellation, live open-order loading, and order-audit formatting for telegram control."""

    def __init__(self, controller: "TelegramLiquidityLabController") -> None:
        self.controller = controller

    async def send_cancel_stale_domestic_prompt(self) -> None:
        controller = self.controller
        try:
            live_open_orders = await controller._load_live_open_domestic_orders()
        except Exception as exc:  # noqa: BLE001
            await controller.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = controller._filter_stale_live_open_orders(live_open_orders)
        lines = [
            "[KIS][국내미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "동작=확인",
        ]
        if not stale_orders:
            lines.append("대상=없음 (30분 이상 국내 미체결 없음)")
            await controller.notifier.send("\n".join(lines))
            return
        lines.append(f"대상={len(stale_orders)}건")
        for row in stale_orders[:8]:
            lines.append(controller._format_live_open_domestic_order_line(row))
        if len(stale_orders) > 8:
            lines.append(f"외 {len(stale_orders) - 8}건")
        lines.extend(
            [
                "주의=확정 명령을 보내면 위 국내 미체결 주문을 KIS에 취소 요청합니다.",
                "실행=/lab_cancel_stale_domestic_confirm",
            ]
        )
        await controller.notifier.send("\n".join(lines))

    async def execute_cancel_stale_domestic_orders(
        self,
        *,
        source: str = "manual",
        candidate_orders: list[dict] | None = None,
        now: datetime | None = None,
    ) -> None:
        controller = self.controller
        from . import telegram_control as _tc

        try:
            live_open_orders = (
                candidate_orders
                if candidate_orders is not None
                else await controller._load_live_open_domestic_orders()
            )
        except Exception as exc:  # noqa: BLE001
            await controller.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = controller._filter_stale_live_open_orders(live_open_orders)
        if not stale_orders:
            await controller.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=취소대상없음",
                    ]
                )
            )
            return

        current = now or datetime.now(timezone.utc)
        if not _tc.is_krx_regular_session(current) or _tc.is_krx_holiday(current.astimezone(KST).date()):
            await controller.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(current)}",
                        "상태=장외취소보류",
                        f"대상={len(stale_orders)}건",
                        "안내=국내 정규장 중에 /lab_cancel_stale_domestic_confirm 재시도",
                    ]
                )
            )
            controller.repository.save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol="",
                detail={
                    "reason": "domestic_cancel_outside_regular_session",
                    "stale_order_count": len(stale_orders),
                    "source": source,
                },
                cycle_no=getattr(controller, "current_cycle_no", 0),
                session_id=getattr(controller, "active_session_id", ""),
            )
            return

        lines = [
            "[KIS][국내미체결취소]",
            f"시각={format_kst_korean(current)}",
            f"동작={'자동취소' if source == 'auto' else '확정취소'}",
            f"요청={len(stale_orders)}건",
        ]
        async with _tc.KisRestClient(controller.config.credentials) as client:
            for row in stale_orders[:10]:
                symbol = str(row.get("symbol") or row.get("pdno") or "").strip().upper()
                order_no = str(row.get("order_no") or row.get("odno") or "").strip()
                orgno = str(row.get("ord_gno_brno") or row.get("krx_fwdg_ord_orgno") or "").strip()
                order_division = str(row.get("ord_dvsn_cd") or "00").strip() or "00"
                exchange_code = str(
                    row.get("excg_id_dvsn_cd")
                    or row.get("excg_id_dvsn_Cd")
                    or row.get("EXCG_ID_DVSN_CD")
                    or "KRX"
                ).strip() or "KRX"
                open_qty = int(row.get("open_qty") or parse_kis_number(row.get("rmn_qty")))
                price = int(round(float(row.get("order_price") or controller._parse_float(row.get("ord_unpr")))))
                side = controller._domestic_order_side(row)
                if not symbol or not order_no or not orgno:
                    lines.append(f"{symbol or '-'} 취소실패=필수 주문정보 부족")
                    continue
                try:
                    response = await client.revise_or_cancel_domestic_order(
                        krx_order_orgno=orgno,
                        original_order_no=order_no,
                        order_division=order_division,
                        rvse_cncl_dvsn_cd="02",
                        qty=0,
                        price=0,
                        qty_all_order_yn="Y",
                        exchange_code=exchange_code,
                    )
                except Exception as exc:  # noqa: BLE001
                    error_text = str(exc)[:80]
                    if "장종료" in error_text:
                        error_text = "장종료(국내장중 재시도 필요)"
                    controller.repository.save_broker_order_event(
                        created_at=datetime.now(timezone.utc).isoformat(),
                        market="domestic",
                        symbol=symbol,
                        exchange_code=exchange_code,
                        side=side,
                        order_kind="cancel",
                        requested_qty=open_qty,
                        requested_price=price,
                        status="REJECTED",
                        reason="stale_live_order_cancel_failed",
                        broker_order_no=order_no,
                        is_virtual=0,
                        payload={
                            "original_order_no": order_no,
                            "original_order_orgno": orgno,
                            "order_division": order_division,
                            "original_order_price": price,
                            "reference_price": price,
                            "open_qty": open_qty,
                            "error": str(exc),
                        },
                    )
                    lines.append(f"{symbol} 취소실패={error_text}")
                    continue

                output = response.get("output") if isinstance(response, dict) else {}
                if not isinstance(output, dict):
                    output = {}
                cancel_order_no = str(output.get("ODNO") or output.get("odno") or order_no).strip()
                controller.repository.save_broker_order_event(
                    created_at=datetime.now(timezone.utc).isoformat(),
                    market="domestic",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    side=side,
                    order_kind="cancel",
                    requested_qty=open_qty,
                    requested_price=price,
                    status="CANCELED",
                    reason="stale_live_order_cancel",
                    broker_order_no=cancel_order_no,
                    is_virtual=0,
                    payload={
                        "original_order_no": order_no,
                        "original_order_orgno": orgno,
                        "order_division": order_division,
                        "original_order_price": price,
                        "reference_price": price,
                        "open_qty": open_qty,
                        "response": response,
                    },
                )
                name = str(row.get("name") or row.get("prdt_name") or "").strip()
                symbol_text = f"{symbol}({name})" if name else symbol
                lines.append(f"{symbol_text} 취소요청 x{open_qty} 원주문={order_no} 취소주문={cancel_order_no}")
        await controller.notifier.send("\n".join(lines))

    async def send_cancel_stale_overseas_prompt(self) -> None:
        controller = self.controller
        try:
            live_open_orders = await controller._load_live_open_overseas_orders()
        except Exception as exc:  # noqa: BLE001
            await controller.notifier.send(
                "\n".join(
                    [
                        "[KIS][해외미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = controller._filter_stale_live_open_orders(live_open_orders)
        lines = [
            "[KIS][해외미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "동작=확인",
        ]
        if not stale_orders:
            lines.append("대상=없음 (30분 이상 해외 미체결 없음)")
            await controller.notifier.send("\n".join(lines))
            return
        lines.append(f"대상={len(stale_orders)}건")
        for row in stale_orders[:8]:
            lines.append(controller._format_live_open_overseas_order_line(row))
        if len(stale_orders) > 8:
            lines.append(f"외 {len(stale_orders) - 8}건")
        lines.extend(
            [
                "주의=확정 명령을 보내면 위 해외 미체결 주문을 KIS에 취소 요청합니다.",
                "실행=/lab_cancel_stale_overseas_confirm",
            ]
        )
        await controller.notifier.send("\n".join(lines))

    async def maybe_auto_cancel_stale_domestic_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        controller = self.controller
        from . import telegram_control as _tc

        current = now or datetime.now(timezone.utc)
        if not _tc.is_krx_regular_session(current) or _tc.is_krx_holiday(current.astimezone(KST).date()):
            return False
        last_run = controller._last_auto_stale_domestic_cancel_at
        if last_run is not None:
            elapsed_min = (current - last_run).total_seconds() / 60
            if elapsed_min < 10:
                return False
        controller._last_auto_stale_domestic_cancel_at = current
        try:
            live_open_orders = await controller._load_live_open_domestic_orders()
        except Exception as exc:  # noqa: BLE001
            controller.repository.save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol="",
                detail={
                    "reason": "auto_stale_domestic_cancel_lookup_failed",
                    "error": str(exc)[:120],
                },
                cycle_no=controller.current_cycle_no,
                session_id=controller.active_session_id,
            )
            return False

        stale_orders = controller._filter_stale_live_open_orders(live_open_orders, now=current)
        bot_owned_stale_orders = controller._filter_bot_submitted_domestic_orders(stale_orders)
        if not bot_owned_stale_orders:
            return False
        await controller._execute_cancel_stale_domestic_orders(
            source="auto",
            candidate_orders=bot_owned_stale_orders,
            now=current,
        )
        return True

    def filter_bot_submitted_domestic_orders(self, rows: list[dict]) -> list[dict]:
        controller = self.controller
        if not rows:
            return []
        submitted_order_numbers = {
            str(event.get("broker_order_no", "") or "").strip()
            for event in controller.repository.list_broker_order_events(limit=500)
            if str(event.get("market", "") or "").lower() == "domestic"
            and str(event.get("status", "") or "").upper() == "SUBMITTED"
            and str(event.get("order_kind", "") or "").lower() != "cancel"
        }
        if not submitted_order_numbers:
            return []
        result: list[dict] = []
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            if order_no and order_no in submitted_order_numbers:
                result.append(row)
        return result

    async def maybe_auto_cancel_stale_overseas_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        controller = self.controller
        from . import telegram_control as _tc

        current = now or datetime.now(timezone.utc)
        env = str(getattr(controller.config.credentials, "env", "vps") or "vps")
        if (
            not is_us_orderable_session_for_env(current, env)
            or _tc.is_nyse_holiday(us_holiday_date_for_kis_session(current))
        ):
            return False
        last_run = controller._last_auto_stale_overseas_cancel_at
        if last_run is not None:
            elapsed_min = (current - last_run).total_seconds() / 60
            if elapsed_min < 10:
                return False
        controller._last_auto_stale_overseas_cancel_at = current
        try:
            live_open_orders = await controller._load_live_open_overseas_orders()
        except Exception as exc:  # noqa: BLE001
            controller.repository.save_event(
                event_type="maintenance_skip",
                market="overseas",
                symbol="",
                detail={
                    "reason": "auto_stale_overseas_cancel_lookup_failed",
                    "error": str(exc)[:120],
                },
                cycle_no=controller.current_cycle_no,
                session_id=controller.active_session_id,
            )
            return False

        stale_orders = controller._filter_stale_live_open_orders(live_open_orders, now=current)
        bot_owned_stale_orders = controller._filter_bot_submitted_overseas_orders(stale_orders)
        if not bot_owned_stale_orders:
            return False
        await controller._execute_cancel_stale_overseas_orders(
            source="auto",
            candidate_orders=bot_owned_stale_orders,
        )
        return True

    def filter_bot_submitted_overseas_orders(self, rows: list[dict]) -> list[dict]:
        controller = self.controller
        if not rows:
            return []
        submitted_events: dict[str, dict] = {}
        for event in controller.repository.list_broker_order_events(limit=500):
            order_no = str(event.get("broker_order_no", "") or "").strip()
            if (
                order_no
                and str(event.get("market", "") or "").lower() == "overseas"
                and str(event.get("status", "") or "").upper() == "SUBMITTED"
                and str(event.get("order_kind", "") or "").lower() != "cancel"
            ):
                submitted_events[order_no] = event
        if not submitted_events:
            return []
        result: list[dict] = []
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            event = submitted_events.get(order_no)
            if event is None:
                continue
            item = dict(row)
            if not str(item.get("exchange_code") or "").strip():
                item["exchange_code"] = str(event.get("exchange_code") or "NASD").strip().upper()
            if not str(item.get("side") or "").strip():
                item["side"] = str(event.get("side") or "").strip().upper()
            result.append(item)
        return result

    async def execute_cancel_stale_overseas_orders(
        self,
        *,
        source: str = "auto",
        candidate_orders: list[dict] | None = None,
    ) -> None:
        controller = self.controller
        from . import telegram_control as _tc

        try:
            live_open_orders = (
                candidate_orders
                if candidate_orders is not None
                else await controller._load_live_open_overseas_orders()
            )
        except Exception as exc:  # noqa: BLE001
            if source != "auto":
                await controller.notifier.send(
                    "\n".join(
                        [
                            "[KIS][해외미체결취소]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            "상태=조회실패",
                            f"사유={str(exc)[:120]}",
                        ]
                    )
                )
            return
        stale_orders = controller._filter_stale_live_open_orders(live_open_orders)
        if not stale_orders:
            if source != "auto":
                await controller.notifier.send(
                    "\n".join(
                        [
                            "[KIS][해외미체결취소]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            "대상=없음 (30분 이상 해외 미체결 없음)",
                        ]
                    )
                )
            return

        lines = [
            "[KIS][해외미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"동작={'자동취소' if source == 'auto' else '확정취소'}",
            f"요청={len(stale_orders)}건",
        ]
        async with _tc.KisRestClient(controller.config.credentials) as client:
            lab = _tc.LiquidityLabService(controller.config, client, controller.repository, controller.notifier)
            for row in stale_orders[:10]:
                symbol = str(row.get("symbol") or row.get("pdno") or row.get("ovrs_pdno") or "").strip().upper()
                exchange_code = str(
                    row.get("exchange_code")
                    or row.get("ovrs_excg_cd")
                    or "NASD"
                ).strip().upper()
                order_no = str(row.get("order_no") or row.get("odno") or "").strip()
                open_qty = int(row.get("open_qty") or parse_kis_number(row.get("nccs_qty")))
                price = controller._parse_float(row.get("order_price") or row.get("ft_ord_unpr3"))
                side = controller._overseas_order_side(row)
                if not symbol or not order_no or open_qty <= 0:
                    lines.append(f"{symbol or '-'} 취소실패=필수 주문정보 부족")
                    continue
                try:
                    response = await lab._cancel_open_overseas_order(
                        symbol=symbol,
                        exchange_code=exchange_code,
                        pending_order={**row, "order_no": order_no, "open_qty": open_qty},
                    )
                except Exception as exc:  # noqa: BLE001
                    controller.repository.save_broker_order_event(
                        created_at=datetime.now(timezone.utc).isoformat(),
                        market="overseas",
                        symbol=symbol,
                        exchange_code=exchange_code,
                        side=side,
                        order_kind="cancel",
                        requested_qty=open_qty,
                        requested_price=price,
                        status="REJECTED",
                        reason="stale_live_overseas_order_cancel_failed",
                        broker_order_no=order_no,
                        is_virtual=0,
                        payload={
                            "original_order_no": order_no,
                            "order_division": str(row.get("ord_dvsn_cd") or "00").strip() or "00",
                            "original_order_price": price,
                            "reference_price": price,
                            "open_qty": open_qty,
                            "error": str(exc),
                        },
                    )
                    lines.append(f"{symbol} 취소실패={str(exc)[:80]}")
                    continue

                output = response.get("output") if isinstance(response, dict) else {}
                if not isinstance(output, dict):
                    output = {}
                cancel_order_no = str(output.get("ODNO") or output.get("odno") or order_no).strip()
                controller.repository.save_broker_order_event(
                    created_at=datetime.now(timezone.utc).isoformat(),
                    market="overseas",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    side=side,
                    order_kind="cancel",
                    requested_qty=open_qty,
                    requested_price=price,
                    status="CANCELED",
                    reason="stale_live_overseas_order_cancel",
                    broker_order_no=cancel_order_no,
                    is_virtual=0,
                    payload={
                        "original_order_no": order_no,
                        "order_division": str(row.get("ord_dvsn_cd") or "00").strip() or "00",
                        "original_order_price": price,
                        "reference_price": price,
                        "open_qty": open_qty,
                        "response": response,
                    },
                )
                lines.append(f"{symbol} 취소요청 x{open_qty} 원주문={order_no} 취소주문={cancel_order_no}")
        await controller.notifier.send("\n".join(lines))

    def build_recent_order_events_message(
        self,
        *,
        limit: int = 12,
        live_open_domestic_orders: list[dict] | None = None,
        live_open_domestic_error: str = "",
        live_open_orders: list[dict] | None = None,
        live_open_error: str = "",
    ) -> str:
        controller = self.controller
        rows = controller.repository.list_broker_order_events(limit=limit)
        audit_rows = controller.repository.list_submitted_order_audit_rows(limit=5, source_limit=500)
        live_open_order_keys: set[tuple[str, str]] = set()
        live_checked_markets: set[str] = set()
        if live_open_domestic_orders is not None and not live_open_domestic_error:
            live_checked_markets.add("domestic")
            live_open_order_keys.update(
                controller._live_open_order_keys("domestic", live_open_domestic_orders)
            )
        if live_open_orders is not None and not live_open_error:
            live_checked_markets.add("overseas")
            live_open_order_keys.update(
                controller._live_open_order_keys("overseas", live_open_orders)
            )
        lines = [
            "[KIS][주문기록]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "기준=주문 접수/취소/가상기록 (체결확정 아님)",
        ]
        if live_open_domestic_orders is not None or live_open_domestic_error:
            lines.append("─── live 국내 미체결 ───")
            if live_open_domestic_error:
                lines.append(f"조회실패={live_open_domestic_error[:80]}")
            elif not live_open_domestic_orders:
                lines.append("미체결=없음")
            else:
                for row in live_open_domestic_orders[:8]:
                    lines.append(controller._format_live_open_domestic_order_line(row))
        if live_open_orders is not None or live_open_error:
            lines.append("─── live 해외 미체결 ───")
            if live_open_error:
                lines.append(f"조회실패={live_open_error[:80]}")
            elif not live_open_orders:
                lines.append("미체결=없음")
            else:
                for row in live_open_orders[:8]:
                    lines.append(controller._format_live_open_overseas_order_line(row))
        if audit_rows:
            lines.append("─── 접수 후 체결확정 추적 필요 ───")
            lines.append("기준=실주문 SUBMITTED, DB상 체결확정 이벤트 없음")
            for row in audit_rows:
                lines.append(
                    controller._format_submitted_order_audit_line(
                        row,
                        live_open_order_keys=live_open_order_keys,
                        live_checked_markets=live_checked_markets,
                    )
                )
        if rows:
            lines.append("─── 내부 주문 이벤트 ───")
        if not rows:
            lines.append("주문기록=없음")
            return "\n".join(lines)

        for row in rows:
            created_at = parse_datetime(row.get("created_at"))
            time_text = format_kst_korean(created_at) if created_at else "-"
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "-")).upper()
            side = str(row.get("side", "")).upper()
            status = str(row.get("status", "") or "-").upper()
            action = controller._format_order_event_action(row)
            qty = int(row.get("requested_qty", 0) or 0)
            price = float(row.get("requested_price", 0.0) or 0.0)
            currency = "KRW" if market == "domestic" else "USD"
            price_text = "-" if price <= 0 else controller._format_price(price, currency)
            reason = format_reason_korean(str(row.get("reason", "") or "-"))
            order_no = str(row.get("broker_order_no", "") or "").strip()
            virtual_note = " virtual" if int(row.get("is_virtual", 0) or 0) else ""
            parts = [
                f"{time_text} {format_market_korean(market)} {symbol}{virtual_note}",
                action,
                price_text,
                f"x{qty}",
                f"상태={status}",
                f"사유={reason}",
            ]
            if order_no:
                parts.append(f"주문번호={order_no}")
            payload = row.get("payload_json") or {}
            if status == "REJECTED" and isinstance(payload, dict):
                error_text = str(payload.get("error") or "").strip()
                if error_text:
                    parts.append(f"오류={error_text[:80]}")
            if side and side not in {"BUY", "SELL"}:
                parts.append(f"원시구분={side}")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    @staticmethod
    def live_open_order_keys(market: str, rows: list[dict]) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            if order_no:
                result.add((market, order_no))
        return result

    async def load_live_open_domestic_orders(self, *, limit: int = 12) -> list[dict]:
        controller = self.controller
        from . import telegram_control as _tc

        now_kst = datetime.now(timezone.utc).astimezone(KST)
        trade_date = now_kst.strftime("%Y%m%d")
        async with _tc.KisRestClient(controller.config.credentials) as client:
            history = await client.get_domestic_order_history(
                symbol="",
                start_date=trade_date,
                end_date=trade_date,
                side_filter="00",
                fill_filter="02",
                query_order="00",
                query_type="00",
                exchange_code="KRX",
            )
        return controller._parse_live_open_domestic_order_rows(history.get("orders", []), limit=limit)

    def parse_live_open_domestic_order_rows(self, rows: list[dict], *, limit: int = 12) -> list[dict]:
        controller = self.controller
        parsed: list[dict] = []
        for row in rows:
            open_qty = parse_kis_number(row.get("rmn_qty"))
            if open_qty <= 0:
                order_qty = parse_kis_number(row.get("ord_qty"))
                filled_qty = parse_kis_number(row.get("tot_ccld_qty"))
                canceled_qty = parse_kis_number(row.get("cncl_cfrm_qty"))
                rejected_qty = parse_kis_number(row.get("rjct_qty"))
                open_qty = max(0, order_qty - filled_qty - canceled_qty - rejected_qty)
            if open_qty <= 0:
                continue
            if str(row.get("cncl_yn", "") or "").strip().upper() == "Y":
                continue
            item = dict(row)
            item["open_qty"] = open_qty
            item["symbol"] = str(row.get("pdno") or "").strip().upper()
            item["name"] = str(row.get("prdt_name") or "").strip()
            item["order_no"] = str(row.get("odno") or "").strip()
            item["order_price"] = controller._parse_float(row.get("ord_unpr"))
            item["created_at"] = controller._parse_domestic_order_history_timestamp(row)
            parsed.append(item)
        parsed.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return parsed[:limit]

    @staticmethod
    def parse_domestic_order_history_timestamp(row: dict) -> datetime | None:
        ord_dt = str(row.get("ord_dt") or "").strip()
        ord_tmd = str(row.get("ord_tmd") or "").strip()
        if not ord_dt or not ord_tmd:
            return None
        ord_tmd = ord_tmd.zfill(6)[:6]
        try:
            parsed = datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=KST).astimezone(timezone.utc)

    def format_live_open_domestic_order_line(
        self,
        row: dict,
        *,
        now: datetime | None = None,
    ) -> str:
        controller = self.controller
        from . import telegram_control as _tc

        created_at = row.get("created_at")
        time_text = format_kst_korean(created_at) if isinstance(created_at, datetime) else "-"
        symbol = str(row.get("symbol") or row.get("pdno") or "-").upper()
        name = str(row.get("name") or row.get("prdt_name") or "").strip()
        symbol_text = f"{symbol}({name})" if name else symbol
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
        if side_code == "01" or side_name == "매도":
            side_text = "매도미체결"
        elif side_code == "02" or side_name == "매수":
            side_text = "매수미체결"
        else:
            side_text = "미체결"
        qty = int(row.get("open_qty") or parse_kis_number(row.get("rmn_qty")))
        price = controller._parse_float(row.get("order_price") or row.get("ord_unpr"))
        price_text = "-" if price <= 0 else controller._format_price(price, "KRW")
        order_no = str(row.get("order_no") or row.get("odno") or "").strip()
        parts = [
            f"{time_text} 국내 {symbol_text}",
            side_text,
            price_text,
            f"x{qty}",
        ]
        if order_no:
            parts.append(f"주문번호={order_no}")
        current = now or datetime.now(timezone.utc)
        age_parts = controller._format_open_order_age_parts(created_at, now=current)
        parts.extend(age_parts)
        if "주의=장기미체결" in age_parts and not _tc.is_krx_regular_session(current):
            parts.append("취소가능=국내장중")
        return " ".join(parts)

    async def load_live_open_overseas_orders(self, *, limit: int = 12) -> list[dict]:
        controller = self.controller
        from . import telegram_control as _tc

        now_kst = datetime.now(timezone.utc).astimezone(KST)
        start_date = (now_kst - timedelta(days=1)).strftime("%Y%m%d")
        end_date = now_kst.strftime("%Y%m%d")
        env = str(getattr(controller.config.credentials, "env", "vps") or "vps")
        async with _tc.KisRestClient(controller.config.credentials) as client:
            if env != "prod":
                history = await client.get_overseas_order_history(
                    symbol="",
                    start_date=start_date,
                    end_date=end_date,
                    side_filter="00",
                    fill_filter="00",
                    exchange_code="",
                    sort_sqn="DS",
                )
                return controller._parse_live_open_overseas_order_rows(history.get("orders", []), limit=limit)

            service = _tc.LiquidityLabService(controller.config, client, controller.repository, controller.notifier)
            results: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for event in controller.repository.list_broker_order_events(limit=200):
                if str(event.get("market", "")).lower() != "overseas":
                    continue
                if int(event.get("is_virtual", 0) or 0):
                    continue
                symbol = str(event.get("symbol", "") or "").strip().upper()
                exchange_code = str(event.get("exchange_code") or "NASD").strip().upper()
                key = (symbol, exchange_code)
                if not symbol or key in seen:
                    continue
                seen.add(key)
                results.extend(
                    await service._list_open_overseas_orders(
                        symbol=symbol,
                        exchange_code=exchange_code,
                    )
                )
                if len(seen) >= 10 or len(results) >= limit:
                    break
            return sorted(
                results,
                key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[:limit]

    def parse_live_open_overseas_order_rows(self, rows: list[dict], *, limit: int = 12) -> list[dict]:
        controller = self.controller
        from . import telegram_control as _tc

        parsed: list[dict] = []
        for row in rows:
            open_qty = parse_kis_number(row.get("nccs_qty"))
            if open_qty <= 0:
                continue
            item = dict(row)
            item["open_qty"] = open_qty
            item["symbol"] = str(row.get("pdno") or row.get("ovrs_pdno") or "").strip().upper()
            item["order_no"] = str(row.get("odno") or "").strip()
            item["order_price"] = controller._parse_float(row.get("ft_ord_unpr3"))
            item["created_at"] = _tc.LiquidityLabService._parse_overseas_order_history_timestamp(row)
            parsed.append(item)
        parsed.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return parsed[:limit]

    def format_live_open_overseas_order_line(self, row: dict) -> str:
        controller = self.controller
        created_at = row.get("created_at")
        time_text = format_kst_korean(created_at) if isinstance(created_at, datetime) else "-"
        symbol = str(row.get("symbol") or row.get("pdno") or row.get("ovrs_pdno") or "-").upper()
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_text = "매도미체결" if side_code == "01" else "매수미체결" if side_code == "02" else "미체결"
        qty = int(row.get("open_qty") or parse_kis_number(row.get("nccs_qty")))
        price = controller._parse_float(row.get("order_price") or row.get("ft_ord_unpr3"))
        price_text = "-" if price <= 0 else controller._format_price(price, "USD")
        order_no = str(row.get("order_no") or row.get("odno") or "").strip()
        parts = [
            f"{time_text} 해외 {symbol}",
            side_text,
            price_text,
            f"x{qty}",
        ]
        if order_no:
            parts.append(f"주문번호={order_no}")
        age_parts = controller._format_open_order_age_parts(created_at)
        parts.extend(age_parts)
        return " ".join(parts)

    def filter_stale_live_open_orders(
        self,
        rows: list[dict],
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[dict]:
        current = now or datetime.now(timezone.utc)
        result: list[dict] = []
        for row in rows:
            created_at = row.get("created_at")
            if not isinstance(created_at, datetime):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_min = int(max((current - created_at).total_seconds(), 0.0) // 60)
            if age_min >= stale_threshold_min:
                result.append(row)
        return result

    @staticmethod
    def domestic_order_side(row: dict) -> str:
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
        if side_code == "01" or side_name == "매도":
            return "SELL"
        if side_code == "02" or side_name == "매수":
            return "BUY"
        return ""

    @staticmethod
    def overseas_order_side(row: dict) -> str:
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or row.get("sll_buy_dvsn_name") or "").strip()
        raw_side = str(row.get("side") or "").strip().upper()
        if side_code == "01" or side_name == "매도" or raw_side == "SELL":
            return "SELL"
        if side_code == "02" or side_name == "매수" or raw_side == "BUY":
            return "BUY"
        return ""

    @staticmethod
    def format_open_order_age_parts(
        created_at: object,
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[str]:
        if not isinstance(created_at, datetime):
            return []
        current = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_min = int(max((current - created_at).total_seconds(), 0.0) // 60)
        if age_min < 60:
            age_text = f"{age_min}분"
        else:
            hours, minutes = divmod(age_min, 60)
            age_text = f"{hours}시간{minutes:02d}분"
        parts = [f"경과={age_text}"]
        if age_min >= stale_threshold_min:
            parts.append("주의=장기미체결")
        return parts

    @staticmethod
    def format_order_event_action(row: dict) -> str:
        status = str(row.get("status", "") or "").upper()
        side = str(row.get("side", "") or "").upper()
        order_kind = str(row.get("order_kind", "") or "").lower()
        is_virtual = bool(int(row.get("is_virtual", 0) or 0))
        if status == "CANCELED":
            return "취소"
        if status in {"REJECTED", "FAILED"}:
            if order_kind == "cancel":
                return "취소거부"
            return "주문거부"
        if status == "RECORDED":
            if is_virtual and side == "BUY":
                return "가상매수기록"
            if is_virtual and side == "SELL":
                return "가상매도기록"
            return "기록"
        if side == "BUY":
            return "매수접수"
        if side == "SELL":
            return "매도접수"
        return status or "-"

    def format_submitted_order_audit_line(
        self,
        row: dict,
        *,
        live_open_order_keys: set[tuple[str, str]] | None = None,
        live_checked_markets: set[str] | None = None,
    ) -> str:
        controller = self.controller
        created_at = parse_datetime(row.get("created_at"))
        time_text = format_kst_korean(created_at) if created_at else "-"
        market = str(row.get("market", "overseas"))
        symbol = str(row.get("symbol", "-")).upper()
        side = str(row.get("side", "")).upper()
        side_text = "매수접수" if side == "BUY" else "매도접수" if side == "SELL" else "접수"
        qty = int(row.get("requested_qty", 0) or 0)
        price = float(row.get("requested_price", 0.0) or 0.0)
        currency = "KRW" if market == "domestic" else "USD"
        price_text = "-" if price <= 0 else controller._format_price(price, currency)
        order_no = str(row.get("broker_order_no", "") or "").strip()
        parts = [
            f"{time_text} {format_market_korean(market)} {symbol}",
            side_text,
            price_text,
            f"x{qty}",
            "확인필요=MTS/잔고",
        ]
        parts.extend(controller._format_open_order_age_parts(created_at))
        if order_no:
            parts.append(f"주문번호={order_no}")
            live_key = (market, order_no)
            if live_key in (live_open_order_keys or set()):
                parts.append("브로커상태=미체결")
            elif market in (live_checked_markets or set()):
                parts.append("브로커상태=미체결목록없음")
        followup_status = str(row.get("followup_status") or "").strip().upper()
        if followup_status:
            followup_reason = format_reason_korean(str(row.get("followup_reason") or "-"))
            followup_action = controller._format_order_event_action(
                {
                    "status": followup_status,
                    "order_kind": "cancel",
                    "side": side,
                    "is_virtual": 0,
                }
            )
            parts.append(f"후속={followup_action}")
            parts.append(f"후속사유={followup_reason}")
        return " ".join(parts)
