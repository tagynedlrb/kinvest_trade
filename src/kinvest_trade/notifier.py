from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from .config import NotificationConfig

if TYPE_CHECKING:
    from .repository import SqliteRepository


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
                error=error,
            )
        except Exception:  # noqa: BLE001
            pass

    async def send(self, message: str) -> bool:
        if not self.enabled:
            return False

        url = self._api_url("sendMessage")
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": message,
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._log_outbound(message, success=False, error=str(exc)[:200])
            raise
        self._log_outbound(message, success=True)
        return True

    async def set_commands(self, commands: list[dict[str, str]]) -> bool:
        if not self.enabled:
            return False

        url = self._api_url("setMyCommands")
        payload = {"commands": commands}

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
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

        async with httpx.AsyncClient(timeout=max(params["timeout"] + 5, 10)) as client:
            response = await client.get(self._api_url("getUpdates"), params=params)
            response.raise_for_status()
            payload = response.json()

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
