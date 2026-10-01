import asyncio
import json
import sqlite3
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kinvest_trade.config import account_fingerprint, load_app_config
from kinvest_trade.execution_acceptance import arm, check, inspect, load_arm
from kinvest_trade.market_policy import MarketPolicyRegistry
from kinvest_trade.repository import SqliteRepository
from kinvest_trade.technical_signals import MovingAverageSnapshot


@pytest.fixture
def case(tmp_path):
    config = load_app_config()
    config.credentials = replace(
        config.credentials,
        env="vps",
        account_no="TEST1234",
        dry_run=False,
        live_trading_enabled=False,
    )
    repository = SqliteRepository(tmp_path / "acceptance.db")
    evaluation_id = repository.save_policy_evaluation(
        created_at="2026-10-01T09:00:00+00:00",
        market="domestic",
        evaluation_kind="test",
        subject="acceptance",
        decision="paper_only",
        hypothesis="natural fill",
        outcome={"old_evidence": True},
    )
    now = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)
    runtime = {
        "updated_at": now.isoformat(),
        "linked_account_fingerprint": account_fingerprint(config.credentials),
        "deployment": {
            "git_commit": "test-commit",
            "git_dirty": False,
            "domestic_policy_id": "domestic_momentum_v12",
        },
        "telegram_control": {"mode": "running", "last_completed_at": now.isoformat()},
    }
    manifest = arm(
        config,
        repository,
        runtime,
        "2026-10-02",
        "domestic_momentum_v12",
        evaluation_id,
        now - timedelta(days=1),
    )
    return SimpleNamespace(
        config=config,
        repository=repository,
        runtime=runtime,
        manifest=manifest,
        now=now,
        evaluation_id=evaluation_id,
    )


def observe(c, now=None):
    when = now or c.now
    c.runtime["updated_at"] = when.isoformat()
    return inspect(c.config, c.repository, c.runtime, c.manifest, when)


def test_preopen_deployment_refresh_preserves_arm_history(case):
    c = case
    c.runtime["deployment"]["git_commit"] = "new-commit"
    before_open = c.now - timedelta(days=1)
    args = (c.config, c.repository, c.runtime, "2026-10-02", "domestic_momentum_v12", c.evaluation_id, before_open)
    with pytest.raises(ValueError, match="identity_mismatch"):
        arm(*args)
    new = arm(*args, refresh_deployment=True)
    assert new["supersedes_verification_id"] == c.manifest["verification_id"]
    assert new["policy_parameter_fingerprint"] == c.manifest["policy_parameter_fingerprint"]
    assert load_arm(c.repository, "2026-10-02")["verification_id"] == new["verification_id"]
    with sqlite3.connect(c.repository.db_path) as db:
        assert db.execute("SELECT count(*) FROM event_log WHERE event_type='domestic_execution_acceptance_armed'").fetchone()[0] == 2


@pytest.mark.parametrize("change_policy", [False, True])
def test_refresh_cannot_hide_mid_session_deployment_or_changed_policy(case, change_policy):
    c = case
    c.runtime["deployment"]["git_commit"] = "new-commit"
    now = c.now
    if change_policy:
        c.config.market_policies.domestic.auto_trade.stop_loss_pct += 0.001
        now -= timedelta(days=1)
    with pytest.raises(ValueError, match="identity_mismatch"):
        arm(c.config, c.repository, c.runtime, "2026-10-02", "domestic_momentum_v12", c.evaluation_id, now, refresh_deployment=True)


def seed_order(
    c,
    *,
    side="BUY",
    filled=0,
    requested=2,
    virtual=0,
    wrong_identity=False,
    bad_history=False,
    missing_cycle=False,
    entry_time=None,
    bad_policy=False,
    net=-100.0,
):
    repository = c.repository
    created = datetime(
        2026, 10, 2, 1 if side == "BUY" else 2, tzinfo=timezone.utc
    ).isoformat()
    no = "000100" if side == "BUY" else "000101"
    snapshot = {f.name: 0.0 for f in fields(MovingAverageSnapshot)}
    snapshot.update(
        price=10000, rsi14=29, volume_ratio=1.5, regime="pullback", macd_golden=True
    )
    context = {
        k: c.manifest[k]
        for k in (
            "policy_id",
            "policy_parameter_fingerprint",
            "account_fingerprint",
            "environment",
        )
    }
    context.update(
        signal_snapshot=snapshot,
        product_type="STOCK" if bad_policy else "ETF",
        strategy_guard_probe={"admitted": True, "slot_multiplier": 0.1},
        entry_market_regime={
            "available": True,
            "session_date": "2026-10-02",
            "observation_age_sec": 20,
            "return_pct": 0.5,
        },
    )
    if wrong_identity:
        context["account_fingerprint"] = "old-account"
    event = repository.save_broker_order_event(
        created_at=created,
        market="domestic",
        symbol="229200",
        exchange_code=None,
        side=side,
        order_kind="limit",
        requested_qty=requested,
        requested_price=10000,
        strategy_flag="RSI",
        status="SUBMITTED",
        broker_order_no=no,
        is_virtual=virtual,
        payload={"execution_context": context},
    )
    execution = repository.save_broker_order_execution(
        broker_event_id=event,
        created_at=created,
        market="domestic",
        symbol="229200",
        exchange_code=None,
        side=side,
        broker_order_no=no,
        requested_qty=requested,
        requested_price=10000,
        strategy_flag="RSI",
        session_id="natural-policy",
        cycle_no=1,
        is_session_trade=1,
        context=context,
        entry_time=entry_time,
    )
    with sqlite3.connect(repository.db_path) as db:
        cycle_id = None
        if filled and not missing_cycle:
            cycle_id = db.execute(
                "INSERT INTO cycle_log(logged_at,market,symbol,action_bias,action_reason,"
                "qty_executed,is_virtual,is_session_trade,execution_group_id,net_pnl_krw) "
                "VALUES (?,'domestic','229200',?,'strategy',?,0,1,?,?)",
                (created, side + "_REAL", filled, execution["execution_group_id"], net),
            ).lastrowid
        history = {
            "ord_dt": "20261002",
            "odno": "999" if bad_history else no,
            "pdno": "229200",
            "sll_buy_dvsn_cd": "02" if side == "BUY" else "01",
            "tot_ccld_qty": str(filled),
            "avg_prvs": "10000",
        }
        db.execute(
            "UPDATE broker_order_executions SET filled_qty=?,remaining_qty=?,avg_fill_price=?,"
            "status=?,fill_recorded_at=?,history_json=?,cycle_log_id=? WHERE id=?",
            (
                filled,
                requested - filled,
                10000 if filled else None,
                "FILLED" if filled == requested else "PARTIAL" if filled else "PENDING",
                created if filled else None,
                json.dumps(history),
                cycle_id,
                execution["id"],
            ),
        )
    return created


def test_arm_is_idempotent_and_has_finite_deadlines(case):
    c = case
    again = arm(
        c.config,
        c.repository,
        c.runtime,
        "2026-10-02",
        "domestic_momentum_v12",
        c.evaluation_id,
        c.now - timedelta(days=1),
    )
    assert again == c.manifest == load_arm(c.repository, "2026-10-02")
    assert c.manifest["entry_deadline"] == "2026-10-02T14:30:00+09:00"
    assert c.manifest["final_at"] == "2026-10-02T15:40:00+09:00"
    assert not c.manifest["verifier_can_submit_orders"]


@pytest.mark.parametrize(
    "changes", [{"env": "prod"}, {"dry_run": True}, {"live_trading_enabled": True}]
)
def test_real_or_disabled_accounts_rejected(case, changes):
    case.config.credentials = replace(case.config.credentials, **changes)
    with pytest.raises(ValueError, match="enabled_paper"):
        observe(case)


def test_no_signals_never_pass_even_after_final_deadline(case):
    report = observe(case, datetime(2026, 10, 2, 7, tzinfo=timezone.utc))
    assert report["final"] and report["status"] == "NO_SCAN_EVIDENCE"
    assert (
        not report["entry_execution_verified"] and not report["profitability_validated"]
    )


def test_diagnostic_or_old_account_order_not_counted(case):
    seed_order(case, filled=2, wrong_identity=True)
    report = observe(case)
    assert report["confirmed_buy_count"] == 0
    assert "UNVERIFIED_ORDER_CONTEXT" in report["health_issues"]


def test_virtual_fill_not_counted(case):
    seed_order(case, filled=2, virtual=1)
    assert observe(case)["confirmed_buy_count"] == 0


def test_submitted_is_not_filled(case):
    seed_order(case)
    report = observe(case)
    assert report["status"] == "SUBMITTED_NOT_FILLED"
    assert not report["entry_execution_verified"]


@pytest.mark.parametrize("kwargs", [{"bad_history": True}, {"missing_cycle": True}])
def test_fill_needs_matching_broker_history_and_confirmed_cycle(case, kwargs):
    seed_order(case, filled=2, **kwargs)
    report = observe(case)
    assert report["status"] == "BROKER_FILL_LEDGER_PENDING"
    assert not report["entry_execution_verified"]


def test_policy_entry_fill_confirmed_but_profitability_not_assumed(case):
    seed_order(case, filled=2)
    report = observe(case)
    assert report["status"] == "ENTRY_CONFIRMED"
    assert report["policy_evidence_verified"]
    assert not report["roundtrip_verified"] and not report["profitability_validated"]


def test_policy_violation_not_accepted_even_with_real_fill(case):
    seed_order(case, filled=2, bad_policy=True)
    report = observe(case)
    assert report["entry_execution_verified"]
    assert report["status"] == "POLICY_EVIDENCE_INVALID"
    assert not report["policy_evidence_verified"]


def test_partial_entry_not_roundtrip(case):
    seed_order(case, filled=1)
    report = observe(case)
    assert report["entry_execution_verified"] and not report["roundtrip_verified"]


def test_linked_roundtrip_uses_cost_adjusted_result(case):
    entered = seed_order(case, filled=2)
    seed_order(case, side="SELL", filled=2, entry_time=entered)
    report = observe(case, datetime(2026, 10, 2, 3, tzinfo=timezone.utc))
    assert report["status"] == "ROUNDTRIP_CONFIRMED"
    assert report["closed_net_pnl_krw"] == -100
    assert not report["profitability_validated"]


def test_unrelated_exit_cannot_complete_roundtrip(case):
    seed_order(case, filled=2)
    seed_order(case, side="SELL", filled=2, entry_time="2026-10-01T01:00:00+00:00")
    report = observe(case, datetime(2026, 10, 2, 3, tzinfo=timezone.utc))
    assert report["status"] == "ENTRY_CONFIRMED"


def test_policy_and_runtime_drift_are_reported(case):
    case.config.market_policies.domestic.auto_trade.volume_spike_ratio += 0.1
    case.runtime["telegram_control"]["mode"] = "paused"
    report = observe(case)
    assert "POLICY_CHANGED" in report["health_issues"]
    assert "SERVICE_NOT_RUNNING" in report["health_issues"]


def test_policy_fingerprint_is_market_independent(case):
    registry = MarketPolicyRegistry(case.config)
    before = registry.for_market("domestic").parameter_fingerprint
    case.config.market_policies.overseas.auto_trade.volume_spike_ratio += 0.1
    assert registry.for_market("domestic").parameter_fingerprint == before
    case.config.market_policies.domestic.auto_trade.volume_spike_ratio += 0.1
    assert registry.for_market("domestic").parameter_fingerprint != before


def test_failed_notification_retries_and_deduplicates_after_success(case):
    calls = []

    async def send(message):
        calls.append(message)
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return True

    async def run():
        reports = []
        for _ in range(3):
            reports.append(
                await check(
                    case.config,
                    case.repository,
                    case.runtime,
                    case.manifest,
                    case.now,
                    notify=True,
                    notifier=SimpleNamespace(send=send),
                )
            )
        return reports

    reports = asyncio.run(run())
    assert len(calls) == 2
    assert not reports[0]["notification_sent"]
    assert reports[1]["notification_sent"] and reports[2]["notification_sent"]
    with sqlite3.connect(case.repository.db_path) as db:
        outcome, reviewed = db.execute(
            "SELECT outcome_json,reviewed_at FROM policy_evaluation_log"
        ).fetchone()
    assert json.loads(outcome)["old_evidence"] is True
    assert reviewed is None


def test_final_report_does_not_restart_completed_verification(case):
    calls = []

    async def send(message):
        calls.append(message)
        return True

    async def run():
        now = datetime(2026, 10, 2, 7, tzinfo=timezone.utc)
        case.runtime["updated_at"] = now.isoformat()
        first = await check(
            case.config,
            case.repository,
            case.runtime,
            case.manifest,
            now,
            notify=True,
            notifier=SimpleNamespace(send=send),
        )
        second = await check(
            case.config,
            case.repository,
            {},
            case.manifest,
            now + timedelta(days=1),
            notify=True,
            notifier=SimpleNamespace(send=send),
        )
        assert second == first

    asyncio.run(run())
    assert len(calls) == 1


def test_malformed_policy_evidence_is_a_failure_not_a_crash(case):
    seed_order(case, filled=2)
    with sqlite3.connect(case.repository.db_path) as db:
        context = json.loads(
            db.execute("SELECT context_json FROM broker_order_executions").fetchone()[0]
        )
        context["strategy_guard_probe"]["slot_multiplier"] = "bad"
        db.execute(
            "UPDATE broker_order_executions SET context_json=?", (json.dumps(context),)
        )
    report = observe(case)
    assert report["status"] == "POLICY_EVIDENCE_INVALID"
    assert not report["policy_evidence_verified"]


def test_missing_costs_do_not_become_zero_profit(case):
    entered = seed_order(case, filled=2)
    seed_order(case, side="SELL", filled=2, entry_time=entered, net=None)
    report = observe(case, datetime(2026, 10, 2, 3, tzinfo=timezone.utc))
    assert report["roundtrip_verified"]
    assert report["closed_net_pnl_krw"] is None


def test_buy_observed_without_submission_is_not_called_condition_failure(case):
    with sqlite3.connect(case.repository.db_path) as db:
        db.execute(
            "INSERT INTO cycle_log(logged_at,market,symbol,action_bias,action_reason) VALUES (?,'domestic','229200','BUY','strategy_buy_signal')",
            (case.now.isoformat(),),
        )
    assert observe(case)["status"] == "BUY_OBSERVED_NO_SUBMISSION"


def test_no_buy_signal_and_pre_submission_block_are_distinct(case):
    with sqlite3.connect(case.repository.db_path) as db:
        db.execute(
            "INSERT INTO cycle_log(logged_at,market,symbol,action_bias,action_reason) VALUES (?,'domestic','229200','WAIT','volume_low')",
            (case.now.isoformat(),),
        )
    assert observe(case)["status"] == "NO_BUY_SIGNAL"
    with sqlite3.connect(case.repository.db_path) as db:
        db.execute(
            "INSERT INTO cycle_log(logged_at,market,symbol,action_bias,action_reason) VALUES (?,'domestic','229200','SKIP','buy:entry_rsi_too_high')",
            (case.now.isoformat(),),
        )
    assert observe(case)["status"] == "PRE_SUBMISSION_BLOCKED"


def test_recorded_buy_requires_actual_strategy_confirmation(case):
    seed_order(case, filled=2)
    with sqlite3.connect(case.repository.db_path) as db:
        context = json.loads(
            db.execute("SELECT context_json FROM broker_order_executions").fetchone()[0]
        )
        context["signal_snapshot"]["macd_golden"] = False
        db.execute(
            "UPDATE broker_order_executions SET context_json=?", (json.dumps(context),)
        )
    report = observe(case)
    assert report["status"] == "POLICY_EVIDENCE_INVALID"
    assert (
        "recorded_strategy_signal_not_reproduced"
        in report["policy_evidence_issues"][0]["reasons"]
    )
