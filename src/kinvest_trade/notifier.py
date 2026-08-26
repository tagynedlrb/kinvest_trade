from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from .config import NotificationConfig

if TYPE_CHECKING:
    from .repository import SqliteRepository

_logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        config: NotificationConfig,
        *,
        repository: "SqliteRepository | None" = None,
    ) -> None:
        self.config = config
        self.repository = repository

    @property
    def enabled(self) -> bool:
        return (
            self.config.telegram_enabled
            and bool(self.config.telegram_bot_token)
            and bool(self.config.telegram_chat_id)
        )

    def _log_outbound(self, text: str, *, success: bool, error: str = "") -> None:
        if self.repository is None:
            return
        try:
            self.repository.save_telegram_message(
                created_at=datetime.now(timezone.utc).isoformat(),
                direction="sent",
                text=text,
                success=success,
                error=self._sanitize_error(error),
            )
        except Exception:  # noqa: BLE001
            _logger.exception("telegram_outbound_log_failed")

    def _sanitize_error(self, error: object) -> str:
        redacted = str(error or "")
        token = str(self.config.telegram_bot_token or "")
        if token:
            redacted = redacted.replace(token, "<redacted>")
        redacted = re.sub(
            r"(https://api\.telegram\.org/bot)[^/\s'\"?]+",
            r"\1<redacted>",
            redacted,
        )
        return redacted[:200]

    def _redacted_error(self, exc: Exception) -> RuntimeError:
        return RuntimeError(self._sanitize_error(exc))

    async def send(
        self,
        message: str,
        *,
        reply_markup: dict | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        url = self._api_url("sendMessage")
        payload: dict = {
            "chat_id": self.config.telegram_chat_id,
            "text": message,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._log_outbound(message, success=False, error=str(exc))
            raise self._redacted_error(exc) from None
        self._log_outbound(message, success=True)
        return True

    async def edit_message(
        self,
        *,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        url = self._api_url("editMessageText")
        payload: dict = {
            "chat_id": self.config.telegram_chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._redacted_error(exc) from None
        return bool(body.get("ok"))

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> bool:
        if not self.enabled:
            return False

        url = self._api_url("answerCallbackQuery")
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._redacted_error(exc) from None
        return bool(body.get("ok"))

    async def set_commands(self, commands: list[dict[str, str]]) -> bool:
        if not self.enabled:
            return False

        url = self._api_url("setMyCommands")
        payload = {"commands": commands}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._redacted_error(exc) from None
        return bool(body.get("ok"))

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_sec: int | None = None,
    ) -> list[dict]:
        if not self.enabled:
            return []

        params: dict[str, int] = {
            "timeout": timeout_sec
            if timeout_sec is not None
            else self.config.telegram_command_poll_timeout_sec,
        }
        if offset is not None:
            params["offset"] = offset

        request_timeout = httpx.Timeout(
            connect=5.0,
            read=max(float(params["timeout"]) + 10.0, 15.0),
            write=5.0,
            pool=5.0,
        )
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.get(self._api_url("getUpdates"), params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._redacted_error(exc) from None

        if not payload.get("ok"):
            return []
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    def is_authorized_chat(self, chat_id: str | int | None) -> bool:
        if chat_id is None:
            return False
        return str(chat_id).strip() == self.config.telegram_chat_id.strip()

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.telegram_bot_token}/{method}"
