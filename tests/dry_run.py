"""Dry-run harness: exercises the real app code with Telegram output captured in memory.

Usage: python3 tests/dry_run.py [project_id]
Nothing is sent to Telegram and no bid is placed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freelancer_manager.bot import App  # noqa: E402
from freelancer_manager.config import Config  # noqa: E402
from freelancer_manager.tg import Bot  # noqa: E402

OUT: list[str] = []


def fake_send(self, text, keyboard=None, preview=False):  # noqa: ANN001
    OUT.append(text)
    print("\n----- MESSAGE -----")
    print(text)
    if keyboard:
        for row in keyboard:
            print("  [" + "] [".join(b["text"] + " -> " + b["callback_data"] for b in row) + "]")
    return len(OUT)


Bot.send = fake_send  # type: ignore[method-assign]
Bot.edit = lambda self, *a, **k: None  # type: ignore[method-assign]
Bot.answer_callback = lambda self, *a, **k: None  # type: ignore[method-assign]


def main() -> int:
    cfg = Config()
    cfg.chat_id = "dry-run"
    app = App(cfg)

    print("session:", app.api.verify_session())

    items, pool = app.scan()
    print(f"pool={pool} eligible={len(items)}")
    for p in items[:3]:
        print(f"  {p['id']} {p['bids']}b ${p['budget_min_usd']:.0f} {p['title'][:60]}")

    target = int(sys.argv[1]) if len(sys.argv) > 1 else (items[0]["id"] if items else None)
    if target:
        print(f"\n=== drafting {target} ===")
        app.make_draft(target)
        row = app.store.draft(target)
        print(f"\nstored draft: amount={row['amount']} period={row['period']} "
              f"status={row['status']} len={len(row['description'])}")

    print(f"\nmessages produced: {len(OUT)}")
    return 0 if OUT else 1


if __name__ == "__main__":
    raise SystemExit(main())
