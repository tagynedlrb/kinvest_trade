from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from kinvest_trade.strategy_trials import ARMS, CONFIG_PATH, StrategyTrials, trial_db_path


NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
SESSION = "2026-10-02"


@pytest.fixture
def trials(tmp_path):
    return StrategyTrials(tmp_path / "trials.sqlite3")


def register(trials, market="overseas", fingerprint="baseline", commission=.00015):
    return trials.register(market, policy_id="test_v1", policy_fingerprint=fingerprint,
                           commission=commission, sell_tax=.002 if market == "domestic" else 0,
                           sec_fee=.0000206 if market == "overseas" else 0, now=NOW)


def sample(now=NOW, price=100.0, **kwargs):
    data = {
        "symbol": "TEST", "exchange_code": "NASD", "price": price,
        "bid": price - .01, "ask": price + .01,
        "quote_at": now.isoformat(), "signal_at": now.isoformat(),
        "eligible": True, "baseline_buy": True, "tax_exempt": True,
        "snapshot": {"atr": 1.0, "vwap": 103.0, "rsi14": 45, "volume_ratio": 1.3,
                     "minute_ma_fast": 100.5, "minute_ma_slow": 99.5,
                     "daily_ma_fast": 100, "daily_ma_slow": 99},
        "regime": {"available": True, "session_date": SESSION, "captured_at": now.isoformat(),
                   "return_pct": .1, "regime_key": "sideways|normal|normal", "is_final": 0},
    }
    data.update(kwargs)
    return data


def observe(trials, run, minute=0, rows=None, **kwargs):
    now = NOW + timedelta(minutes=minute)
    return trials.observe(run, session=kwargs.pop("session", SESSION), now=now,
                          samples=rows if rows is not None else [sample(now)],
                          regular_open=kwargs.pop("regular_open", True),
                          remaining=kwargs.pop("remaining", 120), **kwargs)


def trades(trials):
    with trials.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY id")]


def test_next_observation_fill_costs_and_restart(trials):
    run = register(trials)
    assert observe(trials, run)["signals"] == 1
    assert trades(trials)[0]["status"] == "PENDING"
    assert observe(trials, run) == {}
    restarted = StrategyTrials(trials.db_path)
    assert observe(restarted, run, 1)["opened"] == 1
    entry = trades(restarted)[0]
    assert entry["entry_price"] == pytest.approx(100.01 * 1.0005)
    assert entry["qty"] == 4
    assert observe(restarted, run, 2, [sample(NOW + timedelta(minutes=2), price=103)])["closed"] == 1
    row = trades(restarted)[0]
    exit_price = 102.99 * .9995
    expected = (exit_price - entry["entry_price"] - entry["entry_price"] * .00015 - exit_price * .0001706) * 4
    assert row["net_pnl"] == pytest.approx(expected)
    assert row["exit_observation_id"]
    assert row["hold_minutes"] == 1
    assert row["reason"] == "target"
    assert "승격금지" in restarted.report()


def test_same_quote_cannot_fill_pending(trials):
    run = register(trials)
    observe(trials, run)
    observe(trials, run, 1, [sample()])
    assert trades(trials)[0]["status"] == "PENDING"


def test_cost_guard_records_reason_not_fake_signal(trials):
    run = register(trials, commission=.02)
    observe(trials, run)
    assert not trades(trials)
    assert "cost_reward_or_capital" in trials.report()


@pytest.mark.parametrize("patch", [
    {"ask": 0}, {"bid": 102}, {"bid": 98}, {"eligible": False},
    {"signal_at": (NOW - timedelta(hours=1)).isoformat()},
    {"quote_at": (NOW + timedelta(minutes=1)).isoformat()},
    {"quote_at": (NOW - timedelta(minutes=5)).isoformat()},
    {"snapshot": {"atr": None}},
])
def test_bad_data_does_not_open(trials, patch):
    run = register(trials)
    observe(trials, run, rows=[sample(**patch)])
    assert not trades(trials)


def test_no_fill_outside_regular_session(trials):
    run = register(trials)
    observe(trials, run)
    observe(trials, run, 1, regular_open=False)
    assert trades(trials)[0]["status"] == "EXPIRED"


def test_gap_invalidates_does_not_fabricate_exit(trials):
    run = register(trials)
    observe(trials, run)
    observe(trials, run, 1)
    observe(trials, run, 8, [sample(NOW + timedelta(minutes=8), price=200)])
    row = trades(trials)[0]
    assert row["status"] == "INVALID"
    assert row["net_pnl"] is None
    observe(trials, run, 9, [sample(NOW + timedelta(minutes=9), symbol="OTHER")])
    assert len(trades(trials)) == 1
    assert "누락1" in trials.report()


def test_reversion_requires_prior_dislocation_and_later_recovery(trials):
    run = register(trials)
    observe(trials, run, rows=[sample(baseline_buy=False)])
    assert not trades(trials)
    observe(trials, run, 1, [sample(NOW + timedelta(minutes=1), price=100.1, baseline_buy=False)])
    assert trades(trials)[0]["arm"] == ARMS[1]
    assert trades(trials)[0]["status"] == "PENDING"


@pytest.mark.parametrize("patch", [
    {"is_final": 1}, {"session_date": "2026-10-01"}, {"available": False},
    {"captured_at": (NOW - timedelta(hours=1)).isoformat()},
    {"captured_at": (NOW + timedelta(hours=1)).isoformat()},
    {"return_pct": None}, {"return_pct": -1.5},
])
def test_no_final_future_stale_or_wrong_regime_lookahead(trials, patch):
    run = register(trials)
    observe(trials, run, rows=[sample(baseline_buy=False)])
    row = sample(NOW + timedelta(minutes=1), price=100.1, baseline_buy=False)
    row["regime"].update(patch)
    observe(trials, run, 1, [row])
    assert not trades(trials)


def test_trend_pullback_in_up_market(trials):
    run = register(trials)
    row = sample(baseline_buy=False)
    row["regime"]["return_pct"] = .8
    observe(trials, run, rows=[row])
    row = sample(NOW + timedelta(minutes=1), price=100.6, baseline_buy=False)
    row["regime"]["return_pct"] = .8
    observe(trials, run, 1, [row])
    assert [r["arm"] for r in trades(trials)] == [ARMS[2]]


def test_versions_markets_and_configs_are_independent(trials):
    run = register(trials)
    assert register(trials) == run
    assert register(trials, market="domestic") != run
    other = register(trials, fingerprint="changed")
    assert other != run
    trials.feed_hash = "changed_quote_or_signal_code"
    assert register(trials) not in {run, other}
    observe(trials, run)
    observe(trials, other, 1)
    assert len(trades(trials)) == 1
    assert trades(trials)[0]["run_id"] == run
    assert trades(trials)[0]["status"] == "OPEN"
    cfg = json.loads(CONFIG_PATH.read_text())
    original = copy.deepcopy(cfg["markets"]["overseas"])
    cfg["markets"]["domestic"]["stop_atr"] = 2
    assert cfg["markets"]["overseas"] == original


def test_slots_budget_and_new_session_reset(trials):
    run = register(trials)
    observe(trials, run, rows=[sample(symbol="ZZZ"), sample(symbol="AAA")])
    assert len(trades(trials)) == 1
    assert trades(trials)[0]["symbol"] == "AAA"
    observe(trials, run, 1, [sample(NOW + timedelta(minutes=1), symbol="AAA")])
    row = trades(trials)[0]
    assert row["qty"] * row["entry_price"] * 1.00015 <= 500
    observe(trials, run, 1440, session="2026-10-03")
    assert trades(trials)[0]["status"] == "INVALID"


def test_stop_fills_at_observed_bid_not_stop_level(trials):
    run = register(trials)
    observe(trials, run)
    observe(trials, run, 1)
    observe(trials, run, 2, [sample(NOW + timedelta(minutes=2), price=98)])
    row = trades(trials)[0]
    assert row["reason"] == "stop"
    assert row["exit_price"] == pytest.approx(97.99 * .9995)
    assert row["exit_price"] < json.loads(row["terms_json"])["stop"]


def test_near_close_and_report_delivery_dedup(trials):
    run = register(trials)
    observe(trials, run, remaining=60)
    assert not trades(trials)
    assert trials.report_due("one")
    trials.mark_report_sent("one", NOW)
    assert not StrategyTrials(trials.db_path).report_due("one")
    assert trial_db_path("data/bot.db").name == "bot_strategy_trials.sqlite3"


def test_loss_reduces_next_position_budget(trials):
    run = register(trials)
    observe(trials, run)
    observe(trials, run, 1)
    observe(trials, run, 2, [sample(NOW + timedelta(minutes=2), price=98)])
    observe(trials, run, 3, [sample(NOW + timedelta(minutes=3), symbol="OTHER")])
    row = trades(trials)[-1]
    assert json.loads(row["terms_json"])["capital"] < 500


def test_closed_market_not_counted_as_experiment_session(trials):
    run = register(trials)
    observe(trials, run, regular_open=False, regime=sample()["regime"])
    assert not trials.has_session_observations(run, SESSION)
    assert "관측=0 세션=0" in trials.report()


def test_final_context_is_separate_from_entry_context(trials):
    run = register(trials)
    entry_regime = sample()["regime"]
    observe(trials, run, regime=entry_regime)
    final = {**entry_regime, "is_final": 1, "return_pct": -1.5,
             "captured_at": (NOW + timedelta(hours=6)).isoformat()}
    observe(trials, run, 360, rows=[], regular_open=False, regime=final)
    assert json.loads(trades(trials)[0]["terms_json"])["regime"]["return_pct"] == .1
    assert "마감" in trials.report()
    assert "-1.5%" in trials.report()


def test_telegram_report_route(trials):
    from types import SimpleNamespace
    from kinvest_trade.telegram_reports import ReportHelper

    main_db = trials.db_path.parent / "main.db"
    engine = StrategyTrials(trial_db_path(main_db))
    register(engine)
    helper = ReportHelper(SimpleNamespace(repository=SimpleNamespace(db_path=main_db)))
    assert "us_snapshot_trials_v1" in helper.build_report_message("trials US")
    assert "domestic: 초기화 대기" not in helper.build_report_message("trials US")
    assert "사용법" in helper.build_report_message("trials unknown")


def test_service_observer_does_not_submit_broker_orders(trials, monkeypatch):
    import asyncio
    from dataclasses import make_dataclass
    from types import SimpleNamespace
    import kinvest_trade.liquidity_lab as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    class Notifier:
        messages = []

        async def send(self, message):
            self.messages.append(message)
            return True

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "get_us_trading_session", lambda now: "regular")
    monkeypatch.setattr(module, "minutes_until_regular_session_close", lambda market, now: 120)
    service = module.LiquidityLabService.__new__(module.LiquidityLabService)
    service.strategy_trials = trials
    service.notifier = Notifier()
    service.client = object()  # No order API, or indeed any API, is available here.
    service._trial_quote_times = {("overseas", "TEST"): NOW}
    service._signal_cache_updated_at = {"TEST": NOW}
    service._get_market_policy = lambda market: SimpleNamespace(
        policy_id="baseline_v1", parameter_fingerprint="hash",
        auto_trade=SimpleNamespace(domestic_commission_rate=.00015, overseas_commission_rate=.00015,
                                   domestic_sell_tax_rate=.002, sec_fee_rate=.0000206))
    service._market_regime_context = lambda *args, **kwargs: sample()["regime"]
    service._market_session_date = lambda *args: SESSION
    service._is_trading_halted = lambda *args: False
    service._is_order_reject_halted = lambda **kwargs: False
    service._is_inverse_symbol = lambda *args: False
    service._is_leveraged_symbol = lambda *args: False
    events = []
    service._save_event = lambda **kwargs: events.append(kwargs)
    snapshot = make_dataclass("Snapshot", [(k, float) for k in sample()["snapshot"]])(**sample()["snapshot"])
    target = SimpleNamespace(market="overseas", code="TEST", signal_snapshot=snapshot,
                             action_bias="BUY", decision_reason="", note="baseline")
    quote = SimpleNamespace(symbol="TEST", exchange_code="NASD", last_price=100, bid=99.99, ask=100.01)
    report = SimpleNamespace(krx_market_open=False, us_market_session="regular", us_orderable_in_profile=True,
                             domestic_ranked=[], overseas_ranked=[quote], watch_targets=[target])
    asyncio.run(service._observe_strategy_trials(report))
    assert len(trades(trials)) == 1
    assert trades(trials)[0]["status"] == "PENDING"
    assert events[0]["event_type"] == "strategy_trial_progress"
    assert len(service.notifier.messages) == 1
    asyncio.run(service._observe_strategy_trials(report))
    assert len(trades(trials)) == 1
    assert len(service.notifier.messages) == 1


@pytest.mark.parametrize("market", ["domestic", "overseas"])
def test_trial_maintenance_quote_does_not_mutate_trading_caches(market):
    import asyncio
    from types import SimpleNamespace
    from kinvest_trade.liquidity_lab import LiquidityLabService

    class Client:
        async def get_current_price(self, *args):
            return {"current_price": "100", "product_type": "ETF"}

        async def get_orderbook(self, *args):
            return {"best_bid": "99", "best_ask": "101"}

        async def get_overseas_price(self, *args):
            return {"last_price": "100.01", "bid": "99.99", "ask": "100.02"}

    service = LiquidityLabService.__new__(LiquidityLabService)
    service.client = Client()
    service.config = SimpleNamespace(trading=SimpleNamespace(market_code="J"))
    service._trial_quote_times = {}
    service._domestic_quote_cache = {}
    service._vol_history = {}
    result = asyncio.run(service._fetch_strategy_trial_quote(market, "TEST", "NASD"))
    assert (market, "TEST") in service._trial_quote_times
    if market == "domestic":
        assert result.best_bid == 99
    else:
        assert result.last_price == 100.01
        assert result.bid == 99.99
        assert result.ask == 100.02
    assert not service._domestic_quote_cache
    assert not service._vol_history
