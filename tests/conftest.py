from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kinvest_trade.repository import SqliteRepository


def _save_confirmed_cycle(
    repository: SqliteRepository,
    side: str,
    cycle_log: dict[str, object],
) -> bool:
    side_key = str(side).strip().upper()
    logged_at = str(cycle_log["logged_at"])
    market = str(cycle_log["market"])
    symbol = str(cycle_log["symbol"])
    exchange_code = cycle_log.get("exchange_code")
    qty = max(1, int(cycle_log.get("qty_executed") or 1))
    requested_price = float(
        cycle_log.get("price") or cycle_log.get("entry_price") or 1.0
    )
    broker_order_no = f"test-{uuid.uuid4().hex}"
    event_id = repository.save_broker_order_event(
        created_at=logged_at,
        market=market,
        symbol=symbol,
        exchange_code=None if exchange_code is None else str(exchange_code),
        side=side_key,
        order_kind="limit",
        requested_qty=qty,
        requested_price=requested_price,
        strategy_flag=str(cycle_log.get("strategy_flag") or ""),
        entry_by=str(cycle_log.get("entry_by") or ""),
        exit_by=str(cycle_log.get("exit_by") or ""),
        status="SUBMITTED",
        reason=str(cycle_log.get("action_reason") or ""),
        broker_order_no=broker_order_no,
    )
    execution = repository.save_broker_order_execution(
        broker_event_id=event_id,
        created_at=logged_at,
        market=market,
        symbol=symbol,
        exchange_code=None if exchange_code is None else str(exchange_code),
        side=side_key,
        broker_order_no=broker_order_no,
        requested_qty=qty,
        requested_price=requested_price,
        strategy_flag=str(cycle_log.get("strategy_flag") or ""),
        entry_by=str(cycle_log.get("entry_by") or ""),
        exit_by=str(cycle_log.get("exit_by") or ""),
        reason=str(cycle_log.get("action_reason") or ""),
        session_id=str(cycle_log.get("session_id") or ""),
        cycle_no=int(cycle_log.get("cycle_no") or 0),
        is_session_trade=int(cycle_log.get("is_session_trade", 1) or 0),
        entry_price=(
            float(cycle_log["entry_price"])
            if cycle_log.get("entry_price") is not None
            else None
        ),
    )
    assert execution is not None
    repository.update_broker_order_execution(
        int(execution["id"]),
        filled_qty=qty,
        filled_amount=requested_price * qty,
        avg_fill_price=requested_price,
        remaining_qty=0,
        canceled_qty=0,
        rejected_qty=0,
        status="FILLED",
        history={"test_fixture": True},
        checked_at=logged_at,
        fill_recorded_at=logged_at,
    )
    cycle_log.setdefault("action_bias", f"{side_key}_REAL")
    cycle_log.setdefault("qty_executed", qty)
    cycle_log["execution_group_id"] = str(execution["execution_group_id"])
    return repository.save_cycle_log(**cycle_log)


@pytest.fixture
def save_confirmed_sell():
    def _save(repository: SqliteRepository, **cycle_log: object) -> bool:
        return _save_confirmed_cycle(repository, "SELL", cycle_log)

    return _save


@pytest.fixture
def save_confirmed_buy():
    def _save(repository: SqliteRepository, **cycle_log: object) -> bool:
        return _save_confirmed_cycle(repository, "BUY", cycle_log)

    return _save
