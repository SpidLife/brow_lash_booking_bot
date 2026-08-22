from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **payload: Any) -> Any:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + method,
            data=encoded,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        timeout = int(payload.get("timeout", 0)) + 15
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                description = json.loads(body).get("description", "Telegram вернул ошибку")
            except json.JSONDecodeError:
                description = f"HTTP {exc.code}"
            raise TelegramAPIError(description) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelegramAPIError(f"Проблема соединения с Telegram: {exc}") from exc
        if not result.get("ok"):
            raise TelegramAPIError(result.get("description", "Неизвестная ошибка Telegram"))
        return result.get("result")

    def send(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", **extra}
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return self.call("sendMessage", **payload)

    def send_photo(
        self,
        chat_id: int,
        file_id: str,
        caption: str = "",
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": file_id, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption[:1024]
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return self.call("sendPhoto", **payload)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            self.call("editMessageText", **payload)
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).lower():
                return
            if len(text) <= 1024:
                caption_payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "caption": text,
                    "parse_mode": "HTML",
                }
                if keyboard is not None:
                    caption_payload["reply_markup"] = keyboard
                try:
                    self.call("editMessageCaption", **caption_payload)
                    return
                except TelegramAPIError as caption_exc:
                    if "message is not modified" in str(caption_exc).lower():
                        return
            self.send(chat_id, text, keyboard)

    def edit_photo(
        self,
        chat_id: int,
        message_id: int,
        file_id: str,
        caption: str = "",
        keyboard: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": {
                "type": "photo",
                "media": file_id,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            self.call("editMessageMedia", **payload)
        except TelegramAPIError as exc:
            if "message is not modified" not in str(exc).lower():
                self.send_photo(chat_id, file_id, caption, keyboard)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)


def inline(*rows: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
            if row
        ]
    }


def reply_keyboard(*rows: list[dict[str, Any] | str], one_time: bool = False) -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": button} if isinstance(button, str) else button for button in row]
            for row in rows
        ],
        "resize_keyboard": True,
        "one_time_keyboard": one_time,
    }
