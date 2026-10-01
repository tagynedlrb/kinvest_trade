import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kinvest_trade import gpt_operations as ops, gpt_orders
from kinvest_trade.telegram_gpt import GptJobStore, bridge_root, handle_gpt_update
from test_telegram_gpt import controller, message


def test_operations_require_explicit_scoped_consent_and_capability():
    with pytest.raises(ValueError, match="consent"):
        ops.operation_guard({"edit_enabled": True}, "edit")
    with pytest.raises(ValueError, match="disabled"):
        ops.operation_guard({"operations_consent": {"scope": ops.CONSENT_SCOPE}}, "edit")
    ops.operation_guard({"operations_consent": {"scope": ops.CONSENT_SCOPE}, "edit_enabled": True}, "edit")


@pytest.mark.parametrize("path", ["../config/fixed_config.json", "config/fixed_config.json", "/etc/passwd",
    "src/kinvest_trade/client.py", "src/kinvest_trade/gpt_worker.py", "tests/conftest.py",
    "tests/test_telegram_gpt.py", "state/gpt_bridge/settings.json", "systemd/kinvest-gpt-worker.service"])
def test_model_cannot_edit_privileged_control_paths(path):
    assert not ops.allowed_path(path)
    with pytest.raises(ValueError):
        ops.apply_edits({path: "x=1\n"}, [{"path": path, "before": "1", "after": "2"}])


def test_exact_replacements_are_unique_valid_python_and_scoped():
    path = "src/kinvest_trade/indicators.py"
    assert ops.apply_edits({path: "x=1\n"}, [{"path": path, "before": "1", "after": "2"}]) == {path: "x=2\n"}
    for before, after in [("", "x"), ("missing", "2"), ("1", "1"), ("1", "("), ("1", "\x00")]:
        with pytest.raises((ValueError, SyntaxError)):
            ops.apply_edits({path: "x=1\n"}, [{"path": path, "before": before, "after": after}])
    with pytest.raises(ValueError, match="unique"):
        ops.apply_edits({path: "x=1;y=1\n"}, [{"path": path, "before": "1", "after": "2"}])
    with pytest.raises(ValueError):
        ops.apply_edits({path: "x=1\n"}, [{"path": "tests/test_indicators.py", "before": "1", "after": "2"}])


def test_policy_edit_must_remain_valid_policy_json():
    path = "config/market_policies/domestic.json"
    with pytest.raises(ValueError):
        ops.apply_edits({path: '{"parameters": {}}'}, [{"path": path, "before": "{}", "after": "1"}])


@pytest.mark.parametrize("argument", [
    "KR BUY 005930 0 50000", "KR BUY 005930 -1 50000", "KR BUY 005930 11 1000",
    "KR BUY 005930 2 60000", "US BUY AAPL 2 300", "US BUY AAPL 1 NaN",
    "US BUY AAPL 1 1e2", "KR BUY 005930 1 0", "KR BUY 005930 1 50000.5",
    "US BUY AAPL;id 1 10", "US BUY AAPL 1 10 EVIL", "KR BUY 005930 1 1000 NASD",
    "LIVE BUY AAPL 1 100", "US SHORT AAPL 1 100", "US BUY AAPL 1 -100",
])
def test_explicit_order_parser_rejects_invalid_or_unbounded_orders(argument):
    with pytest.raises(ValueError):
        gpt_orders.parse_order(argument)


def test_order_parser_preserves_exact_terms_and_exchange():
    assert gpt_orders.parse_order("KR buy 005930 1 70000")["notional"] == "70000"
    assert gpt_orders.parse_order("US sell IBM 2 150.25 NYSE") == {
        "market": "overseas", "side": "sell", "symbol": "IBM", "qty": 2,
        "price": "150.25", "notional": "300.50", "exchange": "NYSE"}


def test_order_expiry_and_account_switch_fail_closed(monkeypatch):
    monkeypatch.setattr(gpt_orders, "paper_guard", lambda config: "current")
    order = {**gpt_orders.parse_order("US BUY AAPL 1 100"), "account_fingerprint": "current"}
    gpt_orders.guard_order_job(None, {"created_at": time.time()}, order)
    with pytest.raises(ValueError, match="expired"):
        gpt_orders.guard_order_job(None, {"created_at": time.time() - 61}, order)
    with pytest.raises(ValueError, match="account_changed"):
        gpt_orders.guard_order_job(None, {"created_at": time.time()}, {**order, "account_fingerprint": "old"})
    with pytest.raises(ValueError):
        gpt_orders.guard_order_job(None, {"created_at": time.time()}, {**order, "qty": 100})


@pytest.mark.parametrize("changes", [{"env": "prod"}, {"live_trading_enabled": True}, {"dry_run": True}])
def test_real_money_or_dry_run_never_reaches_broker(changes, monkeypatch):
    monkeypatch.setattr(gpt_orders, "account_fingerprint", lambda c: "current")
    credentials = dict(env="vps", live_trading_enabled=False, dry_run=False)
    with pytest.raises(ValueError, match="paper_account"):
        gpt_orders.paper_guard(SimpleNamespace(credentials=SimpleNamespace(**(credentials | changes))))


def test_durable_irreversible_boundary_cancel_and_no_replay(tmp_path):
    store = GptJobStore(tmp_path)
    job = store.propose(1, "owner", "order", kind="order", now=1000)
    assert not store.confirm("owner", job["id"], now=1061)
    assert store.confirm("owner", job["id"], now=1059)
    store.claim("owner")
    store.begin_side_effect(job["id"])
    assert not store.cancel("owner", job["id"])
    with pytest.raises(ValueError):
        store.begin_side_effect(job["id"])
    store.recover()
    assert store.get("owner")["status"] == "interrupted"
    assert store.claim("owner") is None
    second = store.propose(2, "owner", "deploy", kind="deploy")
    store.confirm("owner", second["id"]); store.claim("owner")
    store.cancel("owner", second["id"])
    with pytest.raises(ValueError):
        store.begin_side_effect(second["id"])


def test_deploy_requires_verified_owned_unchanged_artifact(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        ops.load_manifest(tmp_path, None)
    folder = bridge_root(tmp_path) / "jobs/1"
    folder.mkdir(parents=True)
    (folder / "changes.diff").write_text("diff")
    data = {"base": "abc", "files": {"src/kinvest_trade/indicators.py": "x=1"},
            "tested_at": time.time(), "tests": "passed", "diff_sha256": hashlib.sha256(b"diff").hexdigest()}
    (folder / "verified.json").write_text(json.dumps(data))
    job = {"id": 1, "kind": "edit", "status": "succeeded"}
    assert ops.load_manifest(tmp_path, job) == data
    (folder / "changes.diff").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        ops.load_manifest(tmp_path, job)
    (folder / "changes.diff").write_text("diff")
    (folder / "verified.json").write_text(json.dumps({**data, "tested_at": time.time() - 3601}))
    with pytest.raises(ValueError, match="expired"):
        ops.load_manifest(tmp_path, job)


def test_telegram_edit_does_not_touch_operations_without_consent(tmp_path):
    c = controller(tmp_path)
    asyncio.run(handle_gpt_update(c, {"update_id": 1, "message": message("/gpt_edit src/kinvest_trade/indicators.py fix")}))
    assert GptJobStore(bridge_root(tmp_path)).get("1234567") is None
    assert "consent" in c.notifier.messages[-1]


def test_sandbox_mounts_no_home_tokens_git_or_production_database(tmp_path, monkeypatch):
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")
    monkeypatch.setattr(ops.subprocess, "run", run)
    workspace = tmp_path / "source"; workspace.mkdir()
    assert ops.sandbox_test(Path("/project"), workspace) == "1 passed"
    args = commands[0]
    assert "--unshare-net" in args and "--unshare-pid" in args
    assert "--no-new-privs" in args and "--bounding-set=-all" in args
    assert "--clearenv" in args
    assert "--bind" not in args
    assert "/project/.venv" in args
    mounts = [args[i+1] for i, value in enumerate(args) if value in {"--ro-bind", "--bind"}]
    assert mounts == ["/usr", str(workspace), "/project/.venv"]
    assert not any("git_token" in x or "trading.db" in x for x in args)


def test_uncertain_marker_blocks_controller_resume(tmp_path):
    from kinvest_trade.telegram_control import TelegramLiquidityLabController
    c = controller(tmp_path)
    (bridge_root(tmp_path) / "uncertain_order.json").write_text('{}')
    c._gpt_order_uncertain = lambda: TelegramLiquidityLabController._gpt_order_uncertain(c)
    c.mode = "paused"
    asyncio.run(TelegramLiquidityLabController._handle_start_like_command(c, "running", "resumed"))
    assert c.mode == "paused"
    assert "불확실" in c.notifier.messages[-1]


def test_deploy_cannot_push_changed_manifest(tmp_path, monkeypatch):
    settings = {"deploy_enabled": True, "operations_consent": {"scope": ops.CONSENT_SCOPE}}
    store = SimpleNamespace(get=lambda *a: {})
    monkeypatch.setattr(ops, "load_manifest", lambda *a: {"base": "abc"})
    monkeypatch.setattr(ops, "git", lambda *a, **k: pytest.fail("No git writes allowed"))
    with pytest.raises(ValueError, match="input_changed"):
        ops.deploy(tmp_path, settings, store, {"created_at": time.time(), "owner": "owner",
            "payload_json": json.dumps({"source_id": 1, "digest": "not-approved"})})


@pytest.mark.parametrize("post_fails", [False, True])
def test_order_is_one_post_persisted_and_uncertainty_pauses_without_replay(tmp_path, monkeypatch, post_fails):
    from kinvest_trade import client as client_module
    from kinvest_trade.repository import SqliteRepository
    from test_telegram_gpt import Notifier
    (tmp_path / "data").mkdir()
    SqliteRepository(tmp_path / "data/trading.db")
    runtime = tmp_path / "state/runtime_state.json"
    runtime.parent.mkdir(exist_ok=True)
    runtime.write_text(json.dumps({"telegram_control": {"mode": "running"}}))
    c = SimpleNamespace(credentials=object())
    monkeypatch.setattr(gpt_orders, "load_app_config", lambda: c)
    monkeypatch.setattr(gpt_orders, "paper_guard", lambda c: "paper")
    calls = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def place_cash_order(self, **kwargs):
            calls.append(("POST", kwargs))
            if post_fails:
                raise TimeoutError("unknown broker result")
            return {"rt_cd": "0", "output": {"ODNO": "123"}}
    monkeypatch.setattr(client_module, "KisRestClient", Client)
    class Service:
        def _record_broker_order_event(self, **kwargs):
            calls.append(("record", kwargs))
            return {"id": 10}
        async def _reconcile_broker_executions(self, *args, **kwargs):
            calls.append(("reconcile", {}))
    async def preflight(*args):
        return Service(), SimpleNamespace(guard=lambda: None), None
    monkeypatch.setattr(gpt_orders, "preflight", preflight)
    monkeypatch.setattr(gpt_orders, "service_command", lambda cmd: calls.append((cmd, {})))
    store = GptJobStore(bridge_root(tmp_path))
    payload = {**gpt_orders.parse_order("KR BUY 005930 1 50000"), "account_fingerprint": "paper"}
    job = store.propose(1, "owner", "order", kind="order", payload=payload)
    store.confirm("owner", job["id"]); job = store.claim("owner")
    settings = {"order_enabled": True, "operations_consent": {"scope": ops.CONSENT_SCOPE}}
    if post_fails:
        with pytest.raises(TimeoutError):
            asyncio.run(gpt_orders.execute_order(tmp_path, settings, store, job, Notifier()))
        assert (bridge_root(tmp_path) / "uncertain_order.json").exists()
        assert json.loads(runtime.read_text())["telegram_control"]["mode"] == "paused"
        with pytest.raises(ValueError, match="previous_order_uncertain"):
            asyncio.run(gpt_orders.execute_order(tmp_path, settings, store, job, Notifier()))
    else:
        result = asyncio.run(gpt_orders.execute_order(tmp_path, settings, store, job, Notifier()))
        assert "접수는 체결이 아닙니다" in result
        assert not (bridge_root(tmp_path) / "uncertain_order.json").exists()
        recorded = [value for action, value in calls if action == "record"][0]
        assert recorded["strategy_flag"] == "MANUAL_GPT"
        assert recorded["execution_context"]["is_session_trade"] == 0
    assert [action for action, _ in calls].count("POST") == 1
    assert [action for action, _ in calls][0] == "stop"
    assert [action for action, _ in calls][-1] == "start"
    assert not store.cancel("owner", job["id"])


@pytest.mark.parametrize("start_fails", [False, True])
def test_deploy_pushes_exact_approved_commit_before_restart_and_keeps_backup(tmp_path, monkeypatch, start_fails):
    import sqlite3
    base, commit = "a" * 40, "b" * 40
    folder = bridge_root(tmp_path); folder.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    with sqlite3.connect(tmp_path / "data/trading.db") as conn:
        conn.execute("create table evidence(value text)")
        conn.execute("insert into evidence values('preserved')")
    runtime = tmp_path / "state/runtime_state.json"
    runtime.write_text(json.dumps({"deployment": {"git_commit": commit, "git_dirty": False}}))
    store = GptJobStore(folder)
    source = store.propose(1, "owner", "edit", kind="edit")
    manifest = {"base": base, "files": {"src/kinvest_trade/indicators.py": "x=2\n"}, "tests": "passed"}
    monkeypatch.setattr(ops, "load_manifest", lambda *args: manifest)
    job = store.propose(2, "owner", "deploy", kind="deploy", payload={"source_id": source["id"], "digest": ops.digest(manifest)})
    store.confirm("owner", job["id"]); job = store.claim("owner")
    actions = []
    current = [base]
    def git(project, *args, **kwargs):
        actions.append(args[0])
        if args[:2] == ("branch", "--show-current"):
            return "master"
        if args[0] == "remote":
            return "https://github.com/example/repo.git"
        if args[0] == "rev-parse":
            return current[0]
        if args[0] == "merge":
            current[0] = commit
        if args[0] == "revert":
            current[0] = "c" * 40
        if args[0] == "commit-tree":
            return commit
        if args[0] == "push":
            assert store.get("owner", job["id"])["side_effect_started_at"]
            assert args[-1] in {f"{commit}:refs/heads/master", "HEAD:refs/heads/master"}
        return "" if args[0] == "status" else "test"
    monkeypatch.setattr(ops, "git", git)
    monkeypatch.setattr(gpt_orders, "paper_guard", lambda *a: "paper")
    monkeypatch.setattr(ops, "guard_acceptance_deploy", lambda *a: None)
    monkeypatch.setattr(ops, "refresh_acceptance", lambda *a: actions.append("refresh"))
    def service(action):
        actions.append(action)
        if action == "start" and start_fails and actions.count("start") == 1:
            raise RuntimeError("startup failed")
    monkeypatch.setattr(ops, "service_command", service)
    monkeypatch.setattr(ops.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    settings = {"deploy_enabled": True, "operations_consent": {"scope": ops.CONSENT_SCOPE},
                "git_remote": "https://github.com/example/repo.git"}
    if start_fails:
        with pytest.raises(ValueError, match="rollback"):
            ops.deploy(tmp_path, settings, store, job)
        assert "revert" in actions
        assert actions.count("push") == 2
        assert current[0] == "c" * 40
        assert (folder / "jobs" / str(job["id"]) / "rollback.json").exists()
    else:
        result = ops.deploy(tmp_path, settings, store, job)
        assert commit[:12] in result
        assert actions[-1] == "refresh"
    assert actions.index("push") < actions.index("stop") < actions.index("merge") < actions.index("start")
    with sqlite3.connect(folder / "jobs" / str(job["id"]) / "predeploy.db") as conn:
        assert conn.execute("select value from evidence").fetchone()[0] == "preserved"


def test_operational_lock_excludes_existing_paper_diagnostics(tmp_path):
    import fcntl
    from kinvest_trade.gpt_worker import operation_lock
    (tmp_path / "state").mkdir()
    with (tmp_path / "state/paper_execution_check.lock").open("a") as diagnostic:
        fcntl.flock(diagnostic, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="another_manual"):
            with operation_lock(tmp_path):
                pytest.fail("must not enter")
    with operation_lock(tmp_path):
        pass


def test_model_cannot_add_imports_dynamic_execution_or_change_risk_limits():
    path = "src/kinvest_trade/indicators.py"
    for after in ("import os\nx=2\n", "x=eval('2')\n", "x=open('/etc/passwd')\n"):
        with pytest.raises(ValueError):
            ops.apply_edits({path: "x=1\n"}, [{"path": path, "before": "x=1\n", "after": after}])
    path = "config/market_policies/domestic.json"
    text = '{"parameters":{},"risk":{"limit":1}}'
    with pytest.raises(ValueError, match="risk_limits"):
        ops.apply_edits({path: text}, [{"path": path, "before": '"limit":1', "after": '"limit":2'}])
