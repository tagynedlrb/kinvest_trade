from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from .auto_trade_math import is_domestic_sell_tax_exempt
from .config import account_fingerprint, load_app_config
from .execution_reconciler import BrokerExecutionReconciler
from .market_calendar import is_krx_holiday
from .market_policy import MarketPolicyRegistry
from .notifier import TelegramNotifier
from .repository import SqliteRepository
from .strategy.manager import PriorityStrategyManager
from .technical_signals import MovingAverageSnapshot
from .time_utils import KST, parse_datetime


ARM_EVENT = "domestic_execution_acceptance_armed"
CHECK_EVENT = "domestic_execution_acceptance_check"


def _decode(value):
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@contextmanager
def _read_db(repository):
    db = sqlite3.connect(repository.db_path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _paper_guard(config):
    c = config.credentials
    if c.env != "vps" or c.dry_run or c.live_trading_enabled:
        raise ValueError("enabled_paper_account_required")
    if not account_fingerprint(c):
        raise ValueError("account_identity_required")


def load_arm(repository, session_date):
    with _read_db(repository) as db:
        row = db.execute(
            "SELECT detail FROM event_log WHERE event_type=? "
            "AND json_extract(detail, '$.session_date')=? ORDER BY id DESC LIMIT 1",
            (ARM_EVENT, session_date),
        ).fetchone()
    return _decode(row[0]) if row else None


def arm(config, repository, runtime, session_date, policy_id, evaluation_id, now):
    _paper_guard(config)
    day = date.fromisoformat(session_date)
    if day < now.astimezone(KST).date() or is_krx_holiday(day):
        raise ValueError("future_or_current_krx_session_required")
    policy = MarketPolicyRegistry(config).for_market("domestic")
    if policy.policy_id != policy_id:
        raise ValueError("requested_policy_not_loaded")
    with _read_db(repository) as db:
        if not db.execute(
            "SELECT id FROM policy_evaluation_log WHERE id=?", (evaluation_id,)
        ).fetchone():
            raise ValueError("policy_evaluation_not_found")
    identity = account_fingerprint(config.credentials)
    deployment = runtime.get("deployment", {})
    if (
        runtime.get("linked_account_fingerprint") != identity
        or deployment.get("domestic_policy_id") != policy_id
        or deployment.get("git_dirty")
        or not deployment.get("git_commit")
        or runtime.get("telegram_control", {}).get("mode") != "running"
    ):
        raise ValueError("running_clean_matching_deployment_required")
    manifest = {
        "session_date": session_date,
        "policy_id": policy_id,
        "policy_parameter_fingerprint": policy.parameter_fingerprint,
        "account_fingerprint": identity,
        "environment": "vps",
        "deployment_commit": deployment["git_commit"],
        "evaluation_id": evaluation_id,
    }
    existing = load_arm(repository, session_date)
    if existing:
        if any(existing.get(k) != v for k, v in manifest.items()):
            raise ValueError("existing_verification_identity_mismatch")
        return existing
    close = datetime.combine(day, time(15, 30), KST)
    if now >= close - timedelta(
        minutes=policy.auto_trade.entry_min_minutes_to_regular_close
    ):
        raise ValueError("new_verification_requires_remaining_entry_window")
    manifest.update(
        verification_id=uuid.uuid4().hex,
        armed_at=now.isoformat(),
        open_at=datetime.combine(day, time(9), KST).isoformat(),
        entry_deadline=(
            close
            - timedelta(minutes=policy.auto_trade.entry_min_minutes_to_regular_close)
        ).isoformat(),
        final_at=(close + timedelta(minutes=10)).isoformat(),
        order_submission_owner="kinvest-telegram-control.service",
        verifier_can_submit_orders=False,
    )
    repository.save_event(event_type=ARM_EVENT, market="domestic", detail=manifest)
    return manifest


def _phase(manifest, now):
    opened = parse_datetime(manifest["open_at"])
    if now < opened - timedelta(minutes=5):
        return "SCHEDULED"
    if now < opened:
        return "PREOPEN"
    if now >= parse_datetime(manifest["final_at"]):
        return "FINAL"
    if now >= parse_datetime(manifest["entry_deadline"]):
        return "ENTRY_CLOSED"
    if now >= opened + timedelta(hours=3):
        return "MIDDAY"
    if now >= opened + timedelta(minutes=30):
        return "MORNING_CHECKPOINT"
    return "OPENING"


def _matches(context, manifest):
    return all(
        context.get(k) == manifest[k]
        for k in (
            "policy_id",
            "policy_parameter_fingerprint",
            "account_fingerprint",
            "environment",
        )
    )


def _confirmed_fill(row, manifest):
    history = _decode(row["history_json"])
    normalize = SqliteRepository.normalize_broker_order_no
    return bool(
        row["filled_qty"] > 0
        and parse_datetime(row["fill_recorded_at"]) is not None
        and row["avg_fill_price"]
        and row["avg_fill_price"] > 0
        and row["cycle_log_id"]
        and row["is_session_trade"]
        and row["cycle_virtual"] == 0
        and row["cycle_session_trade"] == 1
        and row["cycle_action"] == f"{row['side'].upper()}_REAL"
        and row["cycle_symbol"] == row["symbol"]
        and row["cycle_group"] == row["execution_group_id"]
        and row["cycle_qty"] >= row["filled_qty"]
        and history.get("ord_dt")
        == date.fromisoformat(manifest["session_date"]).strftime("%Y%m%d")
        and normalize(history.get("odno")) == normalize(row["broker_order_no"])
        and history.get("pdno") == row["symbol"]
        and history.get("sll_buy_dvsn_cd")
        == ("02" if row["side"].upper() == "BUY" else "01")
        and BrokerExecutionReconciler._filled_qty("domestic", history)
        == row["filled_qty"]
    )


def _policy_evidence_issues(row, policy, manifest):
    context = _decode(row["context_json"])
    auto = policy.auto_trade
    problems = []
    strategy = row["strategy_flag"]
    if strategy not in auto.entry_strategy_allowlist:
        problems.append("strategy_not_allowed")
    submitted = parse_datetime(row["created_at"])
    if submitted is None or submitted > parse_datetime(manifest["entry_deadline"]):
        problems.append("entry_after_deadline")
    try:
        snapshot = MovingAverageSnapshot(**context.get("signal_snapshot", {}))
        result = PriorityStrategyManager(auto).evaluate(
            row["symbol"], snapshot, commit=False
        )
        if result.signal != "BUY" or result.flag != strategy:
            problems.append("recorded_strategy_signal_not_reproduced")
        if strategy in auto.entry_confirmation_strategy_flags:
            setup = policy.evaluate_entry(snapshot, symbol=row["symbol"])
            if not setup.ready:
                problems.append(f"formula_not_ready:{setup.reason}")
    except (TypeError, ValueError):
        problems.append("signal_evidence_missing")
    if strategy in auto.strategy_guard_force_probe_strategy_flags:
        probe = context.get("strategy_guard_probe", {})
        regime = context.get("entry_market_regime", {})
        if not probe.get("admitted"):
            problems.append("required_probe_not_admitted")
        if (
            not 0
            < float(probe.get("slot_multiplier") or 1)
            <= auto.strategy_guard_probe_slot_multiplier
        ):
            problems.append("probe_size_limit_mismatch")
        if (
            auto.strategy_guard_probe_tax_exempt_only
            and not is_domestic_sell_tax_exempt(context.get("product_type", ""))
        ):
            problems.append("taxable_product")
        if (
            not regime.get("available")
            or regime.get("session_date") != manifest["session_date"]
            or regime.get("observation_age_sec") is None
            or not 0
            <= float(regime["observation_age_sec"])
            <= auto.strategy_guard_probe_regime_max_age_sec
            or regime.get("return_pct") is None
            or float(regime["return_pct"])
            < auto.strategy_guard_probe_benchmark_floor_pct
        ):
            problems.append("benchmark_evidence_invalid")
    if strategy == "INV" and auto.inverse_execution_mode == "shadow":
        problems.append("inverse_shadow_only")
    return problems


def inspect(config, repository, runtime, manifest, now):
    _paper_guard(config)
    policy = MarketPolicyRegistry(config).for_market("domestic")
    phase = _phase(manifest, now)
    health = []
    if account_fingerprint(config.credentials) != manifest["account_fingerprint"]:
        health.append("ACCOUNT_CHANGED")
    if (
        policy.policy_id != manifest["policy_id"]
        or policy.parameter_fingerprint != manifest["policy_parameter_fingerprint"]
    ):
        health.append("POLICY_CHANGED")
    deployment = runtime.get("deployment", {})
    if (
        deployment.get("git_commit") != manifest["deployment_commit"]
        or deployment.get("git_dirty")
        or deployment.get("domestic_policy_id") != manifest["policy_id"]
        or runtime.get("linked_account_fingerprint") != manifest["account_fingerprint"]
    ):
        health.append("DEPLOYMENT_OR_ACCOUNT_MISMATCH")
    control = runtime.get("telegram_control", {})
    if control.get("mode") != "running":
        health.append("SERVICE_NOT_RUNNING")
    updated = parse_datetime(runtime.get("updated_at"))
    if updated is None or (now - updated).total_seconds() > 900:
        health.append("RUNTIME_STALE")
    if is_krx_holiday(date.fromisoformat(manifest["session_date"])):
        health.append("MARKET_HOLIDAY")
    start = parse_datetime(manifest["open_at"]).astimezone(timezone.utc).isoformat()
    end = (
        min(now, parse_datetime(manifest["final_at"]))
        .astimezone(timezone.utc)
        .isoformat()
    )
    with _read_db(repository) as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT e.*, b.is_virtual, c.action_bias cycle_action, c.symbol cycle_symbol, "
                "c.execution_group_id cycle_group, c.qty_executed cycle_qty, "
                "c.is_virtual cycle_virtual, c.is_session_trade cycle_session_trade, c.net_pnl_krw "
                "FROM broker_order_executions e JOIN broker_order_events b ON b.id=e.broker_event_id "
                "LEFT JOIN cycle_log c ON c.id=e.cycle_log_id "
                "WHERE e.market='domestic' AND e.created_at>=? AND e.created_at<=? ORDER BY e.id",
                (start, end),
            )
        ]
        decisions = [
            dict(r)
            for r in db.execute(
                "SELECT action_bias,action_reason,count(*) n FROM cycle_log "
                "WHERE market='domestic' AND logged_at>=? AND logged_at<=? "
                "GROUP BY action_bias,action_reason ORDER BY n DESC",
                (start, end),
            )
        ]
        rejections = [
            dict(r)
            for r in db.execute(
                "SELECT id,symbol,reason,payload_json FROM broker_order_events "
                "WHERE market='domestic' AND side='BUY' AND status='REJECTED' "
                "AND is_virtual=0 AND created_at>=? AND created_at<=?",
                (start, end),
            )
        ]
        funnel = db.execute(
            "SELECT logged_at,detail FROM event_log WHERE event_type='domestic_candidate_funnel' "
            "AND logged_at>=? AND logged_at<=? ORDER BY id DESC LIMIT 1",
            (start, end),
        ).fetchone()
        regime = db.execute(
            "SELECT captured_at,is_final,benchmark_code,close_price,return_pct,volume_ratio_20,regime_key "
            "FROM market_regime_observations WHERE market='domestic' AND session_date=? "
            "AND captured_at<=? ORDER BY captured_at DESC LIMIT 1",
            (manifest["session_date"], end),
        ).fetchone()
        api_errors = [
            dict(r)
            for r in db.execute(
                "SELECT path,msg_cd,count(*) n FROM api_call_log WHERE created_at>=? AND created_at<=? "
                "AND path LIKE '/uapi/domestic%' AND success=0 AND logical_terminal=1 GROUP BY path,msg_cd",
                (start, end),
            )
        ]
    valid = [
        r
        for r in rows
        if not r["is_virtual"]
        and r["is_session_trade"]
        and _matches(_decode(r["context_json"]), manifest)
    ]
    buys = [r for r in valid if r["side"].upper() == "BUY"]
    policy_issues = []
    for row in buys:
        try:
            problems = _policy_evidence_issues(row, policy, manifest)
        except (TypeError, ValueError, KeyError) as exc:
            problems = [f"malformed_policy_evidence:{type(exc).__name__}"]
        if problems:
            policy_issues.append({"execution_id": row["id"], "reasons": problems})
    filled = [r for r in buys if _confirmed_fill(r, manifest)]
    entry_times = {(r["symbol"], parse_datetime(r["fill_recorded_at"])) for r in filled}
    sells = [
        r
        for r in valid
        if r["side"].upper() == "SELL"
        and _confirmed_fill(r, manifest)
        and (r["symbol"], parse_datetime(r["entry_time"])) in entry_times
    ]
    bought, sold = Counter(), Counter()
    for r in filled:
        bought[r["symbol"]] += r["filled_qty"]
    for r in sells:
        sold[r["symbol"]] += r["filled_qty"]
    roundtrip = (
        bool(bought) and bought == sold and all(not r["remaining_qty"] for r in buys)
    )
    rejected = [
        r
        for r in rejections
        if _matches(_decode(r["payload_json"]).get("execution_context", {}), manifest)
    ]
    buy_observations = sum(r["n"] for r in decisions if r["action_bias"] == "BUY")
    entry_skips = sum(
        r["n"]
        for r in decisions
        if r["action_bias"] == "SKIP"
        and (r["action_reason"].startswith("buy:") or "entry_" in r["action_reason"])
    )
    status = (
        "POLICY_EVIDENCE_INVALID"
        if policy_issues
        else "ROUNDTRIP_CONFIRMED"
        if roundtrip
        else "ENTRY_CONFIRMED"
        if filled
        else "BROKER_FILL_LEDGER_PENDING"
        if any(r["filled_qty"] for r in buys)
        else "SUBMITTED_NOT_FILLED"
        if buys
        else "ORDER_REJECTED"
        if rejected
        else "BUY_OBSERVED_NO_SUBMISSION"
        if buy_observations
        else "PRE_SUBMISSION_BLOCKED"
        if entry_skips
        else "NO_BUY_SIGNAL"
        if decisions
        else "ARMED"
        if phase in {"SCHEDULED", "PREOPEN"}
        else "NO_SCAN_EVIDENCE"
    )
    unverified = [r["id"] for r in rows if not r["is_virtual"] and r not in valid]
    if unverified:
        health.append("UNVERIFIED_ORDER_CONTEXT")
    return {
        "verification_id": manifest["verification_id"],
        "session_date": manifest["session_date"],
        "evaluation_id": manifest["evaluation_id"],
        "checked_at": now.isoformat(),
        "phase": phase,
        "status": status,
        "health_issues": health,
        "policy_id": manifest["policy_id"],
        "entry_execution_verified": bool(filled),
        "policy_evidence_verified": bool(filled) and not policy_issues,
        "policy_evidence_issues": policy_issues,
        "roundtrip_verified": roundtrip and not policy_issues,
        "profitability_validated": False,
        "buy_observations": buy_observations,
        "submitted_buy_count": len(buys),
        "confirmed_buy_count": len(filled),
        "confirmed_sell_count": len(sells),
        "orders": [
            {
                k: r[k]
                for k in (
                    "id",
                    "symbol",
                    "side",
                    "broker_order_no",
                    "status",
                    "requested_qty",
                    "filled_qty",
                    "remaining_qty",
                    "avg_fill_price",
                    "cycle_log_id",
                )
            }
            for r in valid
        ],
        "unverified_execution_ids": unverified,
        "rejected_order_ids": [r["id"] for r in rejected],
        "decision_reasons": decisions[:12],
        "api_terminal_errors": api_errors,
        "candidate_funnel": _decode(funnel["detail"]) if funnel else {},
        "benchmark": dict(regime) if regime else {},
        "closed_net_pnl_krw": (
            sum({r["cycle_log_id"]: r["net_pnl_krw"] for r in sells}.values())
            if sells and all(r["net_pnl_krw"] is not None for r in sells)
            else None
        ),
        "final": phase == "FINAL",
        "last_completed_at": control.get("last_completed_at"),
    }


def _notice_key(report):
    fields = {
        k: report[k]
        for k in (
            "verification_id",
            "phase",
            "status",
            "health_issues",
            "confirmed_buy_count",
            "confirmed_sell_count",
        )
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def format_report(report):
    reasons = (
        ", ".join(
            f"{r['action_reason']}={r['n']}" for r in report["decision_reasons"][:4]
        )
        or "관측 없음"
    )
    orders = (
        "; ".join(
            f"{r['side']} {r['symbol']} #{r['broker_order_no']} 체결 {r['filled_qty']}/{r['requested_qty']} @{r['avg_fill_price']}"
            for r in report["orders"][:6]
        )
        or "주문 없음"
    )
    net = report["closed_net_pnl_krw"]
    net_text = f"{net:.2f}원" if net is not None else "확정 청산/비용 자료 없음"
    funnel = report["candidate_funnel"]
    benchmark = report["benchmark"]
    return "\n".join(
        [
            f"[KIS][국장 자동체결 검증] {report['session_date']}",
            f"정책={report['policy_id']} / 단계={report['phase']} / 결과={report['status']}",
            f"브로커 매수 체결확정={report['confirmed_buy_count']} / 매도 체결확정={report['confirmed_sell_count']}",
            f"매수 신호 관측={report['buy_observations']} / 매수접수={report['submitted_buy_count']}",
            f"체결 주문 정책증거 검증={report['policy_evidence_verified']} / 불일치={len(report['policy_evidence_issues'])}",
            orders,
            f"조건/차단 사유(반복 관측)={reasons}",
            f"후보 조회={funnel.get('discovered_count', '미확보')} / 상품·호가 적격={funnel.get('quote_eligible_count', '미확보')}",
            f"시장={benchmark.get('benchmark_code', '미확보')} / 등락%={benchmark.get('return_pct')} / 거래량비20={benchmark.get('volume_ratio_20')} / 종가확정={bool(benchmark.get('is_final'))}",
            "장중 거래량비는 누적량/20일 평균이며 시간대 보정 지표가 아닙니다.",
            f"상태 문제={','.join(report['health_issues']) or '없음'} / 최종 조회실패={sum(r['n'] for r in report['api_terminal_errors'])}",
            f"확정 청산 비용후 손익={net_text} / 수익성 검증=별도 표본 필요",
            "진단/가상 체결 제외. 미체결·무신호를 성공으로 처리하지 않습니다.",
            "당일 검증 종료. 미확인 항목은 원인과 함께 보존합니다."
            if report["final"]
            else "기존 자동매매만 추적하며 검증기가 별도 주문을 제출하지 않습니다.",
        ]
    )


async def check(
    config, repository, runtime, manifest, now, *, notify=False, notifier=None
):
    _paper_guard(config)
    with _read_db(repository) as db:
        finalized = db.execute(
            "SELECT detail FROM event_log WHERE event_type=? "
            "AND json_extract(detail,'$.verification_id')=? "
            "AND json_extract(detail,'$.final')=1 AND json_extract(detail,'$.notification_sent')=1 "
            "ORDER BY id DESC LIMIT 1",
            (CHECK_EVENT, manifest["verification_id"]),
        ).fetchone()
    if finalized:
        return _decode(finalized[0])
    report = inspect(config, repository, runtime, manifest, now)
    report["notice_key"] = _notice_key(report)
    with _read_db(repository) as db:
        sent = db.execute(
            "SELECT id FROM event_log WHERE event_type=? AND json_extract(detail,'$.notice_key')=? "
            "AND json_extract(detail,'$.notification_sent')=1 LIMIT 1",
            (CHECK_EVENT, report["notice_key"]),
        ).fetchone()
    report["notification_sent"] = bool(sent)
    if notify and not sent:
        try:
            report["notification_sent"] = bool(
                await notifier.send(format_report(report))
            )
        except Exception as exc:
            report["notification_error_type"] = type(exc).__name__
    repository.save_event(event_type=CHECK_EVENT, market="domestic", detail=report)
    repository.merge_policy_evaluation_outcome(
        manifest["evaluation_id"],
        {"execution_acceptance_latest": report},
    )
    return report


async def main():
    parser = argparse.ArgumentParser(
        description="Observe natural paper strategy orders; never submit diagnostic orders."
    )
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--policy-id", default="domestic_momentum_v12")
    parser.add_argument("--evaluation-id", type=int, default=143)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    config = load_app_config()
    repository = SqliteRepository(config.storage.db_path)
    path = config.storage.runtime_state_path
    with path.with_name("execution_acceptance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runtime = _decode(path.read_text()) if path.exists() else {}
        now = datetime.now(timezone.utc)
        if args.arm:
            manifest = arm(
                config,
                repository,
                runtime,
                args.session_date,
                args.policy_id,
                args.evaluation_id,
                now,
            )
            repository.merge_policy_evaluation_outcome(
                args.evaluation_id, {"execution_acceptance_plan": manifest}
            )
        else:
            manifest = load_arm(repository, args.session_date)
            if not manifest:
                raise ValueError("verification_not_armed")
        result = await check(
            config,
            repository,
            runtime,
            manifest,
            now,
            notify=args.notify,
            notifier=TelegramNotifier(config.notifications, repository=repository),
        )
        print(json.dumps(result, ensure_ascii=False))
        if args.notify and not result["notification_sent"]:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
