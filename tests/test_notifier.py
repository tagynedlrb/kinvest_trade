from __future__ import annotations

import asyncio
from types import SimpleNamespace

import kinvest_trade.notifier as notifier_module
from kinvest_trade.notifier import TelegramNotifier


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeAsyncClient:
    def __init__(self, *, payload: dict, calls: list[tuple[str, dict]]) -> None:
        self.payload = payload
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, json: dict):
        self.calls.append((url, json))
        return FakeResponse(self.payload)


def test_set_commands_returns_false_when_disabled() -> None:
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=False,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_command_poll_timeout_sec=30,
        )
    )

    result = asyncio.run(
        notifier.set_commands([{"command": "lab_start", "description": "거래 루프 시작"}])
    )

    assert result is False


def test_set_commands_sends_correct_payload() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True, "result": True},
        calls=calls,
    )
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="token123",
            telegram_chat_id="chat456",
            telegram_command_poll_timeout_sec=30,
        )
    )
    try:
        asyncio.run(
            notifier.set_commands([{"command": "lab_start", "description": "거래 루프 시작"}])
        )
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert calls == [
        (
            "https://api.telegram.org/bottoken123/setMyCommands",
            {"commands": [{"command": "lab_start", "description": "거래 루프 시작"}]},
        )
    ]


def test_set_commands_returns_true_on_ok_response() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True, "result": True},
        calls=calls,
    )
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="token123",
            telegram_chat_id="chat456",
            telegram_command_poll_timeout_sec=30,
        )
    )
    try:
        result = asyncio.run(
            notifier.set_commands([{"command": "lab_help", "description": "명령 목록 보기"}])
        )
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert result is True
