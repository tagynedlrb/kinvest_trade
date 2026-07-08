from __future__ import annotations

import base64
import csv
import io
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_KST = timezone(timedelta(hours=9))


def _extract_trade_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    if date_kst is None:
        date_kst = datetime.now(_KST).strftime("%Y-%m-%d")

    start_kst = datetime.strptime(date_kst, "%Y-%m-%d").replace(tzinfo=_KST)
    end_kst = start_kst + timedelta(days=1)
    start_utc = start_kst.astimezone(timezone.utc).isoformat()
    end_utc = end_kst.astimezone(timezone.utc).isoformat()

    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT *
            FROM cycle_log
            WHERE logged_at >= ? AND logged_at < ?
              AND action_bias IN ('BUY_REAL', 'SELL_REAL', 'SKIP')
            ORDER BY logged_at
            """,
            (start_utc, end_utc),
        )
        rows = [dict(row) for row in cur.fetchall()]
        logger.info("git_upload_trade_log_rows count=%s date_kst=%s", len(rows), date_kst)
        return rows
    except sqlite3.OperationalError as exc:
        logger.warning("git_upload_trade_log_query_failed error=%s", exc)
        return []
    finally:
        conn.close()


def _extract_event_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
    if date_kst is None:
        date_kst = datetime.now(_KST).strftime("%Y-%m-%d")

    start_kst = datetime.strptime(date_kst, "%Y-%m-%d").replace(tzinfo=_KST)
    end_kst = start_kst + timedelta(days=1)
    start_utc = start_kst.astimezone(timezone.utc).isoformat()
    end_utc = end_kst.astimezone(timezone.utc).isoformat()

    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT *
            FROM event_log
            WHERE logged_at >= ? AND logged_at < ?
            ORDER BY logged_at
            """,
            (start_utc, end_utc),
        )
        rows = [dict(row) for row in cur.fetchall()]
        logger.info("git_upload_event_log_rows count=%s date_kst=%s", len(rows), date_kst)
        return rows
    except sqlite3.OperationalError as exc:
        logger.warning("git_upload_event_log_query_failed error=%s", exc)
        return []
    finally:
        conn.close()


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
    trade_rows = _extract_trade_log(db_path, date_kst)
    event_rows = _extract_event_log(db_path, date_kst)
    if not trade_rows and not event_rows:
        return False, "업로드할 거래/이벤트 로그 데이터가 없습니다."

    results: dict[str, dict[str, str | int]] = {}
    if trade_rows:
        ok, url = await _upload_csv(
            client,
            github_token=github_token,
            github_repo=github_repo,
            repo_path=f"logs/trades/{date_str}_trades.csv",
            rows=trade_rows,
            message=f"[auto] 거래 로그 {now_kst.strftime('%H:%M')} KST ({len(trade_rows)}건)",
        )
        if not ok:
            return False, url
        results["trades"] = {
            "url": url,
            "path": f"logs/trades/{date_str}_trades.csv",
            "rows": len(trade_rows),
        }
    if event_rows:
        ok, url = await _upload_csv(
            client,
            github_token=github_token,
            github_repo=github_repo,
            repo_path=f"logs/events/{date_str}_events.csv",
            rows=event_rows,
            message=f"[auto] 이벤트 로그 {now_kst.strftime('%H:%M')} KST ({len(event_rows)}건)",
        )
        if not ok:
            return False, url
        results["events"] = {
            "url": url,
            "path": f"logs/events/{date_str}_events.csv",
            "rows": len(event_rows),
        }
    return True, results
