"""Config loading. Everything comes from env vars or secrets/, nothing hardcoded."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
DATA = ROOT / "data"


def _load_env_file() -> None:
    """Read ROOT/.env so the bot needs nothing but its own directory to run.

    Real environment variables always win, which keeps systemd overrides working
    on a server where the file is shared with other services.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret(name: str, env: str = "") -> str:
    """Read a secret from env first (systemd EnvironmentFile), then from secrets/."""
    if env:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    path = SECRETS / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


class Config:
    def __init__(self) -> None:
        self.bot_token = _secret("bot_token.txt", "FL_BOT_TOKEN")
        self.chat_id = _cfg("FL_CHAT_ID") or _secret("chat_id.txt")
        self.user_id = _cfg("FL_USER_ID", "40891893")
        self.username = _cfg("FL_USERNAME", "aryanhz")
        self.cookies_path = Path(_cfg("FL_COOKIES", str(SECRETS / "cookies.txt")))
        self.db_path = Path(_cfg("FL_DB", str(DATA / "fl.db")))
        self.seen_path = Path(_cfg("FL_SEEN", str(DATA / "seen.json")))

        # scan filters
        self.budget_min = float(_cfg("FL_BUDGET_MIN", "10"))
        self.budget_max = float(_cfg("FL_BUDGET_MAX", "800"))
        self.max_bids = int(_cfg("FL_MAX_BIDS", "60"))
        self.shortlist = int(_cfg("FL_SHORTLIST", "3"))

        # proposal defaults (user confirms before anything is sent)
        self.default_period = int(_cfg("FL_PERIOD", "7"))
        self.milestone = int(_cfg("FL_MILESTONE", "100"))

        self.base = "https://www.freelancer.com/api"
        self.web = "https://www.freelancer.com"

    def save_chat_id(self, chat_id: str) -> None:
        """Adopt the first chat that talks to the bot, so no id has to be guessed."""
        self.chat_id = str(chat_id)
        path = SECRETS / "chat_id.txt"
        path.write_text(self.chat_id, encoding="utf-8")
        path.chmod(0o600)

    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.cookies_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k] = v
        return out