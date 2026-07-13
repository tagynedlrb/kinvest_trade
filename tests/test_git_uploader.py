import asyncio
import re
import sqlite3

import httpx

from kinvest_trade.git_uploader import (
    _extract_api_call_log,
    _extract_broker_order_log,
    _extract_event_log,
    _extract_telegram_log,
    _extract_trade_log,
    upload_log,
)


def _prepare_tables(db_path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action_bias TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            market TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            cycle_no INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE broker_order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE telegram_message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            direction TEXT NOT NULL,
            command TEXT DEFAULT '',
            text TEXT DEFAULT '',
            success INTEGER DEFAULT 1,
            error TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE api_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            method TEXT NOT NULL,
            tr_id TEXT DEFAULT '',
            path TEXT DEFAULT '',
            success INTEGER DEFAULT 1,
            http_status INTEGER,
            msg_cd TEXT DEFAULT '',
            msg1 TEXT DEFAULT '',
            elapsed_ms INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def test_extract_trade_log_filters_by_kst_date_only_no_action_bias_filter(tmp_path) -> None:
    # All decisions (including WAIT/HOLD) are exported now, not just the
    # subset that resulted in a real order -- only the KST day boundary
    # still filters rows.
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "KEEP1", "BUY_REAL"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "overseas", "KEEP_WAIT", "WAIT"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-03T14:59:59+00:00", "overseas", "KEEP2", "SKIP"),
    )
    conn.commit()
    conn.close()

    rows = _extract_trade_log(db_path, "2026-07-03")

    assert [row["symbol"] for row in rows] == ["KEEP1", "KEEP_WAIT", "KEEP2"]


def test_extract_broker_order_log_filters_by_kst_date(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO broker_order_events (created_at, market, symbol, side, status) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-02T14:59:59+00:00", "domestic", "OLD", "BUY", "REJECTED"),
    )
    conn.execute(
        "INSERT INTO broker_order_events (created_at, market, symbol, side, status) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-03T00:00:00+00:00", "domestic", "KEEP1", "BUY", "SUBMITTED"),
    )
    conn.commit()
    conn.close()

    rows = _extract_broker_order_log(db_path, "2026-07-03")

    assert [row["symbol"] for row in rows] == ["KEEP1"]


def test_extract_telegram_log_filters_by_kst_date(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO telegram_message_log (created_at, direction, text) VALUES (?, ?, ?)",
        ("2026-07-02T14:59:59+00:00", "sent", "old"),
    )
    conn.execute(
        "INSERT INTO telegram_message_log (created_at, direction, text) VALUES (?, ?, ?)",
        ("2026-07-03T00:00:00+00:00", "received", "/lab_status"),
    )
    conn.commit()
    conn.close()

    rows = _extract_telegram_log(db_path, "2026-07-03")

    assert [row["text"] for row in rows] == ["/lab_status"]


def test_extract_api_call_log_filters_by_kst_date(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_call_log (created_at, method, tr_id) VALUES (?, ?, ?)",
        ("2026-07-02T14:59:59+00:00", "GET", "OLD_TR"),
    )
    conn.execute(
        "INSERT INTO api_call_log (created_at, method, tr_id) VALUES (?, ?, ?)",
        ("2026-07-03T00:00:00+00:00", "POST", "VTTC0012U"),
    )
    conn.commit()
    conn.close()

    rows = _extract_api_call_log(db_path, "2026-07-03")

    assert [row["tr_id"] for row in rows] == ["VTTC0012U"]


def test_extract_event_log_filters_by_kst_date(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T14:59:59+00:00", "session_start", "OLD"),
    )
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "trade_skip", "KEEP1"),
    )
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-03T14:59:59+00:00", "cb_fired", "KEEP2"),
    )
    conn.commit()
    conn.close()

    rows = _extract_event_log(db_path, "2026-07-03")

    assert [row["symbol"] for row in rows] == ["KEEP1", "KEEP2"]


def test_upload_log_reports_success_for_trade_and_event_files(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "NVDA", "BUY_REAL"),
    )
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:01:00+00:00", "session_start", "NVDA"),
    )
    conn.commit()
    conn.close()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(
            201,
            json={
                "content": {
                    "html_url": f"https://github.com/tagynedlrb/kinvest_trade/blob/master{request.url.path.split('/contents', 1)[-1]}"
                }
            },
        )

    async def run_case():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await upload_log(
                client=client,
                db_path=db_path,
                github_token="test-token",
                github_repo="tagynedlrb/kinvest_trade",
                date_kst="2026-07-03",
            )

    success, result = asyncio.run(run_case())
    assert success is True
    assert isinstance(result, dict)
    assert result["trades"]["url"].endswith("_trades.csv")
    assert result["events"]["url"].endswith("_events.csv")


def test_upload_log_uses_date_based_filenames(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "NVDA", "SELL_REAL"),
    )
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:05:00+00:00", "cb_fired", "NVDA"),
    )
    conn.commit()
    conn.close()

    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(
            201,
            json={"content": {"html_url": "https://github.com/example/repo/blob/master/test.csv"}},
        )

    async def run_case():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await upload_log(
                client=client,
                db_path=db_path,
                github_token="test-token",
                github_repo="tagynedlrb/kinvest_trade",
                date_kst="2026-07-03",
            )

    success, _ = asyncio.run(run_case())

    assert success is True
    assert any(re.search(r"/logs/trades/\d{8}_trades\.csv$", path) for path in seen_paths)
    assert any(re.search(r"/logs/events/\d{8}_events\.csv$", path) for path in seen_paths)


def test_upload_log_includes_orders_telegram_and_api_calls_when_present(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "overseas", "NVDA", "WAIT"),
    )
    conn.execute(
        "INSERT INTO event_log (logged_at, event_type, symbol) VALUES (?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "trade_skip", "NVDA"),
    )
    conn.execute(
        "INSERT INTO broker_order_events (created_at, market, symbol, side, status) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "overseas", "NVDA", "BUY", "SUBMITTED"),
    )
    conn.execute(
        "INSERT INTO telegram_message_log (created_at, direction, text) VALUES (?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "received", "/lab_status"),
    )
    conn.execute(
        "INSERT INTO api_call_log (created_at, method, tr_id) VALUES (?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "POST", "VTTC0012U"),
    )
    conn.commit()
    conn.close()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(
            201,
            json={"content": {"html_url": f"https://github.com/example/repo/blob/master{request.url.path}"}},
        )

    async def run_case():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await upload_log(
                client=client,
                db_path=db_path,
                github_token="test-token",
                github_repo="tagynedlrb/kinvest_trade",
                date_kst="2026-07-03",
            )

    success, result = asyncio.run(run_case())

    assert success is True
    assert set(result.keys()) == {"trades", "events", "orders", "telegram", "api_calls"}
    assert result["orders"]["path"].endswith("_orders.csv")
    assert result["telegram"]["path"].endswith("_telegram.csv")
    assert result["api_calls"]["path"].endswith("_api_calls.csv")
    for entry in result.values():
        assert entry["rows"] == 1
