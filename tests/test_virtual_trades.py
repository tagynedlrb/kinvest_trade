from kinvest_trade.lab_positions import VirtualTradeManager
from kinvest_trade.repository import SqliteRepository


def _build_manager(tmp_path) -> VirtualTradeManager:
    repository = SqliteRepository(tmp_path / "virtual_trades.db")
    return VirtualTradeManager(repository)


def test_record_buy_creates_new_position(tmp_path) -> None:
    manager = _build_manager(tmp_path)

    position = manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=2,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    assert position is not None
    assert position.qty == 2
    assert position.avg_price == 20.0


def test_record_buy_averages_existing_position(tmp_path) -> None:
    manager = _build_manager(tmp_path)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=2,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    position = manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=26.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:56:00 KST",
    )

    assert position is not None
    assert position.qty == 3
    assert round(position.avg_price, 4) == round((20.0 * 2 + 26.0) / 3, 4)


def test_record_sell_calculates_realized_pnl(tmp_path) -> None:
    manager = _build_manager(tmp_path)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=2,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    realized_pnl, realized_pnl_pct = manager.record_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=23.0,
        currency="USD",
        session="premarket",
        reason="take_profit",
        created_at="2026-06-30 20:10:00 KST",
    )

    assert realized_pnl == 3.0
    assert round(realized_pnl_pct, 4) == 0.15


def test_record_sell_partial_keeps_remaining_position(tmp_path) -> None:
    manager = _build_manager(tmp_path)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=3,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    manager.record_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=22.0,
        currency="USD",
        session="premarket",
        reason="take_profit",
        created_at="2026-06-30 20:10:00 KST",
    )

    position = manager.get_position("overseas", "SOXL")
    assert position is not None
    assert position.qty == 2
    assert position.avg_price == 20.0


def test_record_sell_full_deletes_position(tmp_path) -> None:
    manager = _build_manager(tmp_path)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )

    manager.record_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=22.0,
        currency="USD",
        session="premarket",
        reason="take_profit",
        created_at="2026-06-30 20:10:00 KST",
    )

    assert manager.get_position("overseas", "SOXL") is None


def test_performance_summary_aggregates_by_market_currency(tmp_path) -> None:
    manager = _build_manager(tmp_path)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )
    manager.record_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=22.0,
        currency="USD",
        session="premarket",
        reason="take_profit",
        created_at="2026-06-30 20:10:00 KST",
    )
    manager.record_buy(
        market="domestic",
        symbol="005930",
        exchange_code=None,
        qty=1,
        fill_price=80000.0,
        currency="KRW",
        session="krx_regular",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 10:00:00 KST",
    )
    manager.record_sell(
        market="domestic",
        symbol="005930",
        exchange_code=None,
        qty=1,
        fill_price=79000.0,
        currency="KRW",
        session="krx_regular",
        reason="stop_loss",
        created_at="2026-06-30 10:30:00 KST",
    )

    summary = manager.performance_summary()

    assert summary["overseas_USD"]["trade_count"] == 1
    assert summary["overseas_USD"]["win_count"] == 1
    assert summary["overseas_USD"]["total_pnl"] == 2.0
    assert summary["domestic_KRW"]["trade_count"] == 1
    assert summary["domestic_KRW"]["win_count"] == 0
    assert summary["domestic_KRW"]["total_pnl"] == -1000.0
