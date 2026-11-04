"""Synthetic Telegram updates through the real dispatcher, with outbound calls captured.

The point of this suite is the multi-tenant boundary, so isolation is tested before
features: two chats must never read or write each other's projects, drafts, bids,
cookies, settings or AI key. A bot that serves the wrong chat looks healthy from the
outside, so every denial is asserted explicitly.

Usage: python3 tests/dispatch_test.py
No network write to Telegram, no bid placed.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freelancer_manager.bot import App  # noqa: E402
from freelancer_manager.config import Config  # noqa: E402
from freelancer_manager import logbuf, menu as menus  # noqa: E402
from freelancer_manager.account import parse_cookies  # noqa: E402
from freelancer_manager.aisettings import AISettings  # noqa: E402
from freelancer_manager.runtime import Runtime  # noqa: E402
from freelancer_manager.tg import Bot  # noqa: E402

SENT: list[tuple[str, list | None]] = []
MENUS: list[tuple[list[list[str]], str]] = []
# Who each outbound message went to: a leak test needs the recipient, not just the text.
TO: list[str] = []

OWNER = 999001
OTHER = 555000
NEWCOMER = 121212


def fake_send(self, text, keyboard=None, preview=False):  # noqa: ANN001
    SENT.append((text, keyboard))
    TO.append(str(self.chat))
    return len(SENT)


def fake_menu(self, rows, text="منو"):  # noqa: ANN001
    MENUS.append((rows, text))
    TO.append(str(self.chat))
    return 1


class _To:
    """Stand-in for Bot.to() so a reply is captured like any other send."""

    def __init__(self, bot, chat):
        self.bot, self.chat = bot, chat

    def __enter__(self):
        self.bot._local.chat = str(self.chat)
        return self.bot

    def __exit__(self, *exc):
        self.bot._local.chat = ""
        return False


Bot.to = lambda self, chat: _To(self, chat)  # type: ignore[method-assign]
Bot.menu = fake_menu  # type: ignore[method-assign]
Bot.hide_menu = lambda self, text="": None  # type: ignore[method-assign]
Bot.send = fake_send  # type: ignore[method-assign]
Bot.answer_callback = lambda self, *a, **k: None  # type: ignore[method-assign]
# Binding is checked in memory; never let a synthetic chat id touch secrets/chat_id.txt.
_real_save_chat_id = Config.save_chat_id
Config.save_chat_id = lambda self, chat_id: setattr(self, "chat_id", str(chat_id))  # type: ignore[method-assign]


def chat(cid: int) -> dict:
    return {"id": cid, "type": "private"}


def update_text(text: str, cid: int = OWNER) -> dict:
    return {"update_id": 1, "message": {"message_id": 1, "text": text, "chat": chat(cid),
                                        "from": {"id": cid, "first_name": f"u{cid}"}}}


def update_cb(data: str, cid: int = OWNER) -> dict:
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data,
                                               "message": {"message_id": 1, "chat": chat(cid)},
                                               "from": {"id": cid}}}


def text_of(index: int = 0) -> str:
    return SENT[index][0]


def buttons(index: int = 0) -> list[dict]:
    return [b for row in (SENT[index][1] or []) for b in row]


def check(label: str, cond: bool) -> bool:
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    return cond


def main() -> int:
    # Never touch the live database or the real cookie file: a test that writes them
    # leaks state between runs (and once overwrote the production AI key).
    sandbox = Path(tempfile.mkdtemp(prefix="fl-test-"))
    cfg = Config()
    cfg.db_path = sandbox / "fl.db"
    cfg.cookies_path = sandbox / "cookies.txt"
    cfg.chat_id = "dry-run"
    app = App(cfg)
    ok = True

    def t(cid: int = OWNER):
        """The tenant under test, exactly as the bot builds it per update."""
        return app.tenant(str(cid))

    default_ai = AISettings.load()

    # --------------------------------------------------------------- first run
    SENT.clear(); MENUS.clear(); TO.clear()
    app.handle(update_text("/start"))
    ok &= check("/start opens the menu",
                len(MENUS) == 1 and len(MENUS[0][0]) == len(menus.MENU_ROWS))
    # Compare against the module's own labels: ZWNJ in Persian words makes literals brittle.
    ok &= check("/start lists every section",
                [b for row in MENUS[0][0] for b in row] == menus.LABELS)
    ok &= check("every menu button maps to a command",
                all(b in menus.LABEL_TO_CMD for b in menus.LABELS))
    ok &= check("first chat becomes owner and binds the id",
                cfg.chat_id == str(OWNER) and app.db.user(str(OWNER))["role"] == "owner")

    # ----------------------------------------------------------- stranger boundary
    # Seed the owner with data a leak would be obvious in.
    t().store.save_draft(40802006, "OWNER-PRIVATE-DRAFT", 55.0, 7, "owner body")
    t().store.mark_seen(40802006, "OWNER-SEEN")
    t().store.set_meta("cookies", {"GETAFREE_USER_ID": "1", "GETAFREE_AUTH_HASH_V2": "a"})

    SENT.clear(); TO.clear()
    app.handle(update_text("/drafts", OTHER))
    ok &= check("a new chat is registered, not refused", app.db.user(str(OTHER)) is not None)
    ok &= check("a new chat is a member, never an owner",
                app.db.user(str(OTHER))["role"] == "member")
    ok &= check("a new chat sees none of the owner's drafts",
                bool(SENT) and all("OWNER-PRIVATE-DRAFT" not in x for x in SENT))
    ok &= check("every reply went to the asking chat", set(TO) == {str(OTHER)})

    SENT.clear()
    app.handle(update_text("/status", OTHER))
    ok &= check("the other chat's /status counts are its own, starting at zero",
                bool(SENT) and "دیده‌شده: <b>0</b>" in text_of()
                and "پیش‌نویس منتظر تأیید: <b>0</b>" in text_of())

    SENT.clear()
    app.handle(update_text("/bids", OTHER))
    ok &= check("a chat without cookies must connect its own account first",
                bool(SENT) and "اکانت فریلنسر خودت" in text_of())

    SENT.clear()
    app.handle(update_text("/scan", OTHER))
    ok &= check("a chat without cookies cannot scan on the owner's session",
                bool(SENT) and "اکانت فریلنسر خودت" in text_of())

    SENT.clear()
    app.handle(update_text("/account", OTHER))
    ok &= check("the other chat's account panel shows no cookie at all",
                bool(SENT) and "اکانتی وصل نیست" in text_of())

    SENT.clear()
    app.handle(update_text("price 40802006 40 5", OTHER))
    ok &= check("the other chat cannot reprice the owner's draft",
                t().store.draft(40802006)["amount"] == 55.0)

    SENT.clear()
    app.handle(update_cb("drop:40802006", OTHER))
    ok &= check("the other chat cannot drop the owner's draft",
                t().store.draft(40802006)["status"] == "pending")

    SENT.clear()
    app.handle(update_cb("send:40802006", OTHER))
    ok &= check("the other chat cannot send the owner's draft",
                "ذخیره نشده" in text_of() and t().store.draft(40802006)["status"] == "pending")

    # --------------------------------------------------------------- owner basics
    SENT.clear()
    app.handle(update_text("not a command"))
    ok &= check("unrelated text is ignored", SENT == [])

    SENT.clear()
    app.handle(update_text("price"))
    ok &= check("bare price is ignored", SENT == [])

    SENT.clear()
    app.handle(update_text("/drafts"))
    ok &= check("owner /drafts lists the owner's draft", "OWNER-PRIVATE-DRAFT" in text_of())

    # Seed a draft explicitly: a test must not depend on rows left by an earlier run.
    t().store.save_draft(40802006, "Test project", 55.0, 7, "draft body")
    SENT.clear()
    app.handle(update_text("price 40802006 40 5"))
    ok &= check("price edit updates the draft",
                t().store.draft(40802006)["amount"] == 40.0
                and t().store.draft(40802006)["period"] == 5)
    ok &= check("price edit returns a send button",
                any(b["callback_data"] == "send:40802006" for b in buttons()))

    SENT.clear()
    app.handle(update_cb("send:99999999"))
    ok &= check("send for an unknown draft warns, never posts", "ذخیره نشده" in text_of())

    SENT.clear()
    app.handle(update_cb("drop:40802006"))
    ok &= check("drop marks the draft discarded",
                t().store.draft(40802006)["status"] == "discarded")
    t().store.set_draft_status(40802006, "pending")

    # -------------------------------------------------------- menu button routing
    MENUS.clear()
    app.handle(update_text("/menu"))
    ok &= check("/menu re-sends the keyboard", len(MENUS) == 1)

    for label, needle in [("📊 وضعیت", "وضعیت ربات"), ("⚙️ تنظیمات", "تنظیمات"),
                          ("🔄 اسکن خودکار", "اسکن خودکار"), ("📋 لاگ", "آخرین رویدادها")]:
        SENT.clear()
        app.handle(update_text(label))
        ok &= check(f"button {label} routes", bool(SENT) and needle in text_of())

    # /logs must actually contain formatted records — a bad log format string makes
    # logging swallow the error and the panel silently stays empty.
    logging.basicConfig(level=logging.INFO)
    logbuf.install()
    logging.getLogger("freelancer_manager.test").info("canary-in-the-log")
    SENT.clear()
    app.handle(update_text("/logs"))
    ok &= check("/logs shows real records", "canary-in-the-log" in text_of())

    SENT.clear()
    app.handle(update_text("🧹 پاک‌سازی"))
    ok &= check("cleanup asks before deleting",
                bool(SENT)
                and any(b["callback_data"] == "confirmed:" for b in buttons()))

    # ------------------------------------ settings: per-chat prompt and per-chat value
    SENT.clear()
    app.handle(update_text("⚙️ تنظیمات"))
    ok &= check("settings lists every knob",
                all(f"cfg:{k}" in {b['callback_data'] for b in buttons()} for k in
                    ["budget_min", "budget_max", "max_bids", "shortlist", "interval",
                     "period", "milestone", "scout"]))

    SENT.clear()
    app.handle(update_cb("cfg:interval"))
    ok &= check("picking a knob asks for a value", app.waiting.get(str(OWNER)) == "interval")

    SENT.clear()
    app.handle(update_text("45", OTHER))
    ok &= check("another chat's message cannot answer the owner's prompt",
                app.waiting.get(str(OWNER)) == "interval")

    SENT.clear()
    app.handle(update_text("45"))
    ok &= check("typed value is stored", t().rt["interval"] == 45)
    ok &= check("prompt is cleared afterwards", app.waiting.get(str(OWNER)) is None)
    ok &= check("value survives a reload", Runtime.load(t().store)["interval"] == 45)
    ok &= check("the other chat kept its own interval", t(OTHER).rt["interval"] == 30)

    SENT.clear()
    app.handle(update_cb("cfg:budget_min"))
    SENT.clear()
    app.handle(update_text("not-a-number"))
    ok &= check("bad setting is rejected, old value kept",
                t().rt["budget_min"] == 10.0 and "نامعتبر" in text_of())

    SENT.clear()
    app.handle(update_text("/cancel"))
    ok &= check("/cancel clears a pending prompt", app.waiting.get(str(OWNER)) is None)

    # Restore the default so nothing later depends on the value above.
    t().rt.values["interval"] = 30
    t().rt.save()

    # ------------------------------------------------------- cleanup confirm path
    before = len(t().store.seen_ids())
    t().store.mark_seen(99900001, "test")
    SENT.clear()
    app.handle(update_cb("confirmed:"))
    ok &= check("confirmed cleanup clears seen ids", len(t().store.seen_ids()) == 0)
    ok &= check("cleanup left the other chat alone", len(t(OTHER).store.seen_ids()) == 0)
    t().store.mark_seen(99900001, "restored") if before else None

    # -------------------------------------------------------------- /users control
    SENT.clear()
    app.handle(update_text("/users"))
    ok &= check("/users lists both chats",
                str(OWNER) in text_of() and str(OTHER) in text_of())

    SENT.clear()
    app.handle(update_text(f"/users block {OTHER}"))
    ok &= check("owner can block a chat", app.db.user(str(OTHER))["role"] == "blocked")

    SENT.clear(); TO.clear()
    app.handle(update_text("/scan", OTHER))
    ok &= check("a blocked chat gets no reply at all", SENT == [])

    SENT.clear()
    app.handle(update_text(f"/users unblock {OTHER}"))
    ok &= check("owner can unblock a chat", app.db.user(str(OTHER))["role"] == "member")

    SENT.clear()
    app.handle(update_text(f"/users block {OWNER}"))
    ok &= check("owner cannot block itself", app.db.user(str(OWNER))["role"] == "owner")

    SENT.clear()
    app.handle(update_text(f"/users block {NEWCOMER}", OTHER))
    ok &= check("a member cannot block anyone",
                "فقط مالک" in text_of() and app.db.user(str(NEWCOMER)) is None)

    SENT.clear()
    app.handle(update_text("/start", NEWCOMER))
    ok &= check("a brand-new chat is registered as a member",
                app.db.user(str(NEWCOMER)) is not None
                and app.db.user(str(NEWCOMER))["role"] == "member")

    # ------------------------------------------- account: identity is per-tenant data
    SENT.clear()
    app.handle(update_text("/account user someuser"))
    ok &= check("account username is stored", t().acc.username == "someuser")

    SENT.clear()
    app.handle(update_text("/account uid 12345"))
    ok &= check("account id is stored", t().acc.user_id == "12345")
    ok &= check("account id did not leak to the other chat",
                app.tenant(str(OTHER)).acc.user_id == "")

    SENT.clear()
    app.handle(update_text("/account cookies"))
    ok &= check("cookie prompt opens", app.waiting.get(str(OWNER)) == "account:cookies")

    SENT.clear()
    app.handle(update_text("GETAFREE_USER_ID=12345; foo=bar"))
    ok &= check("incomplete cookie blob is refused",
                "ناقص" in text_of() and app.waiting.get(str(OWNER)) is None)

    blob = "GETAFREE_USER_ID=12345; GETAFREE_AUTH_HASH_V2=abc%3D; XSRF-TOKEN=xyz"
    parsed = parse_cookies(blob)
    ok &= check("cookie parser accepts a header line",
                parsed["GETAFREE_USER_ID"] == "12345" and parsed["XSRF-TOKEN"] == "xyz")
    ns = parse_cookies("freelancer.com\tTRUE\t/\tTRUE\t0\tGETAFREE_USER_ID\t12345")
    ok &= check("cookie parser accepts a Netscape dump",
                ns.get("GETAFREE_USER_ID") == "12345")

    # The cookie decides whose Freelancer account every later request acts as, so the
    # test asserts the row it landed in, not just the confirmation text.
    App.cmd_session = lambda self, tenant: None  # no network call here
    SENT.clear()
    app.handle(update_text("/account cookies"))
    app.handle(update_text(blob))
    ok &= check("cookie is stored in the caller's own tenant",
                t().store.get_meta("cookies", {}).get("GETAFREE_USER_ID") == "12345")
    ok &= check("cookie did not reach the other chat",
                t(OTHER).store.get_meta("cookies", None) is None)

    # --------------------------------------------------------------- /autoscan
    SENT.clear()
    app.handle(update_text("/autoscan off"))
    ok &= check("autoscan can be turned off", t().rt["scout"] is False)
    SENT.clear()
    app.handle(update_text("/autoscan on"))
    ok &= check("autoscan can be turned on", t().rt["scout"] is True)
    ok &= check("the other chat's autoscan was untouched",
                t(OTHER).rt["scout"] is True)

    # ----------------------------------------- /ai: per-chat endpoint, key and model
    SENT.clear()
    app.handle(update_text("/ai"))
    ok &= check("/ai shows settings",
                "تنظیمات AI" in text_of() and "Base URL" in text_of())

    SENT.clear()
    app.handle(update_text("/ai model some-test-model-1"))
    ok &= check("/ai model persists",
                t().ai.model == "some-test-model-1"
                and t().store.get_meta("ai_settings")["model"] == "some-test-model-1")
    ok &= check("the other chat kept the default model",
                t(OTHER).ai.model == default_ai.model)

    SENT.clear()
    app.handle(update_text("/ai base https://example.invalid/v1/"))
    ok &= check("/ai base trims trailing slash and saves",
                t().ai.base_url == "https://example.invalid/v1")

    SENT.clear()
    app.handle(update_text("/ai temp 0.9"))
    ok &= check("/ai temp accepts a float", t().ai.temperature == 0.9)

    SENT.clear()
    app.handle(update_text("/ai temp not-a-number"))
    ok &= check("/ai rejects a bad number",
                "نامعتبر" in text_of() and t().ai.temperature == 0.9)

    SENT.clear()
    app.handle(update_text("/ai nonsense 1"))
    ok &= check("/ai rejects an unknown field", "زیردستور ناشناخته" in text_of())

    reloaded = AISettings.load(t().store)
    ok &= check("settings survive a restart",
                reloaded.model == "some-test-model-1"
                and reloaded.base_url == "https://example.invalid/v1")

    SENT.clear()
    app.handle(update_text("/ai reset"))
    ok &= check("/ai reset restores defaults", "پیش‌فرض" in text_of())
    ok &= check("reset really cleared the model", t().ai.model == default_ai.model)

    # ---------------------------------------------------------------- /profile
    SENT.clear()
    app.handle(update_text("/profile"))
    ok &= check("/profile with no argument shows the empty intro", "معرفی تو" in text_of())
    SENT.clear()
    app.handle(update_text("/profile Python and Laravel, 8 years"))
    ok &= check("/profile is stored per chat",
                "Laravel" in t().profile and t(OTHER).profile == "")

    # --------------------------------------------------------------- leak sweep
    # No row may hold data for a chat id that is not a real user, so a typo in a
    # tenant key cannot silently create a second world nobody can log into.
    rows = app.db._all("SELECT tenant, project_id FROM drafts")
    ok &= check("every draft row belongs to a real user",
                all(app.db.user(r["tenant"]) is not None for r in rows))

    # ------------------------------------------------------------- deployment
    ok &= deploy_contract(cfg, app, t)

    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


def deploy_contract(cfg, app, t) -> bool:
    """The bot has to come up in a container, so the container's assumptions are tested.

    These are the traps that only bite on a rebuilt image: an owner binding that
    lives in the image instead of .env, a heartbeat that never advances, a compose
    file that starts two pollers, or a Dockerfile that runs as root and therefore
    cannot write the mounted state.
    """
    import json
    import os
    import time as clock
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ok = True

    # 1. A bound owner must survive a container rebuild: FL_CHAT_ID wins over the
    #    baked secrets/chat_id.txt, and adopting a chat must not rewrite that file —
    #    in a container it sits inside the image and the write would be lost.
    old_env = os.environ.get("FL_CHAT_ID")
    try:
        os.environ["FL_CHAT_ID"] = "424242"
        pinned = Config()
        ok &= check("FL_CHAT_ID is read from the environment",
                    pinned.chat_id == "424242" and pinned.chat_id_from_env)
        # A pin must not be overwritten by the first chat that talks.
        _real_save_chat_id(pinned, "999999")
        ok &= check("save_chat_id does not change a pinned owner",
                    pinned.chat_id == "424242")
    finally:
        if old_env is None:
            os.environ.pop("FL_CHAT_ID", None)
        else:
            os.environ["FL_CHAT_ID"] = old_env

    # 2. Heartbeat: the probe is what Docker uses, so it must pass for a live loop
    #    and fail for a stale one instead of always reporting healthy.
    import importlib
    __main__ = importlib.import_module("freelancer_manager.__main__")
    beat = cfg.health_path
    beat.write_text(str(int(clock.time())), encoding="utf-8")
    ok &= check("health passes on a fresh heartbeat", __main__.health(cfg) == 0)
    beat.write_text(str(int(clock.time()) - 3600), encoding="utf-8")
    ok &= check("health fails on a stale heartbeat", __main__.health(cfg) == 1)
    beat.unlink()
    ok &= check("health fails with no heartbeat at all", __main__.health(cfg) == 1)

    # The long-poll must return well inside the probe window, otherwise a quiet bot
    # goes stale-and-unhealthy and gets restarted for doing nothing wrong.
    import re
    loop = (root / "freelancer_manager" / "bot.py").read_text(encoding="utf-8")
    poll = re.search(r"self\.bot\.updates\(offset, timeout=(\d+)\)", loop)
    ok &= check("poll loop uses a bounded long-poll timeout", poll is not None)
    ok &= check("heartbeat window is twice the poll timeout",
                poll is not None and __main__.HEALTH_MAX_AGE >= 2 * int(poll.group(1)))

    # 3. Compose/Dockerfile: one poller, a probe, a non-root user, state mounted.
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    ok &= check("compose runs exactly one bot service",
                compose.count("container_name: freelancer-bot") == 1)
    ok &= check("compose mounts state instead of baking it",
                "./data:/app/data" in compose and "./secrets:/app/secrets:ro" in compose)
    ok &= check("compose has no published ports (a poller needs none)",
                "ports:" not in compose)
    ok &= check("compose restarts after a crash", "restart: unless-stopped" in compose)
    ok &= check("image declares a healthcheck", "HEALTHCHECK" in dockerfile)
    ok &= check("healthcheck calls the same probe the suite exercises",
                "freelancer_manager\", \"health\"" in dockerfile)
    ok &= check("container does not run as root", "USER bot" in dockerfile)

    # 4. A test run must never touch the deployed database: the sandbox is the proof.
    ok &= check("suite writes only inside its sandbox",
                str(app.cfg.db_path).startswith(tempfile.gettempdir()))

    return ok


if __name__ == "__main__":
    raise SystemExit(main())
