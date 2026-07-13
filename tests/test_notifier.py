from __future__ import annotations

import asyncio
from types import SimpleNamespace

import kinvest_trade.notifier as notifier_module
from kinvest_trade.notifier import TelegramNotifier
from kinvest_trade.repository import SqliteRepository


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


def test_send_logs_outbound_message_to_repository_on_success(tmp_path) -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True},
        calls=calls,
    )
    repository = SqliteRepository(tmp_path / "notifier.db")
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="token123",
            telegram_chat_id="chat456",
            telegram_command_poll_timeout_sec=30,
        ),
        repository=repository,
    )
    try:
        asyncio.run(notifier.send("[KIS][TEST] hello"))
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    messages = repository.list_telegram_messages()
    assert len(messages) == 1
    assert messages[0]["direction"] == "sent"
    assert messages[0]["text"] == "[KIS][TEST] hello"
    assert messages[0]["success"] == 1


def test_send_logs_outbound_failure_and_reraises(tmp_path) -> None:
    class FailingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict):
            raise RuntimeError("network down")

    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FailingAsyncClient()  # type: ignore[assignment]
    repository = SqliteRepository(tmp_path / "notifier_fail.db")
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="token123",
            telegram_chat_id="chat456",
            telegram_command_poll_timeout_sec=30,
        ),
        repository=repository,
    )
    try:
        try:
            asyncio.run(notifier.send("[KIS][TEST] boom"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError to propagate")
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    messages = repository.list_telegram_messages()
    assert len(messages) == 1
    assert messages[0]["direction"] == "sent"
    assert messages[0]["success"] == 0
    assert "network down" in messages[0]["error"]


def test_send_without_repository_does_not_error() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True},
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
        result = asyncio.run(notifier.send("no repository configured"))
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert result is True


def test_send_includes_reply_markup_when_provided() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True},
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
    keyboard = {"inline_keyboard": [[{"text": "메뉴", "callback_data": "menu:root"}]]}
    try:
        asyncio.run(notifier.send("메뉴를 선택하세요", reply_markup=keyboard))
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert calls[0][1]["reply_markup"] == keyboard


def test_edit_message_sends_correct_payload_and_returns_ok() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True},
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
    keyboard = {"inline_keyboard": [[{"text": "◀ 메뉴", "callback_data": "menu:root"}]]}
    try:
        result = asyncio.run(
            notifier.edit_message(message_id=42, text="갱신된 내용", reply_markup=keyboard)
        )
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert result is True
    assert calls == [
        (
            "https://api.telegram.org/bottoken123/editMessageText",
            {
                "chat_id": "chat456",
                "message_id": 42,
                "text": "갱신된 내용",
                "reply_markup": keyboard,
            },
        )
    ]


def test_edit_message_returns_false_when_disabled() -> None:
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=False,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_command_poll_timeout_sec=30,
        )
    )

    result = asyncio.run(notifier.edit_message(message_id=1, text="x"))

    assert result is False


def test_answer_callback_query_sends_correct_payload() -> None:
    calls: list[tuple[str, dict]] = []
    original_async_client = notifier_module.httpx.AsyncClient
    notifier_module.httpx.AsyncClient = lambda timeout: FakeAsyncClient(  # type: ignore[assignment]
        payload={"ok": True},
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
        result = asyncio.run(notifier.answer_callback_query("cbq-1", text="열었습니다"))
    finally:
        notifier_module.httpx.AsyncClient = original_async_client

    assert result is True
    assert calls == [
        (
            "https://api.telegram.org/bottoken123/answerCallbackQuery",
            {"callback_query_id": "cbq-1", "text": "열었습니다"},
        )
    ]


def test_answer_callback_query_returns_false_when_disabled() -> None:
    notifier = TelegramNotifier(
        SimpleNamespace(
            telegram_enabled=False,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_command_poll_timeout_sec=30,
        )
    )

    result = asyncio.run(notifier.answer_callback_query("cbq-1"))

    assert result is False
