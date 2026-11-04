"""AI endpoint settings, editable from Telegram and stored in SQLite.

Nothing about the model lives in code: base URL, key, model and sampling come from
the `meta` table (set with /ai in the chat) and fall back to env/secrets on a fresh
install. That keeps the bot portable — copy the directory, set the bot token, done.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from .config import SECRETS

KEY = "ai_settings"
DEFAULTS = {
    "base_url": "https://ai.metft.ir/v1",
    "api_key": "",
    "model": "glm-5.3-flash",
    "temperature": 0.4,
    "max_tokens": 700,
    "timeout": 120,
}


@dataclass
class AISettings:
    base_url: str = DEFAULTS["base_url"]
    api_key: str = ""
    model: str = DEFAULTS["model"]
    temperature: float = DEFAULTS["temperature"]
    max_tokens: int = DEFAULTS["max_tokens"]
    timeout: int = DEFAULTS["timeout"]
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, store=None) -> "AISettings":
        data = dict(DEFAULTS)
        # secrets/ai_key.txt and env are the bootstrap path for a fresh server.
        key_file = SECRETS / "ai_key.txt"
        if key_file.exists():
            data["api_key"] = key_file.read_text(encoding="utf-8").strip()
        data["api_key"] = os.environ.get("FL_AI_KEY", data["api_key"])
        data["base_url"] = os.environ.get("FL_AI_BASE", data["base_url"])
        data["model"] = os.environ.get("FL_AI_MODEL", data["model"])
        if store is not None:
            saved = store.get_meta(KEY, None)
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        return cls(**data)

    def save(self, store) -> None:
        store.set_meta(KEY, {k: getattr(self, k) for k in DEFAULTS})

    def redacted_key(self) -> str:
        k = self.api_key or ""
        if not k:
            return "تنظیم نشده"
        if len(k) <= 10:
            return k[:3] + "…"
        return f"{k[:6]}…{k[-4:]} ({len(k)} chars)"

    def summary(self) -> str:
        return (
            "⚙️ <b>تنظیمات AI</b>\n"
            f"Base URL: <code>{self.base_url}</code>\n"
            f"مدل: <code>{self.model}</code>\n"
            f"کلید: <code>{self.redacted_key()}</code>\n"
            f"temperature={self.temperature} · max_tokens={self.max_tokens} · timeout={self.timeout}s"
        )


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         temperature: float = 0.4, max_tokens: int = 700, timeout: int = 120) -> str:
    """One OpenAI-compatible chat call. Raises RuntimeError with a usable message."""
    import requests

    if not api_key:
        raise RuntimeError("کلید API تنظیم نشده — با /ai کلید را وارد کن")
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:300]}")
    try:
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"پاسخ نامعتبر از مدل: {exc}") from exc
    if not text:
        raise RuntimeError("مدل پاسخ خالی داد")
    return text


def ping(s: AISettings) -> str:
    """Round-trip the endpoint and return the model's reply, so /ai can prove it works."""
    return chat(s.base_url, s.api_key, s.model,
                [{"role": "user", "content": "Reply with the single word: OK"}],
                temperature=0, max_tokens=10, timeout=min(s.timeout, 60))
