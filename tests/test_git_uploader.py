import asyncio
import re
import sqlite3

import httpx

from kinvest_trade.git_uploader import _extract_event_log, _extract_trade_log, upload_log


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
    conn.commit()
    conn.close()


def test_extract_trade_log_filters_by_kst_date_and_action_bias(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "KEEP1", "BUY_REAL"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-03T01:00:00+00:00", "overseas", "DROP", "WAIT"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol, action_bias) VALUES (?, ?, ?, ?)",
        ("2026-07-03T14:59:59+00:00", "overseas", "KEEP2", "SKIP"),
    )
    conn.commit()
    conn.close()

    rows = _extract_trade_log(db_path, "2026-07-03")

    assert [row["symbol"] for row in rows] == ["KEEP1", "KEEP2"]


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
