"""Telegram transport: sending messages and long-polling for buttons/callbacks."""

from __future__ import annotations

import html
import logging
import time

import requests

from .config import Config

log = logging.getLogger(__name__)
TG = "https://api.telegram.org/bot{token}/{method}"
MAXLEN = 3800


class TelegramError(RuntimeError):
    pass


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if not cfg.bot_token:
            raise TelegramError("no bot token: secrets/bot_token.txt or FL_BOT_TOKEN")

    def call(self, method: str, timeout: int = 30, **payload):
        r = requests.post(TG.format(token=self.cfg.bot_token, method=method), json=payload, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, text: str, keyboard: list[list[dict]] | None = None, preview: bool = False) -> int:
        if not self.cfg.chat_id:
            # Nothing has talked to the bot yet; stay silent instead of erroring.
            log.warning("no chat bound yet, dropping message")
            return 0
        chunks = [text[i:i + MAXLEN] for i in range(0, len(text), MAXLEN)] or [""]
        last = 0
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": self.cfg.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            }
            # Attach the keyboard only to the final chunk so it sits under the message.
            if keyboard and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            res = self.call("sendMessage", **payload)
            last = res["message_id"]
        return last

    def edit(self, message_id: int, text: str, keyboard: list[list[dict]] | None = None) -> None:
        payload = {
            "chat_id": self.cfg.chat_id,
            "message_id": message_id,
            "text": text[:MAXLEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self.call("editMessageText", **payload)
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset
        r = requests.post(
            TG.format(token=self.cfg.bot_token, method="getUpdates"),
            json=payload,
            timeout=timeout + 15,
        )
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"getUpdates: {data.get('description')}")
        return data["result"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])
        except TelegramError:
            log.warning("answerCallbackQuery failed", exc_info=True)


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)
