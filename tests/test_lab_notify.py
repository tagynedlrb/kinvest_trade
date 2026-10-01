import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from kinvest_trade.lab_notify import TradeNotifier


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def test_failed_send_preserves_queue_and_successful_retry_clears_it():
    class Flaky(DummyNotifier):
        async def send(self, message):
            if not self.messages:
                self.messages.append("failed")
                raise TimeoutError("temporary")
            return await super().send(message)

    async def run_case():
        sender = Flaky()
        batch = TradeNotifier(sender)
        batch.queue("confirmed fill")
        with pytest.raises(TimeoutError):
            await batch.flush_async(force=True)
        assert batch.queued_lines == ["confirmed fill"]
        await batch.flush_async(force=True)
        assert not batch.queued_lines
        assert "confirmed fill" in sender.messages[-1]
    asyncio.run(run_case())


def test_disabled_notifier_does_not_discard_pending_fills():
    class Disabled:
        async def send(self, message):
            return False

    async def run_case():
        for sender in (None, Disabled()):
            batch = TradeNotifier(sender)
            batch.queue("fill")
            await batch.flush_async(force=True)
            assert batch.queued_lines == ["fill"]
    asyncio.run(run_case())


def test_new_lines_queued_during_send_survive_acknowledgement():
    class Sender:
        async def send(self, message):
            batch.queue("second fill")
            return True

    async def run_case():
        batch.queue("first fill")
        await batch.flush_async(force=True)
        assert batch.queued_lines == ["second fill"]
    batch = TradeNotifier(Sender())
    asyncio.run(run_case())


def test_trade_notifier_defers_flush_inside_window() -> None:
    async def run_case() -> None:
        notifier = DummyNotifier()
        trade_notifier = TradeNotifier(notifier, window_seconds=60, max_batch_size=8)
        trade_notifier.queue("해외 TSLA 매수접수 +$280.00 x1")

        await trade_notifier.flush_async(force=False)

        assert notifier.messages == []
        assert trade_notifier.queued_lines == ["해외 TSLA 매수접수 +$280.00 x1"]

    asyncio.run(run_case())


def test_trade_notifier_flushes_after_window_or_force() -> None:
    async def run_case() -> None:
        notifier = DummyNotifier()
        trade_notifier = TradeNotifier(notifier, window_seconds=60, max_batch_size=8)
        trade_notifier.load_state(
            lines=["국내 005930 매도접수 82,000원 x1"],
            window_start=datetime.now(timezone.utc) - timedelta(seconds=61),
        )

        await trade_notifier.flush_async(force=False)

        assert len(notifier.messages) == 1
        assert notifier.messages[0].startswith("[KIS][거래알림]")
        assert "건수=1" in notifier.messages[0]
        assert "국내 005930 매도접수 82,000원 x1" in notifier.messages[0]
        assert trade_notifier.queued_lines == []

        trade_notifier.queue("해외 NVDA 매수접수 +$130.00 x1")
        await trade_notifier.flush_async(force=True)
        assert len(notifier.messages) == 2
        assert "해외 NVDA 매수접수 +$130.00 x1" in notifier.messages[1]

    asyncio.run(run_case())
