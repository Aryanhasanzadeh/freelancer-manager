"""One user's isolated workspace: their account, cookies, settings and data.

A `Tenant` is built per chat id and is the only thing the handlers touch. It holds a
`TenantStore` (every query scoped to that chat id), its own Freelancer client, its own
AI settings and its own runtime knobs — so two users of the same bot can never read or
write each other's projects, drafts, bids, cookies or API key.

Cookies live in the user's row space (`meta` under `cookies`), not in one shared file:
the bot acts as whoever that user connected. The very first owner keeps working after
the upgrade because their old `secrets/cookies.txt` is imported once, on first use.
"""

from __future__ import annotations

import time

from .account import Account
from .aisettings import AISettings
from .api import Client
from .runtime import Runtime
from .scan import Filters

COOKIE_KEY = "cookies"
PROFILE_KEY = "profile"


class Tenant:
    def __init__(self, cfg, store, chat_id: str) -> None:
        self.cfg = cfg
        self.chat = str(chat_id)
        self.store = store.tenant(self.chat)
        self.row = store.user(self.chat)
        self.acc = Account.load(self.store)
        self.cookies = self._cookies()
        self.api = Client(cfg, cookies=self.cookies, user_id=self.acc.user_id)
        self.ai = AISettings.load(self.store)
        self.rt = Runtime.load(self.store)

    # ---------------------------------------------------------------- role
    @property
    def role(self) -> str:
        return (self.row["role"] if self.row else "member") or "member"

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def name(self) -> str:
        return (self.row["name"] if self.row else "") or ""

    # ------------------------------------------------------------- cookies
    def _cookies(self) -> dict[str, str]:
        saved = self.store.get_meta(COOKIE_KEY, None)
        if isinstance(saved, dict) and saved:
            return {str(k): str(v) for k, v in saved.items()}
        # Upgrade path: the single-account install kept one cookie file. Give it,
        # once, to the chat that owned the bot — never to anybody else.
        if self.chat == str(self.cfg.chat_id or "") and self.cfg.cookies_path.exists():
            from .account import parse_cookies
            legacy = parse_cookies(self.cfg.cookies_path.read_text(encoding="utf-8"))
            if legacy:
                self.save_cookies(legacy)
                return legacy
        return {}

    def save_cookies(self, cookies: dict[str, str]) -> None:
        self.store.set_meta(COOKIE_KEY, cookies)
        self.store.set_meta(COOKIE_KEY + "_at", int(time.time()))
        self.cookies = dict(cookies)
        uid = cookies.get("GETAFREE_USER_ID", "")
        if uid:
            self.acc.user_id = str(uid)
            self.acc.save(self.store)
        self.reload_api()

    def clear_cookies(self) -> None:
        self.store.set_meta(COOKIE_KEY, {})
        self.store.set_meta(COOKIE_KEY + "_at", 0)
        self.cookies = {}
        self.reload_api()

    def reload_api(self) -> None:
        self.api = Client(self.cfg, cookies=self.cookies, user_id=self.acc.user_id)

    def cookie_report(self) -> str:
        if not self.cookies:
            return "❌ اکانتی وصل نیست"
        saved_at = self.store.get_meta(COOKIE_KEY + "_at", 0) or 0
        missing = [k for k in ("GETAFREE_USER_ID", "GETAFREE_AUTH_HASH_V2") if not self.cookies.get(k)]
        age = ""
        if saved_at:
            mins = int((time.time() - float(saved_at)) / 60)
            age = f"، {mins} دقیقه پیش" if mins < 1440 else f"، {mins // 1440} روز پیش"
        head = f"کوکی: {len(self.cookies)} مقدار{age}"
        if missing:
            return f"{head}\n❌ ناقص: {', '.join(missing)}"
        return f"{head}\n✅ کلیدهای لازم موجود است"

    # ------------------------------------------------------------- settings
    @property
    def filters(self) -> Filters:
        return Filters(self.rt["budget_min"], self.rt["budget_max"], self.rt["max_bids"])

    @property
    def period(self) -> int:
        return int(self.rt["period"])

    @property
    def shortlist(self) -> int:
        return int(self.rt["shortlist"])

    @property
    def milestone(self) -> int:
        return int(self.rt["milestone"])

    @property
    def profile(self) -> str:
        return str(self.store.get_meta(PROFILE_KEY, "") or "")

    def set_profile(self, text: str) -> None:
        self.store.set_meta(PROFILE_KEY, text.strip()[:1200])

    # ----------------------------------------------------------------- work
    def on_scan(self, found: int, pool: int) -> None:
        self.store.set_meta("last_scan", int(time.time()))
        self.store.set_meta("last_scan_pool", int(pool))
        self.store.set_meta("last_scan_found", int(found))
