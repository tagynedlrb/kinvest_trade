import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kinvest_trade import gpt_worker
from kinvest_trade.telegram_gpt import (
    GptJobStore, authorized_message, bridge_root, format_job, handle_gpt_update,
)


def message(text="/gpt test", **extra):
    return {"chat": {"id": 1234567, "type": "private"},
            "from": {"id": 1234567, "is_bot": False}, "date": time.time(), "text": text, **extra}


@pytest.mark.parametrize("change", [
    {"from": {"id": 7654321}}, {"from": {"id": 1234567, "is_bot": True}},
    {"chat": {"id": 1234567, "type": "group"}}, {"forward_origin": {"type": "user"}},
    {"via_bot": {"id": 1}}, {"date": 1}, {"date": time.time() + 3600}, {"sender_chat": {"id": 1234567}},
    {"from": None}, {"chat": None},
])
def test_owner_authentication_rejects_spoofed_forwarded_or_stale_messages(change):
    assert not authorized_message(message(**change), "1234567")


def test_private_owner_is_required():
    assert authorized_message(message(), "1234567")
    assert not authorized_message(message(), "-1234567")


def test_durable_queue_dedup_confirmation_expiry_ownership_and_cancel(tmp_path):
    store = GptJobStore(tmp_path / "queue")
    job = store.propose(1, "owner", "analyze", now=1000)
    assert store.propose(1, "owner", "different", now=1001)["id"] == job["id"]
    assert not store.confirm("stranger", job["id"], now=1001)
    assert not store.confirm("owner", job["id"], now=1601)
    assert store.confirm("owner", job["id"], now=1599)
    assert not store.confirm("owner", job["id"], now=1599)
    assert store.claim("stranger") is None
    assert store.claim("owner")["id"] == job["id"]
    assert store.claim("owner") is None
    assert store.cancel("owner", job["id"])
    store.finish(job["id"], status="succeeded", result="done")
    assert store.get("owner", job["id"])["status"] == "cancelled"
    assert store.get("stranger", job["id"]) is None
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_queue_limit_and_restart_do_not_repeat_work(tmp_path):
    store = GptJobStore(tmp_path)
    jobs = [store.propose(i, "owner", "test") for i in range(3)]
    with pytest.raises(ValueError):
        store.propose(4, "owner", "test")
    store.confirm("owner", jobs[0]["id"])
    store.claim("owner")
    store.confirm("owner", jobs[1]["id"])
    assert store.claim("owner") is None
    store.recover()
    assert store.get("owner", jobs[0]["id"])["status"] == "interrupted"
    assert store.claim("owner")["id"] == jobs[1]["id"]
    assert store.claim("owner") is None
    with pytest.raises(ValueError):
        store.propose(10, "owner", "x" * 6001)


class Notifier:
    def __init__(self):
        self.messages = []
        self.fail = False

    async def send(self, text):
        if self.fail:
            raise RuntimeError("network")
        self.messages.append(text)


def controller(tmp_path):
    directory = bridge_root(tmp_path)
    directory.mkdir(parents=True)
    (directory / "settings.json").write_text(json.dumps({"enabled": True}))
    return SimpleNamespace(
        config=SimpleNamespace(notifications=SimpleNamespace(telegram_chat_id="1234567"),
                               storage=SimpleNamespace(runtime_state_path=tmp_path / "state/runtime_state.json")),
        notifier=Notifier(),
    )


def test_telegram_proposal_requires_confirmation_then_supports_status_cancel(tmp_path):
    c = controller(tmp_path)
    async def run():
        assert await handle_gpt_update(c, {"update_id": 1, "message": message("/gpt\n분석해줘")})
        store = GptJobStore(bridge_root(tmp_path))
        assert store.get("1234567")["status"] == "proposed"
        assert store.claim("1234567") is None
        await handle_gpt_update(c, {"update_id": 2, "message": message("/gpt_confirm 1")})
        assert store.get("1234567")["status"] == "queued"
        await handle_gpt_update(c, {"update_id": 3, "message": message("/gpt_status")})
        await handle_gpt_update(c, {"update_id": 4, "message": message("/gpt_cancel 1")})
        assert store.get("1234567")["status"] == "cancelled"
        await handle_gpt_update(c, {"update_id": 5, "message": message("/gpt_result ../../secret")})
        assert not await handle_gpt_update(c, {"message": message("/lab_status")})
    asyncio.run(run())
    assert len(c.notifier.messages) == 5


def test_unauthorized_sender_does_not_create_queue(tmp_path):
    c = controller(tmp_path)
    asyncio.run(handle_gpt_update(c, {"update_id": 1, "message": message(**{"from": {"id": 7}})}))
    assert not (bridge_root(tmp_path) / "jobs.sqlite3").exists()
    assert not c.notifier.messages


def test_controller_routes_gpt_without_raw_inbound_logging(tmp_path):
    from kinvest_trade.telegram_control import TelegramLiquidityLabController
    c = controller(tmp_path)
    c.notifier.is_authorized_chat = lambda chat_id: chat_id == 1234567
    def forbidden(text):
        raise AssertionError("GPT prompt must not be duplicated in raw inbound logs")
    c._log_inbound_command = forbidden
    asyncio.run(TelegramLiquidityLabController._handle_update(c, {"update_id": 1, "message": message()}))
    assert GptJobStore(bridge_root(tmp_path)).get("1234567")["status"] == "proposed"


def test_result_pagination_and_notice_retry(tmp_path):
    store = GptJobStore(tmp_path)
    job = store.propose(1, "owner", "test")
    store.confirm("owner", job["id"])
    store.claim("owner")
    store.finish(job["id"], status="succeeded", result="x" * 5000)
    final = store.get("owner")
    assert "다음:" in format_job(final, include_result=True)
    assert len(format_job(final, include_result=True, page=2)) < 3500
    with pytest.raises(ValueError):
        format_job(final, include_result=True, page=0)
    n = Notifier()
    n.fail = True
    asyncio.run(gpt_worker.deliver_notices(store, "owner", n))
    assert store.get("owner")["notified_at"] is None
    n.fail = False
    asyncio.run(gpt_worker.deliver_notices(store, "owner", n))
    assert store.get("owner")["notified_at"] is not None
    assert not store.pending_notices("owner")


def test_codex_command_is_tool_disabled_and_does_not_use_shell_or_bypass():
    args = gpt_worker.codex_command("/bin/codex", Path("/tmp/input"), Path("/tmp/result"), Path("/tmp/schema"))
    assert args[-1] == "-"
    assert "--ignore-user-config" in args and "--ephemeral" in args
    assert not any("bypass" in value or "danger-full" in value for value in args)
    assert 'approval_policy="never"' in args
    assert 'default_permissions="bridge"' in args
    assert "shell_tool=false" in " ".join(args)
    assert "project_doc_max_bytes=0" in args
    assert "mcp_servers={}" in args
    assert 'network={enabled=false}' in " ".join(args)
    assert 'web_search="disabled"' in args


def test_redaction_removes_credentials_accounts_and_long_tokens():
    secret = "explicit-secret-123456"
    text = f"{secret} 50214258-01 123456789:abcdefghijklmnopqrstuvwxyz_ABC {'A' * 40}"
    redacted = gpt_worker.redact(text, (secret,))
    assert secret not in redacted and "50214258" not in redacted and "A" * 40 not in redacted
    assert "123456789:" not in redacted
    assert gpt_worker.redact("old account 12345678") == "old account [8-DIGIT-ID]"
    assert gpt_worker.redact_payload({"volume": 12345678, "account": "12345678"}) == {
        "volume": 12345678, "account": "[8-DIGIT-ID]",
    }


def test_export_requires_consent_before_accessing_operational_data(tmp_path, monkeypatch):
    store = GptJobStore(bridge_root(tmp_path))
    job = store.propose(1, "owner", "test")
    store.confirm("owner", job["id"]); job = store.claim("owner")
    def forbidden(*args, **kwargs):
        raise AssertionError("data must not be read")
    monkeypatch.setattr(gpt_worker, "build_snapshot", forbidden)
    with pytest.raises(ValueError, match="missing_context_consent"):
        asyncio.run(gpt_worker.execute_job(tmp_path, {"share_project_context": True}, store, job))


def test_snapshot_allowlist_and_redaction(tmp_path):
    from kinvest_trade.repository import SqliteRepository
    (tmp_path / "state").mkdir()
    (tmp_path / "state/runtime_state.json").write_text(json.dumps({
        "status": "running", "linked_account": "50214258-01", "last_error": "sensitive",
        "deployment": {"git_commit": "test", "unexpected_secret": "not_allowed"},
    }))
    secret = "app-secret-for-redaction"
    (tmp_path / "WORKLOG.md").write_text("account=50214258-01 secret=" + secret)
    (tmp_path / "config/market_policies").mkdir(parents=True)
    for market in ("domestic", "overseas"):
        (tmp_path / f"config/market_policies/{market}.json").write_text('{}')
    (tmp_path / "src/kinvest_trade").mkdir(parents=True)
    (tmp_path / "src/kinvest_trade/momentum_policy.py").write_text("# example")
    SqliteRepository(tmp_path / "data/trading.db")
    snapshot = gpt_worker.build_snapshot(tmp_path, (secret,))
    text = json.dumps(snapshot)
    assert secret not in text and "50214258" not in text
    assert "linked_account" not in text and "unexpected_secret" not in text
    assert "last_error" not in snapshot["runtime"]
    assert snapshot["scope"] == gpt_worker.CONTEXT_SCOPE


def test_running_job_cancellation_and_output_bounds(tmp_path, monkeypatch):
    store = GptJobStore(bridge_root(tmp_path))
    job = store.propose(1, "owner", "test")
    store.confirm("owner", job["id"]); job = store.claim("owner")
    store.cancel("owner", job["id"])

    class Input:
        def write(self, value):
            assert b"USER MESSAGE" in value
        async def drain(self):
            pass
        def close(self):
            pass

    class Process:
        returncode = None
        pid = 123
        stdin = Input()
        def __init__(self):
            self.stdout = asyncio.StreamReader(); self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader(); self.stderr.feed_eof()
            self.done = asyncio.Event()
        async def wait(self):
            await self.done.wait()
            return self.returncode

    async def run():
        process = Process()
        async def spawn(*args, **kwargs):
            assert kwargs['start_new_session'] is True
            assert "OPENAI_API_KEY" not in kwargs['env']
            assert "TELEGRAM_BOT_TOKEN" not in kwargs['env']
            return process
        async def stop(value):
            value.returncode = -15; value.done.set()
        monkeypatch.setattr(gpt_worker.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(gpt_worker, "stop_process", stop)
        await gpt_worker.execute_job(tmp_path, {"codex_binary": "/bin/true"}, store, job)
        assert store.get("owner")["status"] == "cancelled"
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 2_100_000); reader.feed_eof()
        assert len(await gpt_worker.bounded_read(reader)) == 2_000_000
    asyncio.run(run())
