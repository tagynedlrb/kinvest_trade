from __future__ import annotations

import base64
import csv
import io
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_KST = timezone(timedelta(hours=9))


def _kst_day_bounds_utc(date_kst: str | None) -> tuple[str, str]:
    if date_kst is None:
        date_kst = datetime.now(_KST).strftime("%Y-%m-%d")
    start_kst = datetime.strptime(date_kst, "%Y-%m-%d").replace(tzinfo=_KST)
    end_kst = start_kst + timedelta(days=1)
    return start_kst.astimezone(timezone.utc).isoformat(), end_kst.astimezone(timezone.utc).isoformat()


def _extract_table_rows(
    db_path: str | Path,
    *,
    table: str,
    time_column: str,
    date_kst: str | None,
    log_label: str,
) -> list[dict]:
    start_utc, end_utc = _kst_day_bounds_utc(date_kst)
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE {time_column} >= ? AND {time_column} < ?
            ORDER BY {time_column}
            """,
            (start_utc, end_utc),
        )
        rows = [dict(row) for row in cur.fetchall()]
        logger.info("git_upload_%s_rows count=%s date_kst=%s", log_label, len(rows), date_kst)
        return rows
    except sqlite3.OperationalError as exc:
        logger.warning("git_upload_%s_query_failed error=%s", log_label, exc)
        return []
    finally:
        conn.close()


def _extract_trade_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    # Every decision the strategy makes for the day (BUY/SELL/HOLD/WAIT/SKIP,
    # real and virtual), not just the ones that resulted in an actual order.
    return _extract_table_rows(
        db_path,
        table="cycle_log",
        time_column="logged_at",
        date_kst=date_kst,
        log_label="trade_log",
    )


def _extract_event_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    return _extract_table_rows(
        db_path,
        table="event_log",
        time_column="logged_at",
        date_kst=date_kst,
        log_label="event_log",
    )


def _extract_broker_order_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    # Every real/virtual order request KIS actually saw: submitted, rejected,
    # canceled, recorded-as-virtual, with the broker's own response text.
    return _extract_table_rows(
        db_path,
        table="broker_order_events",
        time_column="created_at",
        date_kst=date_kst,
        log_label="broker_order_log",
    )


def _extract_telegram_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    # Every Telegram command received and every notification sent, so a
    # request -> notification -> outcome chain can be reconstructed later.
    return _extract_table_rows(
        db_path,
        table="telegram_message_log",
        time_column="created_at",
        date_kst=date_kst,
        log_label="telegram_log",
    )


def _extract_api_call_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    # Every KIS API request/response summary (no account numbers or
    # credentials -- see save_api_call/_request, which never pass those in).
    return _extract_table_rows(
        db_path,
        table="api_call_log",
        time_column="created_at",
        date_kst=date_kst,
        log_label="api_call_log",
    )


def _rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


async def _get_file_sha(
    client: httpx.AsyncClient,
    repo: str,
    path: str,
    token: str,
) -> str | None:
    response = await client.get(
        f"{_GITHUB_API}/repos/{repo}/contents/{path}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    if response.status_code == 200:
        return str(response.json().get("sha") or "")
    return None


async def _upload_csv(
    client: httpx.AsyncClient,
    *,
    github_token: str,
    github_repo: str,
    repo_path: str,
    rows: list[dict],
    message: str,
) -> tuple[bool, str]:
    csv_content = _rows_to_csv(rows)
    sha = None
    try:
        sha = await _get_file_sha(client, github_repo, repo_path, github_token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("git_upload_sha_lookup_failed path=%s error=%s", repo_path, exc)

    payload: dict[str, str] = {
        "message": message,
        "content": base64.b64encode(csv_content.encode("utf-8-sig")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    response = await client.put(
        f"{_GITHUB_API}/repos/{github_repo}/contents/{repo_path}",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        content=json.dumps(payload),
    )
    if response.status_code not in {200, 201}:
        body = response.text[:200]
        logger.warning(
            "git_upload_failed path=%s http=%s body=%s",
            repo_path,
            response.status_code,
            body,
        )
        return False, f"GitHub API 오류 HTTP {response.status_code}: {body[:120]}"

    data = response.json()
    html_url = str((data.get("content") or {}).get("html_url") or "")
    logger.info("git_upload_success path=%s url=%s", repo_path, html_url)
    return True, html_url


_LOG_SPECS: tuple[tuple[str, str, str, Callable[..., list[dict]]], ...] = (
    ("trades", "logs/trades/{date}_trades.csv", "거래 로그", _extract_trade_log),
    ("events", "logs/events/{date}_events.csv", "이벤트 로그", _extract_event_log),
    ("orders", "logs/orders/{date}_orders.csv", "주문 로그", _extract_broker_order_log),
    ("telegram", "logs/telegram/{date}_telegram.csv", "텔레그램 로그", _extract_telegram_log),
    ("api_calls", "logs/api_calls/{date}_api_calls.csv", "API 호출 로그", _extract_api_call_log),
)


async def upload_log(
    client: httpx.AsyncClient,
    db_path: str | Path,
    github_token: str,
    github_repo: str,
    date_kst: str | None = None,
) -> tuple[bool, dict[str, dict[str, str | int]] | str]:
    if not github_token:
        return False, "GITHUB_TOKEN 미설정. fixed_config.json 또는 환경변수에 추가하세요."
    now_kst = datetime.now(_KST)
    date_str = now_kst.strftime("%Y%m%d")

    extracted: list[tuple[str, str, str, list[dict]]] = []
    for key, path_template, label, extract in _LOG_SPECS:
        rows = extract(db_path, date_kst)
        if rows:
            extracted.append((key, path_template.format(date=date_str), label, rows))

    if not extracted:
        return False, "업로드할 로그 데이터가 없습니다."

    results: dict[str, dict[str, str | int]] = {}
    for key, repo_path, label, rows in extracted:
        ok, url = await _upload_csv(
            client,
            github_token=github_token,
            github_repo=github_repo,
            repo_path=repo_path,
            rows=rows,
            message=f"[auto] {label} {now_kst.strftime('%H:%M')} KST ({len(rows)}건)",
        )
        if not ok:
            return False, url
        results[key] = {"url": url, "path": repo_path, "rows": len(rows)}
    return True, results
