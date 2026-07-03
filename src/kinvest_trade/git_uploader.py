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


def _extract_cycle_log(db_path: str | Path, date_kst: str | None = None) -> list[dict]:
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
            ORDER BY logged_at
            """,
            (start_utc, end_utc),
        )
        rows = [dict(row) for row in cur.fetchall()]
        logger.info("git_upload_cycle_log_rows count=%s date_kst=%s", len(rows), date_kst)
        return rows
    except sqlite3.OperationalError as exc:
        logger.warning("git_upload_cycle_log_query_failed error=%s", exc)
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


async def upload_log(
    client: httpx.AsyncClient,
    db_path: str | Path,
    github_token: str,
    github_repo: str,
    date_kst: str | None = None,
) -> tuple[bool, str]:
    if not github_token:
        return False, "GITHUB_TOKEN 미설정. fixed_config.json 또는 환경변수에 추가하세요."
    rows = _extract_cycle_log(db_path, date_kst)
    if not rows:
        return False, "업로드할 cycle_log 데이터가 없습니다."

    csv_content = _rows_to_csv(rows)
    now_kst = datetime.now(_KST)
    filename = now_kst.strftime("%Y%m%d_%H%M%S_session.csv")
    repo_path = f"logs/trades/{filename}"
    sha = None
    try:
        sha = await _get_file_sha(client, github_repo, repo_path, github_token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("git_upload_sha_lookup_failed error=%s", exc)

    payload: dict[str, str] = {
        "message": (
            f"[auto] 거래 로그 업로드 {now_kst.strftime('%Y-%m-%d %H:%M')} KST "
            f"({len(rows)} rows)"
        ),
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
        logger.warning("git_upload_failed http=%s body=%s", response.status_code, body)
        return False, f"GitHub API 오류 HTTP {response.status_code}: {body[:120]}"

    data = response.json()
    html_url = str((data.get("content") or {}).get("html_url") or "")
    logger.info("git_upload_success url=%s", html_url)
    return True, html_url
