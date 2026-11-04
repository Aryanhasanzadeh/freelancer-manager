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
from freelancer_manager.tg import Bot  # noqa: E402

SENT: list[tuple[str, list | None]] = []


def fake_send(self, text, keyboard=None, preview=False):  # noqa: ANN001
    SENT.append((text, keyboard))
    return len(SENT)


Bot.send = fake_send  # type: ignore[method-assign]
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
    app.handle(update_text("/start"))
    ok &= check("/start replies", len(SENT) == 1 and "scan" in SENT[0][0])
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

    print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
