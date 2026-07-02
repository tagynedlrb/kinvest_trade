import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from kinvest_trade.liquidity_lab import LiquidityLabService, OverseasScanResult
from kinvest_trade.technical_signals import MovingAverageSnapshot


@dataclass
class DummyCandidate:
    symbol: str
    exchange_code: str


@dataclass
class DummyWatchTarget:
    code: str
    signal_snapshot: MovingAverageSnapshot | None


class DummyRepository:
    def __init__(self) -> None:
        self.heartbeats: list[tuple[str, str]] = []

    def save_heartbeat(self, status: str, message: str) -> None:
        self.heartbeats.append((status, message))


class DummyClient:
    def __init__(self) -> None:
        self.balance_calls: list[tuple[str, str]] = []
        self.positions_by_exchange: dict[str, list[dict[str, str]]] = {}
        self.raise_balance_error = False

    async def get_overseas_balance(self, exchange_code: str, currency_code: str):
        self.balance_calls.append((exchange_code, currency_code))
        if self.raise_balance_error:
            raise RuntimeError("balance lookup failed")
        return {"positions": list(self.positions_by_exchange.get(exchange_code, []))}


def _build_service() -> LiquidityLabService:
    service = LiquidityLabService.__new__(LiquidityLabService)
    candidates = [
        DummyCandidate("AAA", "NASD"),
        DummyCandidate("BBB", "NASD"),
        DummyCandidate("CCC", "NASD"),
        DummyCandidate("DDD", "NYSE"),
        DummyCandidate("EEE", "NYSE"),
        DummyCandidate("FFF", "NYSE"),
    ]
    service.config = SimpleNamespace(
        liquidity_lab=SimpleNamespace(
            overseas_candidates=candidates,
            unified_watch_top_n=15,
            unified_scan_top_n=2,
            overseas_scan_top_n=2,
            overseas_min_price_usd=10.0,
            overseas_min_volume=100,
            overseas_max_spread_pct=0.01,
            overseas_stop_loss_pct=0.008,
            overseas_take_profit_pct=0.012,
        )
    )
    service.repository = DummyRepository()
    service.notifier = None
    service.client = DummyClient()
    service._domestic_excluded = []
    service._overseas_excluded = []
    service._last_held_symbols = set()
    service._signal_cache = {}
    service._wait_cycles = {}
    return service


def _result(symbol: str, score: float, volume: int = 1000) -> OverseasScanResult:
    return OverseasScanResult(
        symbol=symbol,
        exchange_code="NASD" if symbol in {"AAA", "BBB", "CCC"} else "NYSE",
        last_price=20.0,
        bid=19.99,
        ask=20.01,
        spread_pct=0.001,
        change_rate_pct=1.0,
        volume=volume,
        orderable_qty=0,
        fx_rate_krw=0.0,
        activity_score=score,
    )


def _snapshot(price: float = 20.0, regime: str = "trend_up") -> MovingAverageSnapshot:
    return MovingAverageSnapshot(
        price=price,
        spread_pct=0.001,
        daily_ma_fast=19.5,
        daily_ma_slow=19.0,
        minute_ma_fast=20.1,
        minute_ma_slow=19.8,
        prev_minute_ma_fast=19.9,
        prev_minute_ma_slow=19.7,
        rsi14=58.0,
        intraday_volatility=0.01,
        intraday_momentum=0.004,
        intraday_bar_return=0.002,
        volume_last=2000.0,
        volume_avg=1000.0,
        volume_ratio=2.0,
        breakout_level=19.9,
        breakdown_level=19.2,
        breakout_distance_pct=0.002,
        atr=0.4,
        atr_pct=0.02,
        bollinger_basis=19.8,
        bollinger_upper=20.3,
        bollinger_lower=19.3,
        daily_gap_fast_pct=0.025,
        daily_gap_slow_pct=0.05,
        minute_gap_slow_pct=0.01,
        fast_above_slow=True,
        crossed_up=False,
        crossed_down=False,
        regime=regime,
    )


def _install_watch_builder_stub(service: LiquidityLabService) -> None:
    def fake_build_watch_target_status(**kwargs):
        return DummyWatchTarget(
            code=kwargs["code"],
            signal_snapshot=kwargs["signal_snapshot"],
        )

    service._build_watch_target_status = fake_build_watch_target_status


def test_scan_overseas_returns_all_passing_candidates() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        volume = 10 if candidate.symbol == "FFF" else 1000
        return _result(candidate.symbol, score_map[candidate.symbol], volume=volume)

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    ranked, held_symbols = asyncio.run(service.scan_overseas())

    assert [item.symbol for item in ranked] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert held_symbols == set()


def test_signal_cache_populated_after_scan() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    ranked, held_symbols = asyncio.run(service.scan_overseas())

    assert [item.symbol for item in ranked[:2]] == ["AAA", "BBB"]
    assert held_symbols == set()
    assert set(service._signal_cache.keys()) == {"AAA", "BBB"}


def test_signal_cache_cleared_for_non_signal_symbols() -> None:
    service = _build_service()
    service._signal_cache = {
        "CCC": _snapshot(18.0),
        "ZZZ": _snapshot(17.0),
    }
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    asyncio.run(service.scan_overseas())

    assert set(service._signal_cache.keys()) == {"AAA", "BBB"}


def test_scan_overseas_excluded_not_in_results() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        volume = 10 if candidate.symbol == "AAA" else 1000
        return _result(candidate.symbol, score_map[candidate.symbol], volume=volume)

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    ranked, _ = asyncio.run(service.scan_overseas())

    assert "AAA" not in [candidate.symbol for candidate in ranked]
    assert service._overseas_excluded[0].code == "AAA"


def test_held_symbol_fallback_uses_cache_on_api_failure() -> None:
    service = _build_service()
    service.client.positions_by_exchange = {
        "NYSE": [{"ovrs_cblc_qty": "2", "ovrs_pdno": "DDD"}],
    }

    held = asyncio.run(service._get_held_symbols())
    assert held == {"DDD"}
    assert service._last_held_symbols == {"DDD"}

    service.client.raise_balance_error = True

    cached = asyncio.run(service._get_held_symbols())

    assert cached == {"DDD"}


def test_held_symbol_in_signal_cache_even_if_low_score() -> None:
    service = _build_service()
    service.client.positions_by_exchange = {
        "NYSE": [{"ovrs_cblc_qty": "3", "ovrs_pdno": "FFF"}],
    }
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 1.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    ranked, held_symbols = asyncio.run(service.scan_overseas())

    assert [item.symbol for item in ranked[:2]] == ["AAA", "BBB"]
    assert held_symbols == {"FFF"}
    assert set(service._signal_cache.keys()) == {"AAA", "FFF"}


def test_build_watch_targets_uses_cache_not_api() -> None:
    service = _build_service()
    _install_watch_builder_stub(service)
    overseas_ranked = [
        _result("AAA", 10.0),
        _result("BBB", 9.0),
        _result("CCC", 8.0),
    ]
    cached_snapshot = _snapshot()
    service._signal_cache = {
        "AAA": cached_snapshot,
        "BBB": None,
    }

    async def fail_load_signal(candidate):
        raise AssertionError("watch target build should use signal cache only")

    service._load_overseas_signal = fail_load_signal

    watch_targets = asyncio.run(service._build_overseas_watch_targets(overseas_ranked, []))

    assert [item.code for item in watch_targets] == ["AAA", "BBB"]
    assert watch_targets[0].signal_snapshot is cached_snapshot
    assert watch_targets[1].signal_snapshot is None


def test_non_signal_symbol_not_in_watch_targets() -> None:
    service = _build_service()
    _install_watch_builder_stub(service)
    service._signal_cache = {
        "AAA": _snapshot(),
        "BBB": _snapshot(21.0),
    }
    overseas_ranked = [
        _result("AAA", 10.0),
        _result("BBB", 9.0),
        _result("CCC", 8.0),
    ]

    watch_targets = asyncio.run(service._build_overseas_watch_targets(overseas_ranked, []))

    assert [item.code for item in watch_targets] == ["AAA", "BBB"]


def test_estimate_api_calls_overseas_reflects_new_structure() -> None:
    service = _build_service()
    service.config.liquidity_lab.overseas_candidates = [
        DummyCandidate(f"S{i:02d}", "NASD" if i < 35 else "NYSE")
        for i in range(69)
    ]
    service.config.liquidity_lab.unified_scan_top_n = 15
    service.config.liquidity_lab.overseas_scan_top_n = 15
    service._last_held_symbols = set()

    estimated = service._estimate_api_calls_per_cycle(
        krx_open=False,
        us_open=True,
        domestic_watch_count=0,
        overseas_watch_count=15,
        include_domestic_paper=False,
        include_overseas_order=False,
    )

    assert estimated == 101


def test_scan_overseas_wait_penalty_reorders_long_wait_symbol() -> None:
    service = _build_service()
    service._wait_cycles = {
        "overseas:AAA": 25,
        "overseas:BBB": 0,
    }

    async def fake_scan(candidate):
        score_map = {"AAA": 10.0, "BBB": 9.5, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}
        return _result(candidate.symbol, score_map[candidate.symbol])

    async def fake_load_signal(candidate):
        return _snapshot(price=candidate.last_price)

    service._scan_single_overseas = fake_scan
    service._load_overseas_signal = fake_load_signal

    ranked, _ = asyncio.run(service.scan_overseas())

    symbols = [item.symbol for item in ranked]
    assert symbols[0] == "BBB"
    assert symbols.index("AAA") > symbols.index("BBB")
