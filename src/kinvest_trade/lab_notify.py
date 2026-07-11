from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .time_utils import format_kst_korean

if TYPE_CHECKING:
    from .notifier import TelegramNotifier


class TradeNotifier:
    """Batch trade notifications into a short time window."""

    def __init__(
        self,
        notifier: "TelegramNotifier | None",
        *,
        window_seconds: int = 60,
        max_batch_size: int = 8,
    ) -> None:
        self._notifier = notifier
        self._window_seconds = self._coerce_window_seconds(window_seconds)
        self._max_batch_size = self._coerce_max_batch_size(max_batch_size)
        self._queue: list[str] = []
        self._window_start: datetime | None = None

    @property
    def queued_lines(self) -> list[str]:
        return list(self._queue)

    @property
    def window_start(self) -> datetime | None:
        return self._window_start

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def set_notifier(self, notifier: "TelegramNotifier | None") -> None:
        self._notifier = notifier

    def set_window_seconds(self, value: int) -> None:
        self._window_seconds = self._coerce_window_seconds(value)

    def set_max_batch_size(self, value: int) -> None:
        self._max_batch_size = self._coerce_max_batch_size(value)

    def load_state(
        self,
        *,
        lines: list[str] | None,
        window_start: datetime | None,
    ) -> None:
        self._queue = list(lines or [])
        self._window_start = window_start

    def queue(self, line: str) -> None:
        if not line:
            return
        if not self._queue:
            self._window_start = datetime.now(timezone.utc)
        self._queue.append(line)

    def force_immediate(self) -> bool:
        return self._window_seconds <= 0

    async def flush_async(self, *, force: bool = False) -> None:
        if not self._queue:
            return

        now = datetime.now(timezone.utc)
        started_at = self._window_start or now
        batch_size = len(self._queue)
        age_sec = max((now - started_at).total_seconds(), 0.0)
        if (
            not force
            and age_sec < float(self._window_seconds)
            and batch_size < self._max_batch_size
        ):
            return

        notifier = self._notifier
        lines = [
            "[KIS][거래알림]",
            f"시각={format_kst_korean(now)}",
            f"건수={batch_size}",
            *self._queue,
        ]
        try:
            if notifier is not None:
                await notifier.send("\n".join(lines))
        finally:
            self._queue = []
            self._window_start = None

    @staticmethod
    def _coerce_window_seconds(value: int) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _coerce_max_batch_size(value: int) -> int:
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            return 8
