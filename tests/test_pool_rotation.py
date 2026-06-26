import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from kinvest_trade.liquidity_lab import LiquidityLabService, OverseasScanResult


@dataclass
class DummyCandidate:
    symbol: str
    exchange_code: str


class DummyRepository:
    def __init__(self) -> None:
        self.heartbeats: list[tuple[str, str]] = []

    def save_heartbeat(self, status: str, message: str) -> None:
        self.heartbeats.append((status, message))


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
            overseas_active_pool_size=2,
            overseas_bench_scan_every=4,
            overseas_min_price_usd=10.0,
            overseas_min_volume=100,
            overseas_max_spread_pct=0.01,
        )
    )
    service.repository = DummyRepository()
    service.notifier = None
    service.client = None
    service._domestic_excluded = []
    service._overseas_excluded = []
    service._active_pool = []
    service._bench_pool = []
    service._cycle_count = 0
    service._pool_initialized = False
    service._bench_scanned_this_cycle = False
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


def test_first_cycle_runs_bench_scan() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    service._scan_single_overseas = fake_scan

    ranked = asyncio.run(service.scan_overseas())

    assert service._pool_initialized is True
    assert service._bench_scanned_this_cycle is True
    assert len(service._active_pool) <= service.config.liquidity_lab.overseas_active_pool_size
    assert [item.symbol for item in ranked] == ["AAA", "BBB"]
    assert any(status == "POOL_ROTATION" for status, _ in service.repository.heartbeats)


def test_bench_scan_runs_every_n_cycles() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    service._scan_single_overseas = fake_scan

    asyncio.run(service.scan_overseas())
    first_rotation_count = sum(1 for status, _ in service.repository.heartbeats if status == "POOL_ROTATION")
    asyncio.run(service.scan_overseas())
    asyncio.run(service.scan_overseas())
    asyncio.run(service.scan_overseas())
    second_rotation_count = sum(1 for status, _ in service.repository.heartbeats if status == "POOL_ROTATION")

    assert first_rotation_count == 1
    assert second_rotation_count == 2
    assert service._cycle_count == 4


def test_speculative_candidates_are_excluded_from_active_pool() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        volume = 10 if candidate.symbol == "AAA" else 1000
        return _result(candidate.symbol, score_map[candidate.symbol], volume=volume)

    service._scan_single_overseas = fake_scan

    asyncio.run(service.scan_overseas())

    assert [candidate.symbol for candidate in service._active_pool] == ["BBB", "CCC"]


def test_active_pool_falls_back_to_candidate_prefix_when_empty() -> None:
    service = _build_service()
    service._pool_initialized = True
    service._active_pool = []
    service._bench_pool = []

    async def fake_scan(candidate):
        return _result(candidate.symbol, 10.0)

    service._scan_single_overseas = fake_scan

    ranked = asyncio.run(service.scan_overseas())

    assert [candidate.symbol for candidate in service._active_pool] == ["AAA", "BBB"]
    assert len(ranked) == 2


def test_active_pool_size_is_never_exceeded() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    service._scan_single_overseas = fake_scan

    asyncio.run(service.scan_overseas())

    assert len(service._active_pool) <= service.config.liquidity_lab.overseas_active_pool_size
