from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .client import parse_kis_number
from .market_sessions import KST

if TYPE_CHECKING:
    from .liquidity_lab import LiquidityLabService

_logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    "FILLED",
    "CANCELED",
    "PARTIAL_CANCELED",
    "REJECTED",
}


def _as_float(value: object) -> float:
    try:
        text = str(value or "").strip().replace(",", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


class BrokerExecutionReconciler:
    """Reconcile submitted KIS orders to fills before recording performance."""

    def __init__(self, service: "LiquidityLabService") -> None:
        self.service = service

    async def reconcile(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        markets: set[str] | None = None,
    ) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        repository = self.service.repository
        executions = repository.list_unfinalized_broker_executions(limit=1000)
        if markets is not None:
            executions = [
                row
                for row in executions
                if str(row.get("market") or "").strip().lower() in markets
            ]
        stats = {
            "pending": len(executions),
            "matched": 0,
            "missing": 0,
            "finalized": 0,
            "no_fill": 0,
            "terminal_followups": 0,
            "failed_markets": 0,
        }
        if not executions:
            return stats

        for market in ("domestic", "overseas"):
            market_executions = [
                row for row in executions if str(row.get("market")) == market
            ]
            if not market_executions:
                continue
            try:
                history_rows = await self._load_history(market, market_executions)
            except Exception as exc:  # noqa: BLE001
                stats["failed_markets"] += 1
                _logger.warning(
                    "[FILLS][%s] execution history failed: %s",
                    market,
                    exc,
                )
                self.service._save_event(
                    event_type="execution_reconcile_failed",
                    market=market,
                    detail={"error": str(exc)[:200]},
                )
                continue

            order_rows, canceled_originals = self._index_history(
                market,
                history_rows,
            )
            terminal_followups = self._terminal_cancel_followups(
                market,
                market_executions,
            )
            checked_at = current.astimezone(timezone.utc).isoformat()
            for execution in market_executions:
                normalized = repository.normalize_broker_order_no(
                    execution.get("broker_order_no")
                )
                order_key = (
                    str(execution.get("order_date") or ""),
                    normalized,
                )
                history_row = order_rows.get(order_key) or order_rows.get(
                    ("", normalized)
                )
                if history_row is None:
                    repository.mark_broker_execution_checked(
                        int(execution["id"]),
                        checked_at=checked_at,
                    )
                    stats["missing"] += 1
                    continue
                terminal_followup = terminal_followups.get(
                    int(execution.get("broker_event_id") or 0)
                )
                snapshot = self._execution_snapshot(
                    market,
                    execution,
                    history_row,
                    canceled=(
                        order_key in canceled_originals
                        or ("", normalized) in canceled_originals
                        or terminal_followup is not None
                    ),
                )
                stored_history = dict(history_row)
                if terminal_followup is not None:
                    stored_history["_terminal_followup"] = {
                        "event_id": int(terminal_followup.get("id") or 0),
                        "created_at": str(
                            terminal_followup.get("created_at") or ""
                        ),
                        "status": str(terminal_followup.get("status") or ""),
                        "reason": str(terminal_followup.get("reason") or ""),
                    }
                    stats["terminal_followups"] += 1
                repository.update_broker_order_execution(
                    int(execution["id"]),
                    **snapshot,
                    history=stored_history,
                    checked_at=checked_at,
                )
                stats["matched"] += 1

        refreshed = repository.list_unfinalized_broker_executions(limit=1000)
        if markets is not None:
            refreshed = [
                row
                for row in refreshed
                if str(row.get("market") or "").strip().lower() in markets
            ]
        groups: dict[str, list[dict]] = defaultdict(list)
        for execution in refreshed:
            groups[str(execution["execution_group_id"])].append(execution)

        for group_id, group_rows in groups.items():
            total_filled = sum(int(row.get("filled_qty") or 0) for row in group_rows)
            total_amount = sum(float(row.get("filled_amount") or 0.0) for row in group_rows)
            target_qty = max(int(row.get("group_target_qty") or 0) for row in group_rows)
            all_terminal = all(
                str(row.get("status") or "").upper() in _TERMINAL_STATUSES
                for row in group_rows
            )
            if total_filled < target_qty and not all_terminal:
                continue
            if total_filled <= 0:
                finalized = repository.finalize_broker_execution_group_without_fill(
                    group_id,
                    finalized_at=current.astimezone(timezone.utc).isoformat(),
                )
                if finalized:
                    await self.service._handle_no_fill_execution_group(group_rows)
                    stats["no_fill"] += 1
                continue
            inserted = await self.service._apply_confirmed_execution_group(
                group_rows,
                filled_qty=total_filled,
                filled_amount=total_amount,
                target_qty=target_qty,
                reconciled_at=current,
            )
            if inserted:
                stats["finalized"] += 1

        if force or stats["finalized"] or stats["no_fill"]:
            _logger.info("[FILLS] reconcile stats=%s", stats)
        return stats

    async def _load_history(
        self,
        market: str,
        executions: list[dict],
    ) -> list[dict]:
        start_date = min(str(row["order_date"]) for row in executions).replace("-", "")
        end_date = max(str(row["order_date"]) for row in executions).replace("-", "")
        if market == "domestic":
            history = await self.service.client.get_domestic_order_history(
                start_date=start_date,
                end_date=end_date,
                side_filter="00",
                fill_filter="00",
                query_order="00",
                query_type="00",
                exchange_code="ALL",
                paginate=True,
                max_pages=10,
            )
        else:
            history = await self.service.client.get_overseas_order_history(
                start_date=start_date,
                end_date=end_date,
                side_filter="00",
                fill_filter="00",
                exchange_code="",
                sort_sqn="DS",
                paginate=True,
                max_pages=10,
            )
        return [
            row for row in history.get("orders", []) if isinstance(row, dict)
        ]

    def _index_history(
        self,
        market: str,
        rows: list[dict],
    ) -> tuple[dict[tuple[str, str], dict], set[tuple[str, str]]]:
        repository = self.service.repository
        order_rows: dict[tuple[str, str], dict] = {}
        canceled_originals: set[tuple[str, str]] = set()
        for row in rows:
            order_date = self._history_order_date(row)
            revision_code = str(
                row.get("rvse_cncl_dvsn")
                or row.get("rvse_cncl_dvsn_cd")
                or ""
            ).strip()
            revision_name = str(
                row.get("rvse_cncl_dvsn_name")
                or row.get("rvse_cncl_dvsn_cd_name")
                or ""
            ).strip()
            original = repository.normalize_broker_order_no(
                row.get("orgn_odno") or row.get("ORGN_ODNO")
            )
            if revision_code == "02" or "취소" in revision_name:
                if original and original != "0":
                    canceled_originals.add((order_date, original))
                continue

            normalized = repository.normalize_broker_order_no(
                row.get("odno") or row.get("ODNO")
            )
            if not normalized:
                continue
            key = (order_date, normalized)
            current = order_rows.get(key)
            if current is None:
                order_rows[key] = row
                continue
            current_filled = self._filled_qty(market, current)
            row_filled = self._filled_qty(market, row)
            if row_filled >= current_filled:
                order_rows[key] = row
        return order_rows, canceled_originals

    def _terminal_cancel_followups(
        self,
        market: str,
        executions: list[dict],
    ) -> dict[int, dict]:
        """Associate each terminal cancel with its nearest prior submission."""
        if market not in {"domestic", "overseas"} or not executions:
            return {}
        repository = self.service.repository
        loader = getattr(
            repository,
            "list_broker_order_events_for_reconcile",
            None,
        )
        if not callable(loader):
            return {}
        events = loader(
            market=market,
            after_created_at=min(
                str(row.get("created_at") or "") for row in executions
            ),
            limit=5000,
        )
        latest_submission: dict[tuple[str, str], dict] = {}
        terminal_by_submission_id: dict[int, dict] = {}
        for event in events:
            if int(event.get("is_virtual") or 0):
                continue
            symbol = str(event.get("symbol") or "").strip().upper()
            order_kind = str(event.get("order_kind") or "").strip().lower()
            status = str(event.get("status") or "").strip().upper()
            if order_kind != "cancel" and status == "SUBMITTED":
                order_no = repository.normalize_broker_order_no(
                    event.get("broker_order_no")
                )
                if symbol and order_no:
                    latest_submission[(symbol, order_no)] = event
                continue
            if order_kind != "cancel" or status not in {
                "CANCELED",
                "CANCELLED",
            }:
                continue
            payload = (
                event.get("payload_json")
                if isinstance(event.get("payload_json"), dict)
                else {}
            )
            original_order_no = repository.normalize_broker_order_no(
                payload.get("original_order_no")
            )
            submission = latest_submission.get(
                (symbol, original_order_no)
            )
            if submission is None:
                continue
            submission_id = int(submission.get("id") or 0)
            if submission_id > 0:
                terminal_by_submission_id[submission_id] = event
        return terminal_by_submission_id

    @staticmethod
    def _history_order_date(row: dict) -> str:
        date_text = str(row.get("ord_dt") or "").strip()
        if len(date_text) == 8 and date_text.isdigit():
            return f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}"
        return ""

    @staticmethod
    def _filled_qty(market: str, row: dict) -> int:
        field = "tot_ccld_qty" if market == "domestic" else "ft_ccld_qty"
        return max(0, parse_kis_number(row.get(field)))

    def _execution_snapshot(
        self,
        market: str,
        execution: dict,
        row: dict,
        *,
        canceled: bool,
    ) -> dict:
        requested_qty = max(
            int(execution.get("requested_qty") or 0),
            parse_kis_number(
                row.get("ord_qty")
                if market == "domestic"
                else row.get("ft_ord_qty")
            ),
        )
        filled_qty = self._filled_qty(market, row)
        if market == "domestic":
            filled_amount = _as_float(row.get("tot_ccld_amt"))
            avg_fill_price = _as_float(row.get("avg_prvs"))
            remaining_qty = parse_kis_number(row.get("rmn_qty"))
            canceled_qty = max(
                parse_kis_number(row.get("cncl_cfrm_qty")),
                parse_kis_number(row.get("cnc_cfrm_qty")),
            )
            rejected_qty = parse_kis_number(row.get("rjct_qty"))
            canceled = canceled or str(row.get("cncl_yn") or "").upper() == "Y"
        else:
            filled_amount = _as_float(row.get("ft_ccld_amt3"))
            avg_fill_price = _as_float(row.get("ft_ccld_unpr3"))
            remaining_qty = parse_kis_number(row.get("nccs_qty"))
            canceled_qty = 0
            rejected_qty = 0
            process_name = str(row.get("prcs_stat_name") or "")
            if "거부" in process_name:
                rejected_qty = max(0, requested_qty - filled_qty)

        if filled_amount <= 0 and avg_fill_price > 0 and filled_qty > 0:
            filled_amount = avg_fill_price * filled_qty
        if avg_fill_price <= 0 and filled_amount > 0 and filled_qty > 0:
            avg_fill_price = filled_amount / filled_qty

        if canceled and canceled_qty <= 0:
            canceled_qty = max(
                0,
                requested_qty - filled_qty - rejected_qty,
            )
        if (
            canceled_qty > 0
            and filled_qty + canceled_qty + rejected_qty >= requested_qty
        ):
            canceled = True
        if "rmn_qty" not in row and market == "domestic":
            remaining_qty = max(
                0,
                requested_qty - filled_qty - canceled_qty - rejected_qty,
            )
        if "nccs_qty" not in row and market == "overseas":
            remaining_qty = max(
                0,
                requested_qty - filled_qty - canceled_qty - rejected_qty,
            )

        if rejected_qty >= requested_qty and filled_qty <= 0:
            status = "REJECTED"
        elif requested_qty > 0 and filled_qty >= requested_qty:
            status = "FILLED"
            remaining_qty = 0
        elif canceled:
            status = "PARTIAL_CANCELED" if filled_qty > 0 else "CANCELED"
            remaining_qty = 0
        elif remaining_qty > 0:
            status = "PARTIAL" if filled_qty > 0 else "PENDING"
        elif filled_qty > 0:
            status = "PARTIAL"
        else:
            status = "PENDING"

        return {
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "avg_fill_price": avg_fill_price or None,
            "remaining_qty": remaining_qty,
            "canceled_qty": canceled_qty,
            "rejected_qty": rejected_qty,
            "status": status,
            "fill_recorded_at": self._history_order_timestamp(market, row),
        }

    @staticmethod
    def _history_order_timestamp(market: str, row: dict) -> str | None:
        """Return KIS's order timestamp, the only event time in these rows.

        The history APIs expose aggregate fill quantity/price but no distinct
        fill timestamp. Keep this as the best available effective time and
        retain the full response in history_json for later broker audits.
        """
        if market == "domestic":
            date_text = str(row.get("ord_dt") or "").strip()
            time_text = str(row.get("ord_tmd") or "").strip()
        else:
            date_text = str(
                row.get("dmst_ord_dt") or row.get("ord_dt") or ""
            ).strip()
            time_text = str(
                row.get("thco_ord_tmd") or row.get("ord_tmd") or ""
            ).strip()
        if not date_text or not time_text:
            return None
        try:
            parsed = datetime.strptime(
                f"{date_text}{time_text.zfill(6)[:6]}",
                "%Y%m%d%H%M%S",
            )
        except ValueError:
            return None
        return parsed.replace(tzinfo=KST).astimezone(timezone.utc).isoformat()
