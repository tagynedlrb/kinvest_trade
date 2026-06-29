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
            overseas_scan_top_n=2,
            overseas_min_price_usd=10.0,
            overseas_min_volume=100,
            overseas_max_spread_pct=0.01,
        )
    )
    service.repository = DummyRepository()
    service.notifier = None
    service.client = DummyClient()
    service._domestic_excluded = []
    service._overseas_excluded = []
    service._last_held_symbols = set()
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


def test_scan_overseas_returns_all_passing_candidates() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        volume = 10 if candidate.symbol == "FFF" else 1000
        return _result(candidate.symbol, score_map[candidate.symbol], volume=volume)

    service._scan_single_overseas = fake_scan

    ranked = asyncio.run(service.scan_overseas())

    assert [item.symbol for item in ranked] == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_scan_overseas_held_symbol_always_in_signal_targets() -> None:
    service = _build_service()
    service.client.positions_by_exchange = {
        "NYSE": [{"ovrs_cblc_qty": "3", "ovrs_pdno": "FFF"}],
    }
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 1.0}

    async def fake_scan(candidate):
        return _result(candidate.symbol, score_map[candidate.symbol])

    service._scan_single_overseas = fake_scan

    ranked = asyncio.run(service.scan_overseas())

    assert [item.symbol for item in ranked[:3]] == ["AAA", "BBB", "FFF"]
    assert service._last_held_symbols == {"FFF"}


def test_scan_overseas_excluded_not_in_results() -> None:
    service = _build_service()
    score_map = {"AAA": 10.0, "BBB": 9.0, "CCC": 8.0, "DDD": 7.0, "EEE": 6.0, "FFF": 5.0}

    async def fake_scan(candidate):
        volume = 10 if candidate.symbol == "AAA" else 1000
        return _result(candidate.symbol, score_map[candidate.symbol], volume=volume)

    service._scan_single_overseas = fake_scan

    ranked = asyncio.run(service.scan_overseas())

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


def test_estimate_api_calls_overseas_reflects_new_structure() -> None:
    service = _build_service()
    service.config.liquidity_lab.overseas_candidates = [
        DummyCandidate(f"S{i:02d}", "NASD" if i < 35 else "NYSE")
        for i in range(69)
    ]
    service.config.liquidity_lab.overseas_scan_top_n = 69
    service._last_held_symbols = set()

    estimated = service._estimate_api_calls_per_cycle(
        krx_open=False,
        us_open=True,
        domestic_watch_count=0,
        overseas_watch_count=69,
        include_domestic_paper=False,
        include_overseas_order=False,
    )

    assert estimated == 211
