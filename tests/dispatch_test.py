"""Synthetic Telegram updates through the real dispatcher, with outbound calls captured.

Usage: python3 tests/dispatch_test.py
No network write to Telegram, no bid placed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freelancer_manager.bot import App  # noqa: E402
from freelancer_manager.config import Config  # noqa: E402
from freelancer_manager import menu as menus  # noqa: E402
from freelancer_manager.runtime import Runtime  # noqa: E402
from freelancer_manager.tg import Bot  # noqa: E402

SENT: list[tuple[str, list | None]] = []


MENUS: list[tuple[list[list[str]], str]] = []


def fake_send(self, text, keyboard=None, preview=False):  # noqa: ANN001
    SENT.append((text, keyboard))
    return len(SENT)


def fake_menu(self, rows, text="منو"):  # noqa: ANN001
    MENUS.append((rows, text))
    return 1


Bot.menu = fake_menu  # type: ignore[method-assign]
Bot.hide_menu = lambda self, text="": None  # type: ignore[method-assign]


Bot.send = fake_send  # type: ignore[method-assign]
# Binding is verified in memory; never let a synthetic chat id reach secrets/chat_id.txt.
Config.save_chat_id = lambda self, chat_id: setattr(self, "chat_id", str(chat_id))  # type: ignore[method-assign]
Bot.answer_callback = lambda self, *a, **k: None  # type: ignore[method-assign]

CHAT = {"id": 999001, "type": "private"}


def update_text(text: str) -> dict:
    return {"update_id": 1, "message": {"message_id": 1, "text": text, "chat": CHAT,
                                        "from": {"id": 999001}}}


def update_cb(data: str) -> dict:
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data,
                                               "message": {"message_id": 1, "chat": CHAT}}}


def check(label: str, cond: bool) -> bool:
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    return cond


def main() -> int:
    cfg = Config()
    cfg.chat_id = "dry-run"
    app = App(cfg)
    ok = True

    SENT.clear()
    MENUS.clear()
    app.handle(update_text("/start"))
    ok &= check("/start opens the menu", len(MENUS) == 1 and len(MENUS[0][0]) == 4)
    # Compare against the module's own labels: ZWNJ in Persian words makes literals brittle.
    ok &= check("/start lists every section",
                [b for row in MENUS[0][0] for b in row] == menus.LABELS)
    ok &= check("every menu button maps to a command",
                all(b in menus.LABEL_TO_CMD for b in menus.LABELS))
    ok &= check("/start binds chat id", app.cfg.chat_id == "999001")

    SENT.clear()
    app.handle(update_text("not a command"))
    ok &= check("unrelated text is ignored", SENT == [])

    SENT.clear()
    app.handle(update_text("price"))
    ok &= check("bare price is ignored", SENT == [])

    SENT.clear()
    app.handle(update_text("/bids"))
    ok &= check("/bids lists live bids", "bid‌های من" in SENT[0][0] and "#" in SENT[0][0])

    # A draft for a known project already exists from the dry run.
    SENT.clear()
    app.handle(update_text("price 40802006 40 5"))
    ok &= check("price edit updates the draft",
                app.store.draft(40802006)["amount"] == 40.0 and
                app.store.draft(40802006)["period"] == 5)
    ok &= check("price edit returns a send button",
                any(b["callback_data"] == "send:40802006" for row in SENT[0][1] for b in row))

    SENT.clear()
    app.handle(update_cb("send:99999999"))
    ok &= check("send for an unknown draft warns, never posts", "ذخیره نشده" in SENT[0][0])

    SENT.clear()
    app.handle(update_cb("drop:40802006"))
    ok &= check("drop marks the draft discarded",
                app.store.draft(40802006)["status"] == "discarded")

    # Restore the draft so the real flow can still use it.
    app.store.set_draft_status(40802006, "pending")

    # --- menu: every button must route, not just render
    MENUS.clear()
    app.handle(update_text("/menu"))
    ok &= check("/menu re-sends the keyboard", len(MENUS) == 1)

    for label, needle in [
        ("📊 وضعیت", "وضعیت ربات"),
        ("⚙️ تنظیمات", "تنظیمات"),
        ("📋 لاگ", "آخرین رویدادها"),
    ]:
        SENT.clear()
        app.handle(update_text(label))
        ok &= check(f"button {label} routes", bool(SENT) and needle in SENT[0][0])

    SENT.clear()
    app.handle(update_text("🧹 پاک‌سازی"))
    ok &= check("cleanup asks before deleting",
                bool(SENT) and any(b["callback_data"] == "confirmed:" for row in SENT[0][1] for b in row))

    # --- settings: a button opens a prompt, the next message is the value
    SENT.clear()
    app.handle(update_text("⚙️ تنظیمات"))
    buttons = {b["callback_data"]: b["text"] for row in SENT[0][1] for b in row}
    ok &= check("settings lists every knob",
                all(f"cfg:{k}" in buttons for k in
                    ["budget_min", "budget_max", "max_bids", "shortlist", "interval", "period", "milestone", "scout"]))

    SENT.clear()
    app.handle(update_cb("cfg:interval"))
    ok &= check("picking a knob asks for a value", app.awaiting == "interval")

    SENT.clear()
    app.handle(update_text("45"))
    ok &= check("typed value is stored", app.rt["interval"] == 45)
    ok &= check("prompt is cleared afterwards", app.awaiting is None)
    ok &= check("value survives a reload", Runtime.load(app.store)["interval"] == 45)

    SENT.clear()
    app.handle(update_cb("cfg:budget_min"))
    SENT.clear()
    app.handle(update_text("not-a-number"))
    ok &= check("bad setting is rejected, old value kept",
                app.rt["budget_min"] == 10.0 and "نامعتبر" in SENT[0][0])

    SENT.clear()
    app.handle(update_text("/cancel"))
    ok &= check("/cancel clears a pending prompt", app.awaiting is None)

    # Restore defaults so the file on disk is unchanged by a test run.
    app.rt.values["interval"] = 30
    app.rt.save()

    # --- /cleanup confirm path
    before = len(app.store.seen_ids())
    app.store.mark_seen(99900001, "test")
    SENT.clear()
    app.handle(update_cb("confirmed:"))
    ok &= check("confirmed cleanup clears seen ids", len(app.store.seen_ids()) == 0)
    app.store.mark_seen(99900001, "restored") if before else None

    # --- /ai: the endpoint must be editable from the chat and survive a restart
    SENT.clear()
    app.handle(update_text("/ai"))
    ok &= check("/ai shows settings", "تنظیمات AI" in SENT[0][0] and "Base URL" in SENT[0][0])

    SENT.clear()
    app.handle(update_text("/ai model some-test-model-1"))
    ok &= check("/ai model persists",
                app.ai.model == "some-test-model-1" and app.store.get_meta("ai_settings")["model"] == "some-test-model-1")

    SENT.clear()
    app.handle(update_text("/ai base https://example.invalid/v1/"))
    ok &= check("/ai base trims trailing slash and saves",
                app.ai.base_url == "https://example.invalid/v1")

    SENT.clear()
    app.handle(update_text("/ai temp 0.9"))
    ok &= check("/ai temp accepts a float", app.ai.temperature == 0.9)

    SENT.clear()
    app.handle(update_text("/ai temp not-a-number"))
    ok &= check("/ai rejects a bad number", "نامعتبر" in SENT[0][0] and app.ai.temperature == 0.9)

    SENT.clear()
    app.handle(update_text("/ai nonsense 1"))
    ok &= check("/ai rejects an unknown field", "فیلد ناشناخته" in SENT[0][0])

    # A reload from the store must see the same values: this is what a restart does.
    from freelancer_manager.aisettings import AISettings
    reloaded = AISettings.load(app.store)
    ok &= check("settings survive a restart",
                reloaded.model == "some-test-model-1" and reloaded.base_url == "https://example.invalid/v1")

    SENT.clear()
    app.handle(update_text("/ai reset"))
    ok &= check("/ai reset restores defaults", "پیش‌فرض" in SENT[0][0])
    app.ai = AISettings.load(app.store)
    ok &= check("reset really cleared the model",
                app.ai.model == "glm-5.3-flash" and app.ai.base_url == "https://ai.metft.ir/v1")

    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
