"""Authenticated, durable Telegram requests; never execute Telegram text as shell."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


COMMANDS = {"/gpt", "/gpt_confirm", "/gpt_status", "/gpt_result", "/gpt_cancel", "/gpt_help"}
HELP = (
    "[GPT]\n"
    "/gpt <지시사항> - 분석/수정안 작업 접수\n"
    "/gpt_confirm <번호> - 비용 발생 작업 실행 승인 (10분 이내)\n"
    "/gpt_status [번호] - 상태 조회\n"
    "/gpt_result <번호> [페이지] - 결과 조회\n"
    "/gpt_cancel <번호> - 대기/실행 작업 취소\n"
    "기존 개인 대화의 본인만 사용 가능합니다.\n"
    "현재 분석·수정안 작성 전용입니다. 운영 파일 수정, 주문, 배포는 실행하지 않습니다.\n"
    "이 채팅과 별도의 Codex 작업이며 기존 로그인 계정 사용량이 소모됩니다."
)


def bridge_root(project: Path) -> Path:
    return project / "state" / "gpt_bridge"


def load_bridge_settings(project: Path) -> dict:
    path = bridge_root(project) / "settings.json"
    if not path.exists():
        return {"enabled": False}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid_gpt_settings")
    return data


class GptJobStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "jobs.sqlite3"
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                owner TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed', created_at REAL NOT NULL,
                confirmed_at REAL, started_at REAL, finished_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                base_commit TEXT NOT NULL DEFAULT '', usage_json TEXT NOT NULL DEFAULT '{}',
                notified_at REAL, notice_attempts INTEGER NOT NULL DEFAULT 0
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def propose(self, update_id: int, owner: str, prompt: str, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not prompt.strip() or len(prompt) > 6000:
            raise ValueError("지시사항은 1~6000자로 입력해주세요.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM jobs WHERE update_id=?", (update_id,)).fetchone()
            if existing:
                if existing["owner"] != owner:
                    raise ValueError("request_owner_mismatch")
                return dict(existing)
            conn.execute("UPDATE jobs SET status='expired',finished_at=? WHERE status='proposed' AND created_at<?", (now, now - 600))
            recent = conn.execute("SELECT count(*) FROM jobs WHERE owner=? AND created_at>?", (owner, now - 3600)).fetchone()[0]
            pending = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('proposed','queued','running')").fetchone()[0]
            if recent >= 6 or pending >= 3:
                raise ValueError("작업 한도에 도달했습니다. 시간당 6개, 동시 대기 포함 3개입니다.")
            cursor = conn.execute("INSERT INTO jobs(update_id,owner,prompt,created_at) VALUES(?,?,?,?)", (update_id, owner, prompt, now))
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone())

    def get(self, owner: str, job_id: int | None = None) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE owner=? AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 1", (owner, job_id, job_id)).fetchone()
        return dict(row) if row else None

    def confirm(self, owner: str, job_id: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.connect() as conn:
            cursor = conn.execute("UPDATE jobs SET status='queued',confirmed_at=? WHERE id=? AND owner=? AND status='proposed' AND created_at>=?", (now, job_id, owner, now - 600))
            return cursor.rowcount == 1

    def cancel(self, owner: str, job_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("""UPDATE jobs SET cancel_requested=1,
                status=CASE WHEN status='running' THEN status ELSE 'cancelled' END,
                finished_at=CASE WHEN status='running' THEN finished_at ELSE ? END
                WHERE id=? AND owner=? AND status IN ('proposed','queued','running')""", (time.time(), job_id, owner))
            return cursor.rowcount == 1

    def recover(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status='interrupted',error='worker_restarted_no_automatic_replay',finished_at=? WHERE status='running'", (time.time(),))

    def claim(self, owner: str) -> dict | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = conn.execute("SELECT * FROM jobs WHERE status='queued' AND owner=? ORDER BY id LIMIT 1", (owner,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def finish(self, job_id: int, *, status: str, result: str = "", error: str = "", base_commit: str = "", usage: dict | None = None) -> None:
        if status not in {"succeeded", "failed", "cancelled", "interrupted", "timed_out"}:
            raise ValueError("invalid_terminal_status")
        with self.connect() as conn:
            conn.execute("""UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                finished_at=?,result=?,error=?,base_commit=?,usage_json=? WHERE id=? AND status='running'""",
                (status, time.time(), result[:20000], error[:200], base_commit, json.dumps(usage or {}), job_id))

    def pending_notices(self, owner: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE owner=? AND status IN ('succeeded','failed','cancelled','interrupted','timed_out') AND notified_at IS NULL AND notice_attempts<5 ORDER BY id LIMIT 5", (owner,)).fetchall()
        return [dict(row) for row in rows]

    def mark_notice(self, job_id: int, success: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET notice_attempts=notice_attempts+1,notified_at=? WHERE id=?", (time.time() if success else None, job_id))


def authorized_message(message: dict, owner: str, *, now: float | None = None) -> bool:
    if not owner.isdigit():
        return False
    chat, sender = message.get("chat", {}), message.get("from", {})
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return False
    if chat.get("type") != "private" or str(chat.get("id")) != owner or str(sender.get("id")) != owner:
        return False
    if sender.get("is_bot") or any(message.get(key) for key in ("forward_origin", "forward_from", "forward_date", "via_bot", "sender_chat")):
        return False
    date = message.get("date")
    return isinstance(date, (int, float)) and -30 <= (time.time() if now is None else now) - date <= 300


def format_job(job: dict, *, include_result: bool = False, page: int = 1) -> str:
    text = f"[GPT #{job['id']}] {job['status']}\n분석/수정안 전용 · 운영 변경 없음"
    if job.get("error"):
        text += "\n오류=" + job["error"]
    if include_result and job.get("result"):
        pages = max(1, (len(job["result"]) + 1799) // 1800)
        if page < 1 or page > pages:
            raise ValueError(f"결과 페이지는 1~{pages}입니다.")
        text += f"\n페이지 {page}/{pages}\n\n" + job["result"][(page - 1) * 1800:page * 1800]
        if page < pages:
            text += f"\n\n다음: /gpt_result {job['id']} {page + 1}"
    elif job["status"] == "succeeded":
        text += f"\n/gpt_result {job['id']}"
    return text[:3500]


async def handle_gpt_update(controller, update: dict) -> bool:
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    token = parts[0] if parts else ""
    argument = parts[1] if len(parts) > 1 else ""
    if token not in COMMANDS:
        return False
    owner = str(controller.config.notifications.telegram_chat_id).strip()
    if not authorized_message(message, owner):
        return True
    project = controller.config.storage.runtime_state_path.parent.parent
    if token == "/gpt_help":
        await controller.notifier.send(HELP)
        return True
    settings = load_bridge_settings(project)
    if settings.get("enabled") is not True:
        await controller.notifier.send("[GPT] 연동이 비활성 상태입니다.")
        return True
    store = GptJobStore(bridge_root(project))
    try:
        if token == "/gpt":
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                return True
            job = store.propose(update_id, owner, argument.strip())
            scope = ("승인된 거래·시장·정책·작업 이력 요약과 지시문" if settings.get("share_project_context") is True
                     else "직접 입력한 지시문만")
            answer = (f"[GPT #{job['id']}] {job['status']}\n분석/수정안 전용, 운영 코드·주문·배포 변경 없음.\n"
                      f"{scope}을 OpenAI Codex로 전달합니다. 기존 로그인 사용량이 소모됩니다.\n"
                      f"실행 승인: /gpt_confirm {job['id']}\n취소: /gpt_cancel {job['id']}")
        else:
            values = argument.split()
            if len(values) > (2 if token == "/gpt_result" else 1):
                raise ValueError("명령 인자를 확인해주세요.")
            page = 1
            if len(values) == 2:
                if not values[1].isascii() or not values[1].isdigit() or len(values[1]) > 3:
                    raise ValueError("결과 페이지를 확인해주세요.")
                page = int(values[1])
            value = values[0] if values else ""
            if value and (not value.isascii() or not value.isdigit() or len(value) > 12):
                raise ValueError("작업 번호를 확인해주세요.")
            job_id = int(value) if value else None
            if token != "/gpt_status" and job_id is None:
                raise ValueError("작업 번호가 필요합니다.")
            if token == "/gpt_confirm":
                if not store.confirm(owner, job_id):
                    raise ValueError("승인 불가: 만료됐거나 이미 처리된 작업입니다.")
            elif token == "/gpt_cancel":
                if not store.cancel(owner, job_id):
                    raise ValueError("취소할 대기/실행 작업이 없습니다.")
            job = store.get(owner, job_id)
            answer = format_job(job, include_result=token == "/gpt_result", page=page) if job else "[GPT] 작업이 없습니다."
            if token == "/gpt_status":
                try:
                    heartbeat = json.loads((bridge_root(project) / "heartbeat.json").read_text())
                    healthy = time.time() - float(heartbeat["updated_at"]) < 40
                except (OSError, ValueError, KeyError, TypeError):
                    healthy = False
                answer += "\n워커=" + ("응답 중" if healthy else "응답 확인 필요")
        await controller.notifier.send(answer)
    except ValueError as exc:
        await controller.notifier.send("[GPT] " + str(exc))
    return True
