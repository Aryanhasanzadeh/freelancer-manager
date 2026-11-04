"""The Telegram bot: scanning, drafting, confirming and placing bids.

Everything about Freelancer.com happens here. Nothing is sent to any other chat,
and no bid is submitted without an explicit button press on the draft.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from .api import Client, FreelancerError
from .config import SECRETS, Config
from . import logbuf, menu as menus
from .aisettings import AISettings, ping as ai_ping
from .runtime import Runtime
from .proposal import DraftError, draft_proposal
from .scan import TERMS, Filters, normalize, rank, reject_reason
from .store import Store
from .tg import Bot, TelegramError, esc

log = logging.getLogger(__name__)

ID_RE = re.compile(r"\b(40\d{6})\b")


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bot = Bot(cfg)
        self.api = Client(cfg)
        self.store = Store(cfg.db_path)
        self.ai = AISettings.load(self.store)
        self.rt = Runtime.load(self.store)
        self.rt.apply(cfg)
        self.filters = Filters(cfg.budget_min, cfg.budget_max, cfg.max_bids)
        # Set when a settings button asked for a value; the next message is that value.
        self.awaiting: str | None = None

    # ------------------------------------------------------------- scanning
    def scan(self) -> tuple[list[dict], int]:
        pool: dict[int, dict] = {}
        for term in TERMS:
            try:
                for p in self.api.fetch_projects(term):
                    pool[p["id"]] = p
            except FreelancerError as exc:
                log.warning("fetch %s failed: %s", term, exc)
        seen = self.store.seen_ids()
        now = time.time()
        found = [
            normalize(p, now)
            for p in pool.values()
            if p["id"] not in seen and reject_reason(p, self.filters, now) is None
        ]
        # Surfaced in /status so a silent scanner is distinguishable from a broken one.
        self.store.set_meta("last_scan", int(now))
        self.store.set_meta("last_scan_pool", len(pool))
        return rank(found), len(pool)

    def format_project(self, p: dict) -> str:
        lines = [
            f"<b>{esc(p['title'])}</b>",
            esc(p["url"]),
            f"💰 ${p['budget_min_usd']:.0f}–${p['budget_max_usd']:.0f} "
            f"({esc(p['orig_budget'])}) · {p['bids']} bid · avg ${p['bid_avg_usd']:.0f} · "
            f"{p['days_left']:.1f} روز مانده",
            f"🛠 {esc(', '.join(p['matched_skills']))}",
            esc(p["description"][:500]) + ("…" if len(p["description"]) > 500 else ""),
        ]
        return "\n".join(lines)

    def cmd_scan(self) -> None:
        try:
            self.api.verify_session()
        except FreelancerError as exc:
            self.bot.send(f"⚠️ سشن فریلنسر معتبر نیست: <code>{esc(exc)}</code>")
            return
        try:
            items, pool_size = self.scan()
        except Exception as exc:  # noqa: BLE001 - never let a scan kill the bot
            log.exception("scan failed")
            self.bot.send(f"⚠️ اسکن شکست خورد: <code>{esc(exc)}</code>")
            return

        if not items:
            self.bot.send(f"این دور پروژهٔ خوبی پیدا نشد. (استخر: {pool_size})")
            return

        top = items[: self.cfg.shortlist]
        self.bot.send(f"🔎 <b>{len(items)}</b> پروژهٔ مناسب از {pool_size} پروژه.")
        for p in top:
            self.store.mark_seen(p["id"], p["title"])
            kb = [[{"text": f"✍️ پیشنهاد بده — ${p['budget_min_usd']:.0f}", "callback_data": f"draft:{p['id']}"}]]
            self.bot.send(self.format_project(p), keyboard=kb)

    # -------------------------------------------------------------- drafting
    def make_draft(self, pid: int, chat_note: str = "") -> None:
        try:
            project = self.api.project(pid)
        except (FreelancerError, KeyError, IndexError) as exc:
            self.bot.send(f"⚠️ پروژهٔ {pid} خوانده نشد: <code>{esc(exc)}</code>")
            return

        budget = project.get("budget") or {}
        rate = (project.get("currency") or {}).get("exchange_rate") or 1
        low_usd = float(budget.get("minimum") or 0) * float(rate)
        amount = round(max(low_usd, self.cfg.budget_min), 2)
        period = self.cfg.default_period
        skills = [j.get("name", "") for j in project.get("jobs", [])]

        try:
            text = draft_proposal(
                project.get("title", ""),
                project.get("description", ""),
                budget,
                skills,
                amount,
                period,
                self.ai,
            )
        except (DraftError, Exception) as exc:  # noqa: BLE001
            log.exception("draft failed")
            self.bot.send(f"⚠️ نوشتن پیشنهاد شکست خورد: <code>{esc(exc)}</code>")
            return

        self.store.save_draft(pid, project.get("title", ""), amount, period, text)
        kb = [
            [{"text": f"✅ تأیید و ارسال — ${amount:.0f} / {period} روز", "callback_data": f"send:{pid}"}],
            [{"text": "✏️ ویرایش قیمت", "callback_data": f"price:{pid}"},
             {"text": "🗑 دور بریز", "callback_data": f"drop:{pid}"}],
        ]
        body = (
            f"<b>پیش‌نویس پیشنهاد</b>\n"
            f"{esc(project.get('title', ''))}\n"
            f"https://www.freelancer.com/projects/{project.get('seo_url', '')}/details\n\n"
            f"{esc(text)}\n\n"
            f"<b>${amount:.0f}</b> · <b>{period} روز</b> · milestone {self.cfg.milestone}%"
        )
        if chat_note:
            body = f"<i>{esc(chat_note)}</i>\n" + body
        self.bot.send(body, keyboard=kb)

    # ---------------------------------------------------------------- bidding
    def send_bid(self, pid: int) -> None:
        d = self.store.draft(pid)
        if not d:
            self.bot.send(f"⚠️ پیش‌نویسی برای {pid} ذخیره نشده.")
            return
        try:
            res = self.api.place_bid(pid, d["amount"], d["period"], d["description"])
        except FreelancerError as exc:
            self.store.set_draft_status(pid, "failed")
            self.bot.send(f"⚠️ ارسال bid رد شد: <code>{esc(exc.code or exc)}</code>\n{esc(str(exc))}")
            return

        bid = res.get("bids", [{}])[0] if isinstance(res, dict) else {}
        bid_id = bid.get("id")
        self.store.record_bid(bid_id, pid, d["amount"], d["period"])
        self.store.set_draft_status(pid, "sent", bid_id)
        self.bot.send(
            f"✅ bid ارسال شد.\n"
            f"#{esc(bid_id)} · ${d['amount']:.0f} · {d['period']} روز\n"
            f"https://www.freelancer.com/projects/{pid}/"
        )

    # ---------------------------------------------------------------- menu
    def cmd_menu(self, first: bool = False) -> None:
        intro = (
            "🤖 <b>ربات فریلنسر — پنل مدیریت</b>\n\n"
            "از دکمه‌های پایین هر بخش را باز کن. دستورهای متنی هم کار می‌کنند:\n"
            "/scan /drafts /bids /status /settings /ai /session /logs /cleanup"
        ) if first else "منو در پایین صفحه فعال شد."
        self.bot.menu(menus.MENU_ROWS, text=intro)

    def cmd_status(self) -> None:
        self.bot.send(menus.status_text(self))

    def cmd_settings(self) -> None:
        self.bot.send(menus.settings_text(self.rt), keyboard=menus.settings_keyboard())

    def cmd_logs(self) -> None:
        lines = logbuf.tail(14) or ["(هنوز چیزی ثبت نشده)"]
        body = "\n".join(esc(line) for line in lines)
        self.bot.send(f"📋 <b>آخرین رویدادها</b>\n<pre>{body}</pre>")

    def cmd_session(self) -> None:
        """Cookies expire silently; prove the session by comparing the API's user."""
        try:
            user = self.api.verify_session()
            self.bot.send(f"🔑 سشن سالم است. کاربر: <code>{esc(user)}</code>")
        except (FreelancerError, Exception) as exc:  # noqa: BLE001
            self.bot.send(
                "❌ سشن فریلنسر کار نمی‌کند.\n"
                f"<code>{esc(exc)}</code>\n\n"
                f"فایل <code>secrets/cookies.txt</code> را از مرورگر تازه کن و ربات را ری‌استارت کن."
            )

    def cmd_cleanup(self, confirm: bool = False) -> None:
        if not confirm:
            self.bot.send(
                "🧹 <b>پاک‌سازی</b>\n"
                "لیست «دیده‌شده» خالی می‌شود تا اسکن بعدی دوباره پروژه‌های فعلی را پیدا کند.\n"
                "پیش‌نویس‌ها و bid‌ها دست نمی‌خورند.",
                keyboard=[[{"text": "✅ بله، خالی کن", "callback_data": "confirmed:"}],
                          [{"text": "انصراف", "callback_data": "cancel:"}]],
            )
            return
        removed = self.store.clear_seen()
        self.bot.send(f"✅ {removed} پروژه از لیست دیده‌شده پاک شد. اسکن بعدی دوباره پر می‌کند.")

    # ----------------------------------------------------------------- ai
    def cmd_ai(self, text: str) -> None:
        """`/ai` — show or edit the model endpoint. Lives in the chat, not in files."""
        # `key` and `token` are what the chat naturally types; `api_key` leaks the field name.
        aliases = {"key": "api_key", "token": "api_key", "apikey": "api_key"}
        parts = text.split(maxsplit=2)
        if len(parts) > 1:
            parts[1] = aliases.get(parts[1].lower(), parts[1])
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("", "show"):
            self.bot.send(
                self.ai.summary() + "\n\n"
                "تغییر:\n"
                "<code>/ai base https://api.example.com/v1</code>\n"
                "<code>/ai key sk-...</code>\n"
                "<code>/ai model gpt-4o-mini</code>\n"
                "<code>/ai temp 0.3</code>\n"
                "<code>/ai maxtokens 900</code>\n"
                "<code>/ai timeout 180</code>\n"
                "<code>/ai test</code> — یک پیام واقعی به مدل\n"
                "<code>/ai reset</code> — بازگشت به پیش‌فرض"
            )
            return

        if sub == "test":
            try:
                reply = ai_ping(self.ai)
                self.bot.send(f"✅ اتصال سالم است. پاسخ مدل: <code>{esc(reply[:40])}</code>")
            except Exception as exc:  # noqa: BLE001
                self.bot.send(f"❌ اتصال شکست خورد: <code>{esc(exc)}</code>")
            return

        if sub == "reset":
            self.ai = AISettings.load()
            self.ai.save(self.store)
            self.bot.send("↩️ تنظیمات AI به پیش‌فرض برگشت.\n" + self.ai.summary())
            return

        value = parts[2].strip() if len(parts) > 2 else ""
        if not value:
            self.bot.send("مقدار را بفرست. مثال: <code>/ai model gpt-4o-mini</code>")
            return

        field_map = {
            "base": "base_url", "url": "base_url", "base_url": "base_url",
            "key": "api_key", "token": "api_key", "api_key": "api_key",
            "model": "model",
            "temp": "temperature", "temperature": "temperature",
            "maxtokens": "max_tokens", "max_tokens": "max_tokens",
            "timeout": "timeout",
        }
        field = field_map.get(sub)
        if not field:
            self.bot.send("فیلد ناشناخته. یکی از: base, key, model, temp, maxtokens, timeout")
            return

        try:
            if field in ("temperature",):
                setattr(self.ai, field, float(value))
            elif field in ("max_tokens", "timeout"):
                setattr(self.ai, field, int(value))
            else:
                setattr(self.ai, field, value.rstrip("/"))
        except ValueError:
            self.bot.send(f"مقدار نامعتبر برای {sub}: <code>{esc(value)}</code>")
            return

        self.ai.save(self.store)
        # A key written from the chat also goes to secrets/ so a plain restart keeps it.
        if field == "api_key":
            p = Path(SECRETS / "ai_key.txt")
            p.write_text(self.ai.api_key, encoding="utf-8")
            p.chmod(0o600)

        self.bot.send("✅ ذخیره شد.\n" + self.ai.summary() + "\n\nبرای تست: <code>/ai test</code>")

    # ------------------------------------------------------------ dispatch
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            data = q.get("data", "")
            action, _, arg = data.partition(":")
            self.bot.answer_callback(q["id"])
            if action == "draft":
                self.make_draft(int(arg))
            elif action == "send":
                self.send_bid(int(arg))
            elif action == "drop":
                self.store.set_draft_status(int(arg), "discarded")
                self.bot.send(f"پیش‌نویس {arg} کنار گذاشته شد.")
            elif action == "price":
                self.bot.send(f"قیمت جدید را بفرست: <code>price {arg} 250 5</code>")
            elif action == "cfg":
                label = menus.SPEC[arg][3]
                self.awaiting = arg
                self.bot.send(
                    f"مقدار تازه برای <b>{esc(label)}</b> را بفرست.\n"
                    f"مقدار فعلی: <code>{self.rt[arg]}</code>\n"
                    "/cancel برای انصراف"
                )
            elif action == "confirmed":
                self.cmd_cleanup(confirm=True)
            elif action == "cancel":
                self.awaiting = None
                self.bot.send("لغو شد.")
            return

        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # Bind the chat on first contact instead of requiring a pre-known id.
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if chat_id and chat_id != self.cfg.chat_id:
            self.cfg.save_chat_id(chat_id)
            log.info("bound to chat %s", chat_id)

        # A menu button just sends its label; translate it to the real command.
        text = menus.LABEL_TO_CMD.get(text, text)
        low = text.lower()

        if low in ("/cancel", "cancel"):
            self.awaiting = None
            self.bot.send("لغو شد.")
            return

        # A pending settings prompt owns the next plain message.
        if self.awaiting and not low.startswith("/"):
            key = self.awaiting
            self.awaiting = None
            try:
                value = self.rt.set(key, text)
            except (ValueError, KeyError):
                self.bot.send("مقدار نامعتبر. دوباره از ⚙️ تنظیمات امتحان کن.")
                return
            self.rt.apply(self.cfg)
            self.filters = Filters(self.cfg.budget_min, self.cfg.budget_max, self.cfg.max_bids)
            self.bot.send(f"✅ {esc(menus.SPEC[key][3])} شد <b>{esc(value)}</b>")
            return

        if low.startswith("/start"):
            self.cmd_menu(first=True)
        elif low.startswith("/scan"):
            self.cmd_scan()
        elif low.startswith("/drafts"):
            rows = self.store.pending_drafts()
            if not rows:
                self.bot.send("پیش‌نویس منتظر تأییدی نیست.")
                return
            for r in rows:
                kb = [[{"text": f"✅ تأیید و ارسال — ${r['amount']:.0f}", "callback_data": f"send:{r['project_id']}"}]]
                self.bot.send(
                    f"<b>{esc(r['title'])}</b>\n${r['amount']:.0f} · {r['period']} روز\n"
                    f"{esc(r['description'][:600])}",
                    keyboard=kb,
                )
        elif low.startswith("/menu"):
            self.cmd_menu()
        elif low.startswith("/status"):
            self.cmd_status()
        elif low.startswith("/settings"):
            self.cmd_settings()
        elif low.startswith("/logs"):
            self.cmd_logs()
        elif low.startswith("/session"):
            self.cmd_session()
        elif low.startswith("/cleanup"):
            self.cmd_cleanup()
        elif low.startswith("/ai"):
            self.cmd_ai(text)
        elif low.startswith("/bids"):
            bids, _ = self.api.my_bids()
            if not bids:
                self.bot.send("bid ثبت‌شده‌ای نیست.")
                return
            body = "\n".join(
                f"#{b['id']} · ${b['amount']:.0f} · {b['period']} روز · پ‍روژه {b['project_id']}"
                for b in bids[:10]
            )
            self.bot.send(f"<b>bid‌های من</b>\n{body}")
        elif low.startswith("price "):
            parts = text.split()
            if len(parts) in (3, 4) and parts[1].isdigit() and parts[2].replace(".", "", 1).isdigit():
                pid = int(parts[1])
                amount = float(parts[2])
                period = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else self.cfg.default_period
                d = self.store.draft(pid)
                if d:
                    self.store.save_draft(pid, d["title"], amount, period, d["description"])
                    kb = [[{"text": f"✅ تأیید و ارسال — ${amount:.0f} / {period} روز",
                            "callback_data": f"send:{pid}"}]]
                    self.bot.send(f"قیمت به <b>${amount:.0f}</b> و <b>{period} روز</b> تغییر کرد.",
                                  keyboard=kb)
                else:
                    self.bot.send("پیش‌نویسی با این آیدی نیست.")
            else:
                self.bot.send("شکل درست: <code>price 40812345 250</code> یا <code>price 40812345 250 5</code>")
        elif ID_RE.search(text):
            self.make_draft(int(ID_RE.search(text).group(1)))

    # ------------------------------------------------------------- commands
    def register_commands(self) -> None:
        """Populate the Telegram command list so / is discoverable without the menu."""
        cmds = [
            ("menu", "پنل مدیریت و دکمه‌ها"),
            ("scan", "اسکن پروژه‌های جدید"),
            ("drafts", "پیش‌نویس‌های منتظر تأیید"),
            ("bids", "bid‌های ثبت‌شده"),
            ("status", "وضعیت و آمار"),
            ("settings", "تنظیم بودجه، فاصلهٔ اسکن و ..."),
            ("ai", "API، کلید و مدل"),
            ("session", "تست سشن فریلنسر"),
            ("logs", "آخرین رویدادها"),
            ("cleanup", "خالی کردن لیست دیده‌شده"),
        ]
        try:
            self.bot.call("setMyCommands",
                          commands=[{"command": c, "description": d} for c, d in cmds])
        except Exception:  # noqa: BLE001 - cosmetic, never blocks startup
            log.warning("setMyCommands failed", exc_info=True)

    # ----------------------------------------------------------------- loop
    def run(self) -> None:
        me = self.bot.call("getMe")
        log.info("bot @%s up", me.get("username"))
        self.register_commands()
        offset = int(self.store.get_meta("offset", 0) or 0)
        while True:
            try:
                for update in self.bot.updates(offset):
                    offset = update["update_id"] + 1
                    self.store.set_meta("offset", offset)
                    try:
                        self.handle(update)
                    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                        log.exception("update failed")
            except (TelegramError, OSError) as exc:
                log.warning("poll error: %s", exc)
                time.sleep(5)


def start_scout_thread(app: App, interval_min: int) -> None:
    """Periodic background scan. Silent when nothing is eligible, like the cron job was."""

    def loop() -> None:
        while True:
            if not app.rt["scout"]:
                # Toggled off from /settings; sleep in small steps so it reactivates fast.
                time.sleep(30)
                continue
            try:
                items, _ = app.scan()
                app.store.set_meta("last_scan_found", len(items))
                if items:
                    top = items[: app.cfg.shortlist]
                    app.bot.send(f"🔎 <b>{len(items)}</b> پروژهٔ جدید پیدا شد.")
                    for p in top:
                        app.store.mark_seen(p["id"], p["title"])
                        kb = [[{"text": f"✍️ پیشنهاد بده — ${p['budget_min_usd']:.0f}",
                                "callback_data": f"draft:{p['id']}"}]]
                        app.bot.send(app.format_project(p), keyboard=kb)
                else:
                    log.info("scout: nothing eligible")
            except Exception:  # noqa: BLE001
                log.exception("scout cycle failed")
            time.sleep((app.rt["interval"] or interval_min) * 60)

    t = threading.Thread(target=loop, name="scout", daemon=True)
    t.start()
