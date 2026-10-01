"""Owner-approved operations. Model output never becomes a shell command."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from .telegram_gpt import bridge_root


SERVICE = "kinvest-telegram-control.service"
CONSENT_SCOPE = "reviewed-code-deploy-paper-orders-v1"
EDITABLE = frozenset({
    "config/market_policies/domestic.json", "config/market_policies/overseas.json",
    *(f"src/kinvest_trade/{name}.py" for name in (
        "momentum_policy", "indicators", "technical_signals", "inverse_policy",
        "adaptive_params", "sector_context", "market_regime", "market_review",
        "trade_analysis", "lab_watch", "tv_scanner",
    )),
    *(f"src/kinvest_trade/strategy/{name}.py" for name in (
        "rsi_macd", "momentum", "vwap_pullback", "volume_breakout", "manager",
    )),
})
EDIT_SCHEMA = {
    "type": "object", "properties": {
        "answer": {"type": "string"}, "proposal": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "edits": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "before": {"type": "string"},
            "after": {"type": "string"}},
            "required": ["path", "before", "after"], "additionalProperties": False}},
    }, "required": ["answer", "proposal", "limitations", "edits"], "additionalProperties": False,
}


def operation_guard(settings: dict, kind: str) -> None:
    if (settings.get("operations_consent") or {}).get("scope") != CONSENT_SCOPE:
        raise ValueError("operations_consent_required")
    if settings.get(f"{kind}_enabled") is not True:
        raise ValueError(f"{kind}_disabled")


def git(project: Path, *args: str, input: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", *args], cwd=project,
                            input=input, text=True, capture_output=True, timeout=120,
                            env=env, check=False)
    if result.returncode:
        raise ValueError("git_" + args[0].replace("-", "_") + "_failed")
    return result.stdout.strip() if args[0] != "show" else result.stdout


def clean_base(project: Path) -> str:
    if git(project, "status", "--porcelain"):
        raise ValueError("working_tree_not_clean")
    return git(project, "rev-parse", "HEAD")


def allowed_path(path: str) -> bool:
    return path in EDITABLE or bool(re.fullmatch(r"tests/test_[a-z0-9_]+\.py", path)) and not any(
        word in path for word in ("gpt", "client", "config", "session", "risk", "execution", "repository", "telegram")
    )


def source_bundle(project: Path, base: str, paths: list[str]) -> dict:
    if not 1 <= len(paths) <= 5 or len(set(paths)) != len(paths):
        raise ValueError("select_one_to_five_files")
    result = {}
    for path in paths:
        if not allowed_path(path) or (project / path).is_symlink():
            raise ValueError("path_not_editable")
        mode = git(project, "ls-tree", base, "--", path).split(" ")[0]
        if mode != "100644":
            raise ValueError("tracked_regular_file_required")
        result[path] = git(project, "show", f"{base}:{path}")
    if sum(len(text.encode()) for text in result.values()) > 180_000:
        raise ValueError("source_bundle_too_large")
    return result


def propose_operation(project, settings, store, owner, command, argument, config):
    if command == "/gpt_edit":
        operation_guard(settings, "edit")
        values = argument.split(maxsplit=1)
        if len(values) != 2:
            raise ValueError("/gpt_edit <상대파일[,파일]> <수정 지시>")
        paths = values[0].split(",")
        base = clean_base(project)
        source_bundle(project, base, paths)
        return "edit", {"base": base, "paths": paths}, (
            "수정안 작성·격리 테스트만 실행합니다. 배포는 별도 승인입니다.\n파일=" + ", ".join(paths))
    if command == "/gpt_deploy":
        operation_guard(settings, "deploy")
        if not re.fullmatch(r"[0-9]{1,12}", argument.strip()):
            raise ValueError("수정작업 번호가 필요합니다.")
        source = store.get(owner, int(argument))
        manifest = load_manifest(project, source)
        if clean_base(project) != manifest["base"]:
            raise ValueError("base_changed_regenerate_edit")
        return "deploy", {"source_id": source["id"], "digest": digest(manifest)}, (
            f"수정작업 #{source['id']} 배포: " + ", ".join(manifest["files"]) +
            f"\n기준={manifest['base'][:12]} / 변경={digest(manifest)[:16]}\n"
            f"격리 전체 테스트 통과. /gpt_diff {source['id']}로 변경분을 먼저 확인하세요.\n"
            "Git push 후 운영 서비스 재시작. 테스트 통과는 수익성 보장이 아닙니다.")
    from .gpt_orders import parse_order, paper_guard
    operation_guard(settings, "order")
    identity = paper_guard(config)
    order = parse_order(argument)
    order.update(account_fingerprint=identity)
    return "order", order, (
        f"모의계좌 {order['market']} {order['exchange']} {order['side'].upper()} {order['symbol']}\n"
        f"수량={order['qty']} 지정가={order['price']} / 최대 주문대금={order['notional']}\n"
        "60초 안에 승인·실행. 수동 주문으로 전략 신호는 우회하며, 전략 수익성 증거가 아닙니다.\n"
        "보유량·주문가능금액·호가·중복 주문 검사. 접수 후 기존 체결 추적에 연결됩니다.")


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def apply_edits(sources: dict[str, str], edits: list[dict]) -> dict[str, str]:
    if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
        raise ValueError("one_to_twenty_edits_required")
    files = dict(sources)
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "before", "after"}:
            raise ValueError("invalid_edit")
        path, before, after = (edit[key] for key in ("path", "before", "after"))
        if not all(isinstance(value, str) for value in (path, before, after)):
            raise ValueError("invalid_edit_types")
        if path not in files or not allowed_path(path) or not before or before == after:
            raise ValueError("invalid_edit_target")
        if files[path].count(before) != 1 or len(after) > 50000 or "\x00" in after:
            raise ValueError("edit_not_unique_or_too_large")
        files[path] = files[path].replace(before, after, 1)
    changed = {path: text for path, text in files.items() if text != sources[path]}
    if not changed:
        raise ValueError("empty_change")
    for path, text in changed.items():
        if path.endswith(".py"):
            tree = ast.parse(text)
            old = ast.parse(sources[path])
            imports = lambda node: {ast.dump(item) for item in ast.walk(node) if isinstance(item, (ast.Import, ast.ImportFrom))}
            if imports(tree) - imports(old):
                raise ValueError("new_imports_require_manual_review")
            prohibited = {"exec", "eval", "compile", "__import__", "open", "getattr", "setattr", "globals", "locals"}
            calls = lambda node: {ast.dump(item) for item in ast.walk(node) if isinstance(item, ast.Call) and
                                 isinstance(item.func, ast.Name) and item.func.id in prohibited}
            if calls(tree) - calls(old):
                raise ValueError("dynamic_code_or_file_access_requires_manual_review")
        else:
            value = json.loads(text)
            if not isinstance(value, dict) or not isinstance(value.get("parameters"), dict):
                raise ValueError("invalid_policy_document")
            if value.get("risk") != json.loads(sources[path]).get("risk"):
                raise ValueError("risk_limits_require_manual_review")
    return changed


def test_snapshot(project: Path, base: str, destination: Path, changes: dict[str, str]) -> None:
    paths = set(git(project, "ls-tree", "-r", "--name-only", base).splitlines())
    for path in sorted(paths | set(changes)):
        if not (path.startswith(("src/", "tests/", "scripts/")) and path.endswith(".py") or
                path.startswith("config/") and path.endswith(".json") or path == "pyproject.toml"):
            continue
        if path in paths and git(project, "ls-tree", base, "--", path).split(" ")[0] not in {"100644", "100755"}:
            raise ValueError("snapshot_special_file")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = changes[path] if path in changes else git(project, "show", f"{base}:{path}")
        if path == "config/fixed_config.json":
            value = json.loads(content)
            # Never mount runtime secrets, even if accidentally committed to this config.
            for key in ("credentials", "kis", "notifications", "github_token"):
                value.pop(key, None)
            content = json.dumps(value)
        target.write_text(content)


def sandbox_test(project: Path, workspace: Path, log_path: Path | None = None) -> str:
    # bwrap performs chdir after dropping its mount capabilities, before setpriv.
    workspace.chmod(0o755)
    args = ["sudo", "-n", "/usr/bin/bwrap", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
            "--proc", "/proc", "--dev", "/dev", "--size", "536870912", "--tmpfs", "/tmp", "--chmod", "1777", "/tmp",
            "--size", "67108864", "--tmpfs", "/dev/shm", "--chmod", "1777", "/dev/shm",
            "--dir", "/home", "--dir", "/home/ubuntu", "--symlink", "/workspace", "/home/ubuntu/kinvest_trade",
            "--ro-bind", str(workspace), "/workspace", "--ro-bind", str(project / ".venv"), "/venv",
            "--clearenv", "--setenv", "PATH", "/venv/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/tmp", "--setenv", "PYTHONPATH", "/workspace/src:/workspace",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "KINVEST_GPT_SANDBOX", "1",
            "--chdir", "/workspace", "--cap-drop", "ALL", "--cap-add", "CAP_SETUID",
            "--cap-add", "CAP_SETGID", "--cap-add", "CAP_SETPCAP",
            "/usr/bin/setpriv", f"--reuid={os.getuid()}", f"--regid={os.getgid()}", "--clear-groups",
            "--bounding-set=-all", "--no-new-privs",
            "/usr/bin/timeout", "--kill-after=10", "600", "/venv/bin/python", "-m", "pytest", "-q",
            "-p", "no:cacheprovider", "--basetemp=/tmp/pytest"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=630)
    (log_path or workspace.parent / (workspace.name + ".test.log")).write_text((result.stdout + result.stderr)[-60000:])
    if result.returncode:
        raise ValueError("isolated_tests_failed:" + str(result.returncode))
    return result.stdout[-3000:]


def prepare_edit(project, job, sources, result):
    base = json.loads(job["payload_json"])["base"]
    if clean_base(project) != base:
        raise ValueError("base_changed_regenerate_edit")
    changes = apply_edits(sources, result["edits"])
    folder = bridge_root(project) / "jobs" / str(job["id"])
    diff = "".join("".join(difflib.unified_diff(sources[path].splitlines(True), text.splitlines(True),
                    fromfile="a/" + path, tofile="b/" + path)) for path, text in changes.items())
    (folder / "changes.diff").write_text(diff)
    with tempfile.TemporaryDirectory(prefix="kinvest-verify-") as temporary:
        workspace = Path(temporary)
        test_snapshot(project, base, workspace, changes)
        evidence = sandbox_test(project, workspace, folder / "tests.log")
    manifest = {"base": base, "files": changes, "tests": evidence, "tested_at": time.time(),
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()}
    (folder / "verified.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return f"\n격리 전체 테스트 통과. 변경 확인: /gpt_diff {job['id']}\n배포 요청: /gpt_deploy {job['id']}"


def load_manifest(project, source):
    if not source or source.get("kind") != "edit" or source["status"] != "succeeded":
        raise ValueError("successful_edit_required")
    folder = bridge_root(project) / "jobs" / str(source["id"])
    path = folder / "verified.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 400_000:
        raise ValueError("verified_manifest_required")
    value = json.loads(path.read_text())
    if time.time() - float(value["tested_at"]) > 3600:
        raise ValueError("edit_verification_expired")
    if hashlib.sha256((folder / "changes.diff").read_bytes()).hexdigest() != value["diff_sha256"]:
        raise ValueError("diff_integrity_failed")
    if not value["files"] or any(not allowed_path(path) for path in value["files"]):
        raise ValueError("invalid_manifest_paths")
    return value


def diff_page(project, job, page):
    if not job or job.get("kind") != "edit":
        raise ValueError("edit_job_required")
    path = bridge_root(project) / "jobs" / str(job["id"]) / "changes.diff"
    if not path.is_file():
        raise ValueError("diff_not_ready")
    text = path.read_text()
    count = max(1, (len(text) + 1799) // 1800)
    if not 1 <= page <= count:
        raise ValueError(f"diff_page_1_to_{count}")
    return f"[GPT #{job['id']} diff {page}/{count}]\n{text[(page-1)*1800:page*1800]}\n/gpt_diff {job['id']} {min(page+1,count)}"


def git_network_env(project):
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(project / "scripts/gpt_git_askpass.py"))
    return env


def service_command(action: str):
    subprocess.run(["systemctl", "--user", action, SERVICE], check=True, timeout=90,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def deploy(project, settings, store, job):
    from .config import load_app_config
    from .gpt_orders import paper_guard
    operation_guard(settings, "deploy")
    if time.time() - job["created_at"] > 600:
        raise ValueError("deployment_approval_expired")
    payload = json.loads(job["payload_json"])
    source = store.get(job["owner"], payload["source_id"])
    manifest = load_manifest(project, source)
    if digest(manifest) != payload["digest"] or clean_base(project) != manifest["base"]:
        raise ValueError("deployment_input_changed")
    paper_guard(load_app_config())
    if git(project, "branch", "--show-current") != "master":
        raise ValueError("master_branch_required")
    remote = git(project, "remote", "get-url", "--push", "origin")
    if remote != settings.get("git_remote") or not remote.startswith("https://github.com/"):
        raise ValueError("git_remote_mismatch")
    env = git_network_env(project)
    git(project, "fetch", "origin", "master", env=env)
    if git(project, "rev-parse", "refs/remotes/origin/master") != manifest["base"]:
        raise ValueError("remote_changed_sync_and_regenerate")
    guard_acceptance_deploy(project, manifest)
    folder = bridge_root(project) / "jobs" / str(job["id"])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A private index creates the commit without modifying the running checkout.
    env = {**env, "GIT_INDEX_FILE": str(folder / "index")}
    git(project, "read-tree", manifest["base"], env=env)
    for path, content in manifest["files"].items():
        blob = git(project, "hash-object", "-w", "--stdin", input=content, env=env)
        git(project, "update-index", "--cacheinfo", f"100644,{blob},{path}", env=env)
    log = (f"## Telegram approved deployment #{job['id']}\n\n"
           f"- Source job: {source['id']}; base: {manifest['base']}; manifest: {payload['digest']}\n"
           f"- Files: {', '.join(manifest['files'])}\n"
           "- Isolated full pytest passed; profitability NOT validated.\n"
           "- Approval, test output and deployment outcome: private state/gpt_bridge/jobs.sqlite3.\n\n")
    blob = git(project, "hash-object", "-w", "--stdin", input=log + git(project, "show", "HEAD:WORKLOG.md"), env=env)
    git(project, "update-index", "--cacheinfo", f"100644,{blob},WORKLOG.md", env=env)
    tree = git(project, "write-tree", env=env)
    commit = git(project, "commit-tree", tree, "-p", manifest["base"],
                 input=f"Apply owner-reviewed Telegram GPT edit #{source['id']}\n", env=env)
    (folder / "deployment.json").write_text(json.dumps({"base": manifest["base"], "commit": commit}))
    store.begin_side_effect(job["id"])
    git(project, "push", "origin", f"{commit}:refs/heads/master", env=env)
    if clean_base(project) != manifest["base"]:
        raise ValueError("pushed_but_checkout_changed_no_restart")
    try:
        service_command("stop")
        with sqlite3.connect(project / "data/trading.db") as source_db, sqlite3.connect(folder / "predeploy.db") as backup:
            source_db.backup(backup)
        git(project, "merge", "--ff-only", commit)
        service_command("start")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                runtime = json.loads((project / "state/runtime_state.json").read_text())
                observed = runtime.get("deployment", {})
                active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode == 0
                if active and str(observed.get("git_commit", "")) in {commit, commit[:12]} and observed.get("git_dirty") is False:
                    refresh_acceptance(project, runtime)
                    return f"배포 완료: {commit[:12]}\nGit push·운영 재시작·실행 커밋 확인 완료. 수익성 미검증."
            except OSError:
                pass
            time.sleep(2)
        raise ValueError("deployment_health_unverified_manual_review_required")
    except Exception:
        # Revert only our exact clean commit; never reset user changes or the DB.
        try:
            if clean_base(project) == commit:
                service_command("stop")
                git(project, "revert", "--no-edit", commit)
                rollback = git(project, "rev-parse", "HEAD")
                (folder / "rollback.json").write_text(json.dumps({"failed_commit": commit, "rollback": rollback}))
                git(project, "push", "origin", "HEAD:refs/heads/master", env=git_network_env(project))
        finally:
            service_command("start")
        raise ValueError("deployment_failed_rollback_recorded_check_status") from None


def guard_acceptance_deploy(project, manifest):
    from datetime import datetime, timezone
    from .execution_acceptance import load_arm
    from .repository import SqliteRepository
    from .time_utils import KST
    now = datetime.now(timezone.utc).astimezone(KST)
    repo = SqliteRepository.__new__(SqliteRepository)
    repo.db_path = project / "data/trading.db"
    existing = load_arm(repo, now.date().isoformat())
    if existing and (9 <= now.hour < 16 or now.hour < 9 and "config/market_policies/domestic.json" in manifest["files"]):
        raise ValueError("active_KR_acceptance_requires_separate_rearm_before_deploy")


def refresh_acceptance(project, runtime):
    from datetime import datetime, timezone
    from .config import load_app_config
    from .execution_acceptance import arm, load_arm
    from .repository import SqliteRepository
    from .time_utils import KST
    now = datetime.now(timezone.utc)
    config = load_app_config()
    repo = SqliteRepository.__new__(SqliteRepository)
    repo.db_path = project / "data/trading.db"
    session = now.astimezone(KST).date().isoformat()
    existing = load_arm(repo, session)
    if existing and now.astimezone(KST).hour < 9:
        arm(config, repo, runtime, session, existing["policy_id"], existing["evaluation_id"], now, refresh_deployment=True)
