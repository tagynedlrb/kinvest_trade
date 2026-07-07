import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from kinvest_trade.git_uploader import _extract_cycle_log, upload_log


def test_extract_cycle_log_filters_by_kst_date(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T14:59:59+00:00", "overseas", "OLD"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "KEEP1"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-03T14:59:59+00:00", "overseas", "KEEP2"),
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-03T15:00:00+00:00", "overseas", "NEXT"),
    )
    conn.commit()
    conn.close()

    rows = _extract_cycle_log(db_path, "2026-07-03")

    assert [row["symbol"] for row in rows] == ["KEEP1", "KEEP2"]


def test_upload_log_reports_success(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "NVDA"),
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
                    "html_url": "https://github.com/tagynedlrb/kinvest_trade/blob/master/logs/trades/test.csv"
                }
            },
        )

    async def run_case() -> tuple[bool, str]:
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
    assert result.endswith("test.csv")


def test_upload_log_uses_date_based_filename(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO cycle_log (logged_at, market, symbol) VALUES (?, ?, ?)",
        ("2026-07-02T15:00:00+00:00", "overseas", "NVDA"),
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
            json={"content": {"html_url": "https://github.com/example/repo/blob/master/logs/trades/20260707_session.csv"}},
        )

    async def run_case() -> tuple[bool, str]:
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
    assert any(re.search(r"/logs/trades/\d{8}_session\.csv$", path) for path in seen_paths)
    assert not any(re.search(r"/logs/trades/\d{8}_\d{6}_session\.csv$", path) for path in seen_paths)
