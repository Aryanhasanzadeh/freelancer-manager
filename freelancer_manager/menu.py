"""The management menu: one persistent keyboard that reaches every part of the bot.

Design rules:
- Buttons send their label as text, so the dispatcher has no second routing table to
  keep in sync — `handle()` sees a plain message either way.
- Text commands keep working (`/scan`, `/ai key ...`); this module only adds a front end.
- Anything destructive asks first via an inline confirm button.
"""

from __future__ import annotations

import time

from .runtime import SPEC

ROW_MAIN = ["🔎 اسکن", "📝 پیش‌نویس‌ها"]
ROW_MAIN2 = ["📊 وضعیت", "⚙️ تنظیمات"]
ROW_MAIN3 = ["🤖 هوش مصنوعی", "🔑 اکانت فریلنسر"]
ROW_MAIN4 = ["📋 لاگ", "👥 کاربران"]
ROW_MAIN5 = ["🧹 پاک‌سازی", "🔄 اسکن خودکار", "🧑‍💻 معرفی من"]

MENU_ROWS = [ROW_MAIN, ROW_MAIN2, ROW_MAIN3, ROW_MAIN4, ROW_MAIN5]

# Button label -> command the dispatcher already understands.
LABEL_TO_CMD = {
    "🔎 اسکن": "/scan",
    "📝 پیش‌نویس‌ها": "/drafts",
    "📊 وضعیت": "/status",
    "⚙️ تنظیمات": "/settings",
    "🤖 هوش مصنوعی": "/ai",
    "🔑 اکانت فریلنسر": "/account",
    "👥 کاربران": "/users",
    "🧑‍💻 معرفی من": "/profile",
    "🔄 اسکن خودکار": "/autoscan",
    "📋 لاگ": "/logs",
    "🧹 پاک‌سازی": "/cleanup",
}

LABELS = [b for row in MENU_ROWS for b in row]


def settings_keyboard() -> list[list[dict]]:
    """One button per editable setting, two per row."""
    items = list(SPEC.items())
    rows, row = [], []
    for key, (_typ, _env, _default, label) in items:
        row.append({"text": label, "callback_data": f"cfg:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def status_text(t) -> str:
    """One tenant's numbers only — never the process-wide totals."""
    s = t.store
    drafts = s.pending_drafts()
    lines = [
        "📊 <b>وضعیت ربات</b>",
        "",
        f"پروژه‌های دیده‌شده: <b>{len(s.seen_ids())}</b>",
        f"پیش‌نویس منتظر تأیید: <b>{len(drafts)}</b>",
        f"bid ثبت‌شده: <b>{s.bid_count()}</b>",
        f"اسکن خودکار: <b>{'روشن' if t.rt['scout'] else 'خاموش'}</b>"
        f" · هر {t.rt['interval']} دقیقه",
        f"فاصلهٔ آخرین اسکن: {_ago(s.get_meta('last_scan', 0))}",
        f"آخرین اسکن: <b>{s.get_meta('last_scan_found', '—')}</b> پروژهٔ واجد",
        "",
        f"AI: <code>{t.ai.model}</code> @ <code>{t.ai.base_url}</code>",
        f"بودجه: ${t.rt['budget_min']:g}–${t.rt['budget_max']:g}"
        f" · حداکثر {t.rt['max_bids']} پیشنهاد رقیب",
    ]
    return "\n".join(lines)


def _ago(ts) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "—"
    if not ts:
        return "—"
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff} ثانیه پیش"
    if diff < 3600:
        return f"{diff // 60} دقیقه پیش"
    if diff < 86400:
        return f"{diff // 3600} ساعت پیش"
    return f"{diff // 86400} روز پیش"


def settings_text(rt) -> str:
    return "⚙️ <b>تنظیمات</b>\n\n" + rt.summary() + "\n\nروی هر مورد بزن و مقدار تازه را بفرست."
