from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .time_utils import parse_datetime
from .strategy import PriorityStrategyManager, STRATEGY_LABEL


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy_trials.json"
ARMS = ("baseline_signal_v1", "rolling_vwap_recovery_v1", "trend_pullback_v1",
        "component_vwap_v1", "component_vol_v1", "component_rsi_v1", "component_mom_v1")
ENGINE_VERSION = "next_observation_comparable_execution_v2"


def independent_entry_signals(snapshot, symbol: str, auto_trade) -> dict[str, bool]:
    manager = PriorityStrategyManager(auto_trade)
    result = {f"component_{STRATEGY_LABEL[key].lower()}_v1": value.buy
              for key, value in manager.entry_component_signals(snapshot).items()}
    result[ARMS[0]] = manager.evaluate(symbol, snapshot, commit=False).signal == "BUY"
    return result


def trial_db_path(main_db: Path | str) -> Path:
    path = Path(main_db)
    return path.with_name(path.stem + "_strategy_trials.sqlite3")


def number(value: object) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (ValueError, TypeError):
        return 0.0


def age_seconds(now: datetime, value: object) -> float:
    parsed = parse_datetime(str(value or ""))
    if parsed is None or parsed.tzinfo is None:
        return math.inf
    return (now - parsed).total_seconds()


def encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


class StrategyTrials:
    """Forward-only signal comparison, isolated from broker orders and accounting."""

    def __init__(self, db_path: Path | str, *, config: dict | None = None) -> None:
        self.db_path = Path(db_path)
        self.config = config if config is not None else json.loads(CONFIG_PATH.read_text())
        self.implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        source_root = Path(__file__).parent
        feed_sources = ["liquidity_lab.py", "technical_signals.py", "momentum_policy.py"]
        feed_sources += [str(p.relative_to(source_root))
                         for p in sorted((source_root / "strategy").glob("*.py"))]
        self.feed_hash = hashlib.sha256(b"".join(
            name.encode() + b"\0" + (source_root / name).read_bytes()
            for name in feed_sources
        )).hexdigest()
        if self.config.get("schema_version") != 1:
            raise ValueError("unsupported strategy trial configuration")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, market TEXT NOT NULL, created_at TEXT NOT NULL,
                    spec_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    decisions_json TEXT NOT NULL, UNIQUE(run_id, symbol, observed_at)
                );
                CREATE INDEX IF NOT EXISTS observations_recent
                    ON observations(run_id, symbol, id DESC);
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, arm TEXT NOT NULL,
                    session_date TEXT NOT NULL, symbol TEXT NOT NULL, exchange_code TEXT,
                    signal_at TEXT NOT NULL, signal_observation_id INTEGER NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    terms_json TEXT NOT NULL, entry_at TEXT, entry_price REAL, qty INTEGER,
                    last_at TEXT, last_bid REAL, exit_at TEXT, exit_price REAL, net_pnl REAL,
                    hold_minutes REAL, exit_observation_id INTEGER,
                    UNIQUE(run_id, arm, session_date, symbol)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS trades_one_slot
                    ON trades(run_id, arm) WHERE status IN ('PENDING', 'OPEN');
                CREATE TABLE IF NOT EXISTS reports (
                    report_key TEXT PRIMARY KEY, sent_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_context (
                    run_id TEXT NOT NULL, session_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL, regime_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, session_date)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    run_id TEXT NOT NULL, arm TEXT NOT NULL, evaluated_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL, PRIMARY KEY(run_id, arm, evaluated_at)
                );
            """)
        self.db_path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def register(self, market: str, *, policy_id: str, policy_fingerprint: str,
                 commission: float, sell_tax: float, sec_fee: float, now: datetime,
                 breakers_disabled: bool = False) -> str:
        spec = {
            "experiment_id": self.config["experiment_id"],
            "source_job_id": self.config["source_job_id"],
            "engine": ENGINE_VERSION, "arms": ARMS, "market": market,
            "implementation_sha256": self.implementation_hash,
            "feed_implementation_sha256": self.feed_hash,
            "paper_circuit_breakers_disabled": breakers_disabled,
            "policy_id": policy_id, "policy_fingerprint": policy_fingerprint,
            "commission": commission, "sell_tax": sell_tax, "sec_fee": sec_fee,
            "settings": self.config["markets"][market],
        }
        raw = encode(spec)
        run_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)",
                       (run_id, market, now.isoformat(), raw))
        return run_id

    def open_symbols(self, market: str) -> dict[str, str]:
        with closing(self.connect()) as db:
            return {r["symbol"]: r["exchange_code"] or "" for r in db.execute(
                "SELECT symbol, exchange_code FROM trades JOIN runs ON runs.id=trades.run_id "
                "WHERE market=? AND status IN ('PENDING','OPEN')", (market,))}

    @staticmethod
    def execution_sample(sample: dict, cfg: dict) -> dict:
        result = dict(sample)
        result["quote_model"] = "observed_bid_ask"
        half = number(cfg.get("missing_quote_half_spread_bps")) / 10000
        if sample.get("bid") == 0 and sample.get("ask") == 0 and 0 < half <= .005:
            price = number(sample.get("price"))
            if price > 0:
                result.update(bid=price * (1 - half), ask=price * (1 + half),
                              raw_bid=0, raw_ask=0, quote_model="last_trade_proxy",
                              assumed_half_spread_bps=half * 10000)
        return result

    @staticmethod
    def _quote_ok(sample: dict, now: datetime, cfg: dict) -> bool:
        bid, ask = number(sample.get("bid")), number(sample.get("ask"))
        return (0 < bid <= ask and
                0 <= age_seconds(now, sample.get("quote_at")) <= cfg["max_quote_age_sec"])

    @staticmethod
    def _signal(arm: str, sample: dict, previous: dict | None, cfg: dict,
                now: datetime, session: str, remaining: float | None) -> str:
        if remaining is None or remaining < cfg["min_close_minutes"]:
            return "near_close"
        if not sample.get("eligible", False):
            return "instrument_or_risk_block"
        if not StrategyTrials._quote_ok(sample, now, cfg):
            return "quote_missing_or_stale"
        bid, ask = number(sample["bid"]), number(sample["ask"])
        if (ask / bid - 1) > cfg["max_spread_pct"]:
            return "spread"
        if not (0 <= age_seconds(now, sample.get("signal_at")) <= cfg["max_quote_age_sec"]):
            return "signal_stale"
        snapshot = sample.get("snapshot") or {}
        price, atr = number(sample.get("price")), number(snapshot.get("atr"))
        if price <= 0 or not 0 < atr < price * .2:
            return "atr_missing"
        if arm == ARMS[0]:
            return "signal" if sample.get("baseline_buy") else "baseline_wait"
        if arm in ARMS[3:]:
            return "signal" if sample.get("component_signals", {}).get(arm) else "component_wait"
        regime = sample.get("regime") or {}
        if (not regime.get("available") or regime.get("session_date") != session or
                regime.get("is_final") or
                not 0 <= age_seconds(now, regime.get("captured_at")) <= 900 or
                regime.get("return_pct") is None):
            return "regime_missing_or_stale"
        if (not previous or previous.get("session_date") != session or
                not 20 <= age_seconds(now, previous.get("observed_at")) <= cfg["max_gap_sec"]):
            return "confirmation_missing"
        prev = previous.get("snapshot") or {}
        prior_price = number(previous.get("price"))
        if price <= prior_price:
            return "recovery_unconfirmed"
        change = number(regime["return_pct"])
        rsi, volume = number(snapshot.get("rsi14")), number(snapshot.get("volume_ratio"))
        if arm == ARMS[1]:
            if abs(change) > cfg["range_benchmark_abs_pct"]:
                return "not_range_regime"
            vwap, prior_vwap = number(snapshot.get("vwap")), number(prev.get("vwap"))
            if (number(prev.get("atr")) <= 0 or
                    prior_vwap - prior_price < cfg["reversion_distance_atr"] * number(prev.get("atr"))):
                return "no_prior_dislocation"
            if not (price < vwap and 30 <= rsi <= 55 and volume >= cfg["reversion_min_volume_ratio"]):
                return "reversion_quality"
        else:
            if change < cfg["trend_benchmark_min_pct"]:
                return "not_up_regime"
            fast, slow = number(snapshot.get("minute_ma_fast")), number(snapshot.get("minute_ma_slow"))
            if not (0 < slow < fast < price and
                    0 < number(snapshot.get("daily_ma_slow")) < number(snapshot.get("daily_ma_fast")) and
                    0 < prior_price <= number(prev.get("minute_ma_fast")) and
                    45 <= rsi <= 70 and volume >= cfg["trend_min_volume_ratio"]):
                return "no_trend_pullback"
        return "signal"

    @staticmethod
    def _entry_check(sample: dict, terms: dict) -> tuple[float, int]:
        cfg = terms["settings"]
        entry = number(sample["ask"]) * (1 + cfg["slippage_bps"] / 10000)
        risk = entry - terms["stop"]
        reward = terms["target"] - entry
        cost = entry * terms["commission"] + terms["target"] * terms["sell_cost"]
        qty = int(terms["capital"] / (entry * (1 + terms["commission"])))
        spread = number(sample["ask"]) / number(sample["bid"]) - 1
        if (not sample.get("eligible") or qty < 1 or risk <= 0 or reward <= 0 or
                spread > cfg["max_spread_pct"] or
                (cfg.get("enforce_expected_reward_risk", True)
                 and reward - cost < risk * cfg["min_net_reward_risk"])):
            return entry, 0
        return entry, qty

    @staticmethod
    def _close_trade(db, trade, sample: dict, now: datetime, reason: str,
                     status: str, observation_id: int | None) -> None:
        terms = json.loads(trade["terms_json"])
        bid = number(sample["bid"]) * (1 - terms["settings"]["slippage_bps"] / 10000)
        net = ((bid - trade["entry_price"]) - trade["entry_price"] * terms["commission"]
               - bid * terms["sell_cost"]) * trade["qty"]
        terms["exit_quote_model"] = sample["quote_model"]
        stamp = now.astimezone(timezone.utc).isoformat()
        db.execute("UPDATE trades SET status=?,reason=?,exit_at=?,exit_price=?,net_pnl=?,hold_minutes=?,"
                   "exit_observation_id=?,last_at=?,last_bid=?,terms_json=? WHERE id=?",
                   (status, reason, stamp, bid, net, age_seconds(now, trade["entry_at"]) / 60,
                    observation_id, stamp, bid, encode(terms), trade["id"]))

    def observe(self, run_id: str, *, session: str, now: datetime, samples: list[dict],
                regular_open: bool, remaining: float | None,
                regime: dict | None = None) -> dict[str, int]:
        counts: Counter = Counter()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("unknown trial run")
            spec = json.loads(run["spec_json"])
            cfg = spec["settings"]
            stamp = now.astimezone(timezone.utc).isoformat()
            if (regime and regime.get("available") and regime.get("session_date") == session
                    and 0 <= age_seconds(now, regime.get("captured_at")) <= 86400):
                db.execute(
                    "INSERT INTO session_context VALUES (?,?,?,?) ON CONFLICT(run_id,session_date) "
                    "DO UPDATE SET captured_at=excluded.captured_at,regime_json=excluded.regime_json "
                    "WHERE excluded.captured_at>=session_context.captured_at",
                    (run_id, session, regime["captured_at"], encode(regime)))
            raw_by_symbol = {s["symbol"]: s for s in samples}
            by_symbol = {symbol: self.execution_sample(s, cfg) for symbol, s in raw_by_symbol.items()}
            observation_ids: dict[str, int] = {}
            decisions_by_symbol: dict[str, dict] = {}
            for symbol, sample in sorted(by_symbol.items()) if regular_open else []:
                prior = db.execute(
                    "SELECT * FROM observations WHERE run_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
                    (run_id, symbol)).fetchone()
                if prior and age_seconds(now, prior["observed_at"]) <= 0:
                    continue
                previous = json.loads(prior["payload_json"]) if prior else None
                decisions = {arm: self._signal(arm, sample, previous, cfg, now, session, remaining)
                             if regular_open else "session_closed" for arm in ARMS}
                payload = {**sample, "observed_at": stamp, "session_date": session}
                cursor = db.execute(
                    "INSERT INTO observations(run_id,session_date,symbol,observed_at,payload_json,decisions_json) "
                    "VALUES (?,?,?,?,?,?)", (run_id, session, symbol, stamp, encode(payload), encode(decisions)))
                observation_ids[symbol] = cursor.lastrowid
                decisions_by_symbol[symbol] = decisions
                counts["observations"] += 1

            # Older versions drain using their immutable terms; no version change can erase exposure.
            active = db.execute(
                "SELECT trades.* FROM trades JOIN runs ON runs.id=trades.run_id "
                "WHERE runs.market=? AND status IN ('PENDING','OPEN')", (run["market"],)).fetchall()
            for trade in active:
                terms = json.loads(trade["terms_json"])
                raw_sample = raw_by_symbol.get(trade["symbol"])
                sample = self.execution_sample(raw_sample, terms["settings"]) if raw_sample else None
                since = age_seconds(now, trade["last_at"] or trade["signal_at"])
                if since <= 0:
                    continue
                valid = sample and self._quote_ok(sample, now, terms["settings"])
                reason = ""
                if trade["session_date"] != session or not regular_open:
                    reason = "session_end_without_executable_quote"
                elif since > terms["settings"]["max_gap_sec"]:
                    reason = "observation_gap"
                if trade["reason"] in {"observation_gap", "session_end_without_executable_quote"}:
                    reason = trade["reason"]
                if reason:
                    if trade["status"] == "OPEN" and terms.get("breakers_disabled"):
                        if (regular_open and valid and age_seconds(now, sample.get("quote_at")) < since):
                            self._close_trade(db, trade, sample, now, reason, "INVALID", observation_ids.get(trade["symbol"]))
                            counts["invalid"] += 1
                        else:
                            # Exposure remains occupied until a fresh, regular-session liquidation observation.
                            db.execute("UPDATE trades SET reason=? WHERE id=?", (reason, trade["id"]))
                            counts["data_wait"] += 1
                        continue
                    db.execute("UPDATE trades SET status=?,reason=?,exit_at=? WHERE id=?",
                               ("INVALID" if trade["status"] == "OPEN" else "EXPIRED", reason, stamp, trade["id"]))
                    counts["invalid"] += 1
                    continue
                if not valid or age_seconds(now, sample.get("quote_at")) >= since:
                    continue
                slip = terms["settings"]["slippage_bps"] / 10000
                bid = number(sample["bid"]) * (1 - slip)
                if trade["status"] == "PENDING":
                    if remaining is None or remaining < terms["settings"]["min_close_minutes"]:
                        db.execute("UPDATE trades SET status='EXPIRED',reason='near_close' WHERE id=?", (trade["id"],))
                        continue
                    entry, qty = self._entry_check(sample, terms)
                    if qty < 1:
                        db.execute("UPDATE trades SET status='EXPIRED',reason='fill_cost_or_risk' WHERE id=?", (trade["id"],))
                        counts["expired"] += 1
                        continue
                    terms["entry_quote_model"] = sample["quote_model"]
                    db.execute("UPDATE trades SET status='OPEN',entry_at=?,entry_price=?,qty=?,last_at=?,last_bid=? WHERE id=?",
                               (stamp, entry, qty, stamp, bid, trade["id"]))
                    db.execute("UPDATE trades SET terms_json=? WHERE id=?", (encode(terms), trade["id"]))
                    counts["opened"] += 1
                    continue
                held = age_seconds(now, trade["entry_at"]) / 60
                if bid <= terms["stop"]:
                    reason = "stop"
                elif bid >= terms["target"]:
                    reason = "target"
                elif held >= terms["settings"]["max_hold_minutes"]:
                    reason = "time"
                elif remaining is not None and remaining <= 5:
                    reason = "session_close"
                if reason:
                    self._close_trade(db, trade, sample, now, reason, "CLOSED", observation_ids.get(trade["symbol"]))
                    counts["closed"] += 1
                else:
                    db.execute("UPDATE trades SET last_at=?,last_bid=? WHERE id=?", (stamp, bid, trade["id"]))

            if regular_open:
                for arm in ARMS:
                    # Unknown exposure is never recycled as cash; a measured invalid exit can release it.
                    blocked = db.execute(
                        "SELECT 1 FROM trades JOIN runs ON runs.id=trades.run_id WHERE runs.market=? AND arm=? "
                        "AND (status IN ('PENDING','OPEN') OR (status='INVALID' AND session_date=? "
                        "AND (?=0 OR net_pnl IS NULL))) LIMIT 1",
                        (run["market"], arm, session, int(spec.get("paper_circuit_breakers_disabled", False)))).fetchone()
                    if blocked:
                        continue
                    for symbol in sorted(decisions_by_symbol):
                        if decisions_by_symbol[symbol][arm] != "signal":
                            continue
                        sample = by_symbol[symbol]
                        price = number(sample["price"])
                        atr = number(sample["snapshot"]["atr"])
                        stop, target = price - cfg["stop_atr"] * atr, price + cfg["target_atr"] * atr
                        if arm == ARMS[1]:
                            target = min(target, number(sample["snapshot"].get("vwap")))
                        sell_cost = spec["commission"] + spec["sec_fee"] + (0 if sample.get("tax_exempt") else spec["sell_tax"])
                        realized = db.execute(
                            "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE run_id=? AND arm=? AND status IN ('CLOSED','INVALID')",
                            (run_id, arm)).fetchone()[0]
                        capital = min(cfg["capital"], max(0, cfg["capital"] + realized))
                        terms = {"settings": cfg, "stop": stop, "target": target, "capital": capital,
                                 "breakers_disabled": spec.get("paper_circuit_breakers_disabled", False),
                                 "commission": spec["commission"], "sell_cost": sell_cost,
                                 "regime": sample.get("regime"), "source": ENGINE_VERSION}
                        if self._entry_check(sample, terms)[1] < 1:
                            decisions_by_symbol[symbol][arm] = "cost_reward_or_capital"
                            continue
                        cursor = db.execute(
                            "INSERT OR IGNORE INTO trades(run_id,arm,session_date,symbol,exchange_code,signal_at,"
                            "signal_observation_id,status,terms_json) VALUES (?,?,?,?,?,?,?,'PENDING',?)",
                            (run_id, arm, session, symbol, sample.get("exchange_code", ""), stamp,
                             observation_ids[symbol], encode(terms)))
                        if cursor.rowcount:
                            counts["signals"] += 1
                            break
            for symbol, decisions in decisions_by_symbol.items():
                db.execute("UPDATE observations SET decisions_json=? WHERE id=?",
                           (encode(decisions), observation_ids[symbol]))
        return dict(counts)

    def report(self, market: str | None = None) -> str:
        lines = ["[전략 비교 모의군] 브로커 주문 아님 / 현금 기준=0",
                 "동일 후보·각1슬롯·비용차감 / 계좌보유와 독립 진입신호(청산식 공통)"]
        with closing(self.connect()) as db, db:
            for market_key in ([market] if market else ["domestic", "overseas"]):
                run = db.execute("SELECT * FROM runs WHERE market=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (market_key,)).fetchone()
                if not run:
                    lines.append(f"{market_key}: 초기화 대기")
                    continue
                spec = json.loads(run["spec_json"])
                cfg = spec["settings"]
                observed = db.execute("SELECT COUNT(*),COUNT(DISTINCT session_date),MAX(observed_at) FROM observations WHERE run_id=?", (run["id"],)).fetchone()
                lines.append(f"{market_key} {cfg['version']} #{run['id'][:8]} {cfg['capital']}{cfg['currency']}")
                lines.append(f"관측={observed[0]} 세션={observed[1]} 최근={observed[2] or '-'}")
                quote_assumption = (f"현재가±{cfg['missing_quote_half_spread_bps']}bp 가정(실제호가 아님)"
                                    if cfg.get("missing_quote_half_spread_bps") else "진입보류")
                lines.append(f"손실중단={'해제' if spec.get('paper_circuit_breakers_disabled') else '기존설정'} "
                             f"호가없음={quote_assumption}")
                older = db.execute(
                    "SELECT COUNT(*) FROM trades JOIN runs ON runs.id=trades.run_id "
                    "WHERE market=? AND runs.id!=? AND status IN ('OPEN','PENDING')", (market_key, run["id"])).fetchone()[0]
                if older:
                    lines.append(f"이전 버전 진행중={older} (구버전 조건으로 청산 감시)")
                context = db.execute("SELECT * FROM session_context WHERE run_id=? ORDER BY session_date DESC LIMIT 1", (run["id"],)).fetchone()
                if context:
                    regime = json.loads(context["regime_json"])
                    lines.append(f"시장 {context['session_date']} {'마감' if regime.get('is_final') else '장중'} "
                                 f"{regime.get('benchmark_code', '')} 등락={regime.get('return_pct')}% "
                                 f"거래량20일비={regime.get('volume_ratio_20')} {regime.get('regime_key', '')}")
                for arm in spec["arms"]:
                    rows = db.execute("SELECT * FROM trades WHERE run_id=? AND arm=? ORDER BY id", (run["id"], arm)).fetchall()
                    closed = [r for r in rows if r["status"] == "CLOSED"]
                    states = Counter(r["status"] for r in rows)
                    net = sum(r["net_pnl"] for r in closed)
                    stress_net = net
                    proxy_count = 0
                    for row in closed:
                        terms = json.loads(row["terms_json"])
                        fees = (row["entry_price"] * terms["commission"] + row["exit_price"] * terms["sell_cost"]) * row["qty"]
                        slip = cfg["slippage_bps"] / 10000 * (row["entry_price"] + row["exit_price"]) * row["qty"]
                        proxy = any(terms.get(key) == "last_trade_proxy" for key in ("entry_quote_model", "exit_quote_model"))
                        proxy_count += proxy
                        spread_stress = (row["entry_price"] + row["exit_price"]) * row["qty"] * cfg.get("missing_quote_half_spread_bps", 0) / 10000 if proxy else 0
                        stress_net -= fees + slip + spread_stress
                    equity, peak, drawdown = cfg["capital"], cfg["capital"], 0.0
                    for row in closed:
                        equity += row["net_pnl"]
                        peak = max(peak, equity)
                        drawdown = max(drawdown, peak - equity)
                    unrealized = 0.0
                    for row in rows:
                        if row["status"] == "OPEN":
                            terms = json.loads(row["terms_json"])
                            unrealized += (row["last_bid"] * (1 - terms["sell_cost"]) - row["entry_price"] * (1 + terms["commission"])) * row["qty"]
                    sessions = len({r["session_date"] for r in closed})
                    enough = sessions >= cfg["min_sessions"] and len(closed) >= cfg["min_closed_trades"]
                    baseline_net = db.execute("SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE run_id=? AND arm=? AND status='CLOSED'",
                                              (run["id"], ARMS[0])).fetchone()[0]
                    candidate = enough and not states["INVALID"] and net > max(0, baseline_net) and stress_net > 0
                    verdict = ("개선검토후보·자동승격없음" if candidate else
                               "열위/미검증·승격금지" if enough else "표본/관측부족·승격금지")
                    invalid_net = sum(r["net_pnl"] or 0 for r in rows if r["status"] == "INVALID")
                    evaluation = {"status": verdict, "closed": len(closed), "closed_sessions": sessions,
                                  "net_pnl": net, "stress_net_pnl": stress_net, "baseline_net_pnl": baseline_net,
                                  "invalid": states["INVALID"], "invalid_liquidation_pnl": invalid_net,
                                  "proxy_trades": proxy_count, "currency": cfg["currency"],
                                  "capital": cfg["capital"], "sample_threshold_met": enough,
                                  "candidate_for_review": candidate, "automatic_promotion": False,
                                  "selection_bias": "current_scanner_universe", "observation_count": observed[0]}
                    db.execute("INSERT INTO evaluations VALUES (?,?,?,?)",
                               (run["id"], arm, datetime.now(timezone.utc).isoformat(), encode(evaluation)))
                    lines.append(f"{arm}: 신호{len(rows)} 완료{len(closed)} 대기{states['PENDING']} 보유{states['OPEN']} 만료{states['EXPIRED']} 누락{states['INVALID']}")
                    lines.append(f"  Net={net:+.2f} 미실현추정={unrealized:+.2f} {cfg['currency']} 승={sum(r['net_pnl'] > 0 for r in closed)}/{len(closed)} {verdict}")
                    if states["INVALID"]:
                        lines.append(f"  관측누락 정리손익={invalid_net:+.2f} (유효표본 아님, 자본에 반영)")
                    if closed:
                        mean_hold = sum(r["hold_minutes"] for r in closed) / len(closed)
                        lines.append(f"  자본수익={net / cfg['capital'] * 100:+.3f}% 청산기준MDD={drawdown:.2f} 평균보유={mean_hold:.1f}분")
                        lines.append(f"  비용2배가정Net={stress_net:+.2f} 현재가대용={proxy_count}/{len(closed)}")
                    regimes: dict[str, list[float]] = {}
                    for row in closed:
                        key = (json.loads(row["terms_json"]).get("regime") or {}).get("regime_key", "unknown")
                        regimes.setdefault(key, []).append(row["net_pnl"])
                    for key, values in sorted(regimes.items(), key=lambda item: (-len(item[1]), item[0]))[:3]:
                        lines.append(f"  진입환경 {key}: n={len(values)} Net={sum(values):+.2f}")
                    if len(regimes) > 3:
                        lines.append(f"  기타 {len(regimes) - 3}개 환경은 실험 DB에 보존")
                reasons: Counter = Counter()
                for row in db.execute("SELECT decisions_json FROM observations WHERE run_id=? ORDER BY id DESC LIMIT 300", (run["id"],)):
                    reasons.update(v for v in json.loads(row[0]).values() if v != "signal")
                lines.append("최근 대기=" + ", ".join(f"{k}:{v}" for k, v in reasons.most_common(3)))
        lines.append("관측형 시뮬레이션: 장중 경로/호가잔량 미확인, 실제 수익성 검증 아님")
        result = "\n".join(lines)
        if len(result) > 3800:
            result = result[:3650].rsplit("\n", 1)[0] + "\n전체 시장별 조회: /lab_report trials KR 또는 US"
        return result

    def report_due(self, key: str) -> bool:
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM reports WHERE report_key=?", (key,)).fetchone() is None

    def has_session_observations(self, run_id: str, session: str) -> bool:
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM observations WHERE run_id=? AND session_date=? LIMIT 1",
                              (run_id, session)).fetchone() is not None

    def mark_report_sent(self, key: str, now: datetime) -> None:
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO reports VALUES (?,?)", (key, now.isoformat()))
