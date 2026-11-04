"""Freelancer account handling: which login the bot uses, and how to change it.

The bot can only act as whoever the cookie file says. That file expires, and when it
does every scan silently returns an anonymous feed — so the account is a first-class
setting, not a file you edit over SSH.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

KEY = "account"

REQUIRED = ("GETAFREE_USER_ID", "GETAFREE_AUTH_HASH_V2")
USEFUL = ("XSRF-TOKEN",)


def parse_cookies(raw: str) -> dict[str, str]:
    """Accept what people actually paste: header lines, `a=b; c=d`, or a Netscape dump."""
    out: dict[str, str] = {}
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Netscape cookie file: domain \t flag \t path \t secure \t expiry \t name \t value
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5].strip()] = parts[6].strip()
                continue
        for piece in line.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            name, _, value = piece.partition("=")
            name = name.strip()
            if name and not name.lower().startswith("cookie:"):
                out[name] = value.strip()
    return out


@dataclass
class Account:
    username: str
    user_id: str

    @classmethod
    def load(cls, store, cfg=None) -> "Account":
        """Per-tenant profile. `cfg` is only a bootstrap for a brand-new install."""
        saved = store.get_meta(KEY, None) or {}
        return cls(
            username=str(saved.get("username") or (getattr(cfg, "username", "") or "")),
            user_id=str(saved.get("user_id") or (getattr(cfg, "user_id", "") or "")),
        )

    def save(self, store) -> None:
        store.set_meta(KEY, {"username": self.username, "user_id": self.user_id})

    # ------------------------------------------------------------------ file
    @staticmethod
    def read_cookies(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        return parse_cookies(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_cookies(path: Path, cookies: dict[str, str]) -> None:
        body = "\n".join(f"{k}={v}" for k, v in cookies.items()) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def cookie_report(path: Path) -> str:
        if not path.exists():
            return "❌ فایل کوکی نیست"
        age_min = int((time.time() - path.stat().st_mtime) / 60)
        cookies = Account.read_cookies(path)
        missing = [k for k in REQUIRED if not cookies.get(k)]
        when = f"{age_min} دقیقه پیش" if age_min < 1440 else f"{age_min // 1440} روز پیش"
        head = f"فایل کوکی: {len(cookies)} مقدار، آخرین بار {when}"
        if missing:
            return head + "\n❌ ناقص: " + ", ".join(missing)
        return head + "\n✅ کلیدهای لازم موجود است"
