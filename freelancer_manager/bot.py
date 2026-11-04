"""The Telegram bot: one process, one bot token, many independent users.

Multi-tenant rules this file enforces:

* Every update resolves to its own `Tenant` (chat id) *before* anything is read or
  written. There is no "current user" and no global data handle on `App`.
* `t.store` is the tenant's scoped store — the handlers physically cannot reach
  another user's rows, because the scoped store has no unscoped query.
* A stranger who writes is registered as `member` and told to connect their own
  Freelancer account. Membership gives them their own empty workspace, never the
  owner's. Only `owner` may block/unblock/remove users.
* The background scout walks the user table and builds a fresh tenant per user per
  tick, so no thread ever holds another user's cookies or client.

Nothing about Freelancer.com happens outside a tenant: no bid is submitted without an
explicit button press on that user's own draft.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from .api import FreelancerError
from .config import Config
from . import logbuf, menu as menus
from .account import parse_cookies
from .aisettings import AISettings, ping as ai_ping
from .proposal import draft_proposal
from .scan import TERMS, normalize, rank, reject_reason
from .store import Store
from .tenant import Tenant
from .tg import Bot, TelegramError, esc

log = logging.getLogger(__name__)

ID_RE = re.compile(r"\b(40\d{6})\b")

# The one chat id that owns process-wide state (poll offset), never user data.
GLOBAL = "__global__"


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bot = Bot(cfg)
        self.db = Store(cfg.db_path)
        # chat_id -> pending prompt (a settings key or "account:cookies"), per user.
        self.waiting: dict[str, str] = {}
        self.waiting_lock = threading.Lock()

    # --------------------------------------------------------------- tenants
    def tenant(self, chat_id: str) -> Tenant:
        return Tenant(self.cfg, self.db, chat_id)

    def touch(self, chat_id: str, name: str = "") -> tuple[Tenant, bool]:
        """Resolve (and register) the caller. Returns (tenant, registered_now)."""
        known = self.db.user(chat_id) is not None
        self.db.ensure_user(chat_id, name)
        t = self.tenant(chat_id)
        if not known and t.is_owner:
            self.cfg.save_chat_id(chat_id)
            log.info("first contact: %s is now the owner", chat_id)
        return t, not known

    # -------------------------------------------------------------- scanning
    def scan(self, t: Tenant) -> tuple[list[dict], int]:
        """Fetch and filter for one tenant, with that tenant's client and limits."""
        pool: dict[int, dict] = {}
        for term in TERMS:
            try:
                for p in t.api.fetch_projects(term):
                    pool[p["id"]] = p
            except FreelancerError as exc:
                log.warning("[%s] fetch %s failed: %s", t.chat, term, exc)
        seen = t.store.seen_ids()
        now = time.time()
        filters = t.filters
        found = [
            normalize(p, now)
            for p in pool.values()
            if p["id"] not in seen and reject_reason(p, filters, now) is None
        ]
        t.on_scan(len(found), len(pool))
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

    def cmd_scan(self, t: Tenant) -> None:
        if not t.cookies:
            self.bot.send("❌ اول اکانت فریلنسر خودت را وصل کن: <code>/account cookies</code>")
            return
        try:
            t.api.verify_session()
        except FreelancerError as exc:
            self.bot.send(
                "⚠️ اکانت فریلنسر تو معتبر نیست.\n"
                f"<code>{esc(exc)}</code>\n\n"
                "با <code>/account cookies</code> کوکی تازه بده."
            )
            return
        try:
            items, pool_size = self.scan(t)
        except Exception as exc:  # noqa: BLE001 - a scan must never kill the bot
            log.exception("scan failed for %s", t.chat)
            self.bot.send(f"⚠️ اسکن شکست خورد: <code>{esc(exc)}</code>")
            return

        if not items:
            self.bot.send(f"این دور پروژهٔ خوبی پیدا نشد. (استخر: {pool_size})")
            return

        top = items[: t.shortlist]
        self.bot.send(f"🔎 <b>{len(items)}</b> پروژهٔ مناسب از {pool_size} پروژه.")
        for p in top:
            t.store.mark_seen(p["id"], p["title"])
            kb = [[{"text": f"✍️ پیشنهاد بده — ${p['budget_min_usd']:.0f}",
                    "callback_data": f"draft:{p['id']}"}]]
            self.bot.send(self.format_project(p), keyboard=kb)

    # -------------------------------------------------------------- drafting
    def make_draft(self, t: Tenant, pid: int) -> None:
        try:
            project = t.api.project(pid)
        except (FreelancerError, KeyError, IndexError) as exc:
            self.bot.send(f"⚠️ پروژهٔ {pid} خوانده نشد: <code>{esc(exc)}</code>")
            return

        budget = project.get("budget") or {}
        rate = (project.get("currency") or {}).get("exchange_rate") or 1
        low_usd = float(budget.get("minimum") or 0) * float(rate)
        amount = round(max(low_usd, t.rt["budget_min"]), 2)
        period = t.period
        skills = [j.get("name", "") for j in project.get("jobs", [])]

        try:
            text = draft_proposal(
                project.get("title", ""),
                project.get("description", ""),
                budget,
                skills,
                amount,
                period,
                t.ai,
                profile=t.profile,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("draft failed for %s", t.chat)
            self.bot.send(f"⚠️ نوشتن پیشنهاد شکست خورد: <code>{esc(exc)}</code>")
            return

        t.store.save_draft(pid, project.get("title", ""), amount, period, text)
        kb = [
            [{"text": f"✅ تأیید و ارسال — ${amount:.0f} / {period} روز",
              "callback_data": f"send:{pid}"}],
            [{"text": "✏️ ویرایش قیمت", "callback_data": f"price:{pid}"},
             {"text": "🗑 دور بریز", "callback_data": f"drop:{pid}"}],
        ]
        self.bot.send(
            f"<b>پیش‌نویس پیشنهاد</b>\n"
            f"{esc(project.get('title', '')[:120])}\n"
            f"${amount:.0f} · {period} روز · https://www.freelancer.com/projects/{pid}/\n\n"
            f"<pre>{esc(text)}</pre>",
            keyboard=kb,
        )

    def send_bid(self, t: Tenant, pid: int) -> None:
        d = t.store.draft(pid)
        if not d:
            self.bot.send(f"⚠️ پیش‌نویسی برای {pid} ذخیره نشده.")
            return
        try:
            res = t.api.place_bid(pid, d["amount"], d["period"], d["description"])
        except FreelancerError as exc:
            t.store.set_draft_status(pid, "failed")
            self.bot.send(f"⚠️ ارسال bid رد شد: <code>{esc(getattr(exc, 'code', '') or exc)}</code>\n"
                          f"{esc(str(exc))}")
            return

        bid = res.get("bids", [{}])[0] if isinstance(res, dict) else {}
        bid_id = bid.get("id")
        t.store.record_bid(bid_id, pid, d["amount"], d["period"])
        t.store.set_draft_status(pid, "sent", bid_id)
        self.bot.send(
            "✅ bid ارسال شد.\n"
            f"#{esc(bid_id)} · ${d['amount']:.0f} · {d['period']} روز\n"
            f"https://www.freelancer.com/projects/{pid}/"
        )

    # ------------------------------------------------------------------- menu
    def cmd_menu(self, t: Tenant, first: bool = False) -> None:
        intro = (
            "🤖 <b>ربات فریلنسر — کارگاه شخصی تو</b>\n\n"
            "چندکاربره: هر کس اکانت فریلنسر، پروژه‌ها و پیش‌نویس‌های خودش را دارد و "
            "دادهٔ هیچ کسی به دیگری نمی‌رسد.\n\n"
            "اگر تازه‌ای، اول <code>/account cookies</code> را بزن و کوکی اکانت خودت را بده، "
            "و با <code>/profile</code> معرفی خودت را بنویس تا پیشنهادها مال تو باشند.\n\n"
            "/scan /drafts /bids /status /settings /ai /session /logs /users"
        ) if first else "منو در پایین صفحه فعال شد."
        self.bot.menu(menus.MENU_ROWS, text=intro)

    def cmd_status(self, t: Tenant) -> None:
        self.bot.send(menus.status_text(t))

    def cmd_settings(self, t: Tenant) -> None:
        self.bot.send(menus.settings_text(t.rt), keyboard=menus.settings_keyboard())

    def cmd_logs(self) -> None:
        lines = logbuf.tail(14) or ["(هنوز چیزی ثبت نشده)"]
        body = "\n".join(esc(line) for line in lines)
        self.bot.send(f"📋 <b>آخرین رویدادها</b>\n<pre>{body}</pre>")

    def cmd_drafts(self, t: Tenant) -> None:
        rows = t.store.pending_drafts()
        if not rows:
            self.bot.send("پیش‌نویس منتظر تأییدی نیست.")
            return
        for d in rows:
            kb = [[{"text": f"✅ ارسال — ${d['amount']:.0f}",
                    "callback_data": f"send:{d['project_id']}"}],
                  [{"text": "🗑 دور بریز", "callback_data": f"drop:{d['project_id']}"}]]
            self.bot.send(
                f"📝 <b>{esc(d['title'])}</b>\n"
                f"${d['amount']:.0f} · {d['period']} روز · پروژه {d['project_id']}\n\n"
                f"<pre>{esc(d['description'])}</pre>",
                keyboard=kb,
            )

    def cmd_cleanup(self, t: Tenant, confirm: bool = False) -> None:
        if not confirm:
            self.bot.send(
                "🧹 <b>پاک‌سازی</b>\n"
                "لیست «دیده‌شده» تو خالی می‌شود تا اسکن بعدی دوباره پروژه‌های فعلی را پیدا کند.\n"
                "پیش‌نویس‌ها و bid‌ها دست نمی‌خورند.",
                keyboard=[[{"text": "✅ بله، خالی کن", "callback_data": "confirmed:"}],
                          [{"text": "انصراف", "callback_data": "cancel:"}]],
            )
            return
        removed = t.store.clear_seen()
        self.bot.send(f"✅ {removed} پروژه از لیست دیده‌شده پاک شد. اسکن بعدی دوباره پر می‌کند.")

    def cmd_autoscan(self, t: Tenant, text: str = "") -> None:
        arg = text.split(maxsplit=1)[1].strip().lower() if len(text.split()) > 1 else ""
        if arg in ("on", "روشن", "1"):
            t.rt.set("scout", "true")
            msg = "✅ اسکن خودکار برای اکانت تو روشن شد."
        elif arg in ("off", "خاموش", "0"):
            t.rt.set("scout", "false")
            msg = "⛔️ اسکن خودکار برای اکانت تو خاموش شد. اسکن دستی کار می‌کند."
        elif arg == "":
            msg = ("🔄 <b>اسکن خودکار</b>\n\n"
                   f"وضعیت: <b>{'روشن' if t.rt['scout'] else 'خاموش'}</b>\n"
                   f"هر <b>{t.rt['interval']}</b> دقیقه\n\n"
                   "<code>/autoscan on</code> · <code>/autoscan off</code>")
        else:
            msg = "فقط on یا off."
        self.bot.send(msg)

    def cmd_session(self, t: Tenant) -> None:
        if not t.cookies:
            self.bot.send("❌ اکانتی وصل نیست. با <code>/account cookies</code> کوکی اکانت خودت را بده.")
            return
        try:
            user = t.api.verify_session()
            self.bot.send(f"🔑 سشن سالم است. کاربر: <code>{esc(user)}</code>")
        except Exception as exc:  # noqa: BLE001
            self.bot.send(
                "❌ سشن فریلنسر تو کار نمی‌کند.\n"
                f"<code>{esc(exc)}</code>\n\n"
                "با <code>/account cookies</code> کوکی تازه بده."
            )

    # --------------------------------------------------------------------- ai
    def cmd_ai(self, t: Tenant, text: str) -> None:
        """`/ai` — show or edit this user's model endpoint and key."""
        aliases = {"key": "api_key", "token": "api_key", "apikey": "api_key"}
        parts = text.split(maxsplit=2)
        if len(parts) > 1:
            parts[1] = aliases.get(parts[1].lower(), parts[1])
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("", "show"):
            self.bot.send(
                t.ai.summary() + "\n\n"
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
                reply = ai_ping(t.ai)
                self.bot.send(f"✅ اتصال سالم است. پاسخ مدل: <code>{esc(reply[:40])}</code>")
            except Exception as exc:  # noqa: BLE001
                self.bot.send(f"❌ شکست: <code>{esc(str(exc)[:300])}</code>")
            return

        if sub == "reset":
            t.ai = AISettings.load()
            t.ai.save(t.store)
            self.bot.send("↩️ تنظیمات AI به پیش‌فرض برگشت.\n" + t.ai.summary())
            return

        field = {"base": "base_url", "base_url": "base_url", "key": "api_key",
                 "api_key": "api_key", "model": "model", "temp": "temperature",
                 "temperature": "temperature", "maxtokens": "max_tokens",
                 "max_tokens": "max_tokens", "timeout": "timeout"}.get(sub, "")
        if not field:
            self.bot.send("زیردستور ناشناخته. <code>/ai</code> را بزن.")
            return
        value = parts[2].strip() if len(parts) > 2 else ""
        if not value:
            self.bot.send("مقدار را هم بفرست. <code>/ai</code> را بزن.")
            return
        try:
            if field == "temperature":
                setattr(t.ai, field, float(value))
            elif field in ("max_tokens", "timeout"):
                setattr(t.ai, field, int(value))
            else:
                setattr(t.ai, field, value.rstrip("/"))
        except ValueError:
            self.bot.send(f"مقدار نامعتبر برای {sub}: <code>{esc(value)}</code>")
            return
        t.ai.save(t.store)
        self.bot.send("✅ ذخیره شد.\n" + t.ai.summary() + "\n\nبرای تست: <code>/ai test</code>")

    # ---------------------------------------------------------------- account
    def cmd_account(self, t: Tenant, text: str) -> None:
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("", "show"):
            self.bot.send(
                "🔑 <b>اکانت فریلنسر</b> (مخصوص خودت)\n\n"
                f"کاربر: <code>{esc(t.acc.username or '—')}</code>\n"
                f"آی‌دی: <code>{esc(t.acc.user_id or '—')}</code>\n"
                f"{t.cookie_report()}\n\n"
                "تغییر:\n"
                "<code>/account check</code> — تست واقعی سشن\n"
                "<code>/account user &lt;username&gt;</code> — نام کاربری\n"
                "<code>/account uid &lt;id&gt;</code> — آی‌دی عددی\n"
                "<code>/account cookies</code> — بعدش کوکی تازه را بفرست\n"
                "<code>/account logout</code> — اکانت از ربات جدا شود\n"
                "<code>/profile</code> — معرفی خودت برای نوشتن پیشنهاد"
            )
            return

        if sub == "check":
            self.cmd_session(t)
            return

        if sub == "cookies":
            self.set_waiting(t.chat, "account:cookies")
            self.bot.send(
                "کوکی‌های اکانت خودت را بفرست. یکی از این دو شکل قبول است:\n"
                "۱. خط کامل <code>Cookie:</code> از DevTools\n"
                "۲. خروجی فایل Netscape (تب‌جدا)\n\n"
                "این کوکی فقط در پروفایل تو ذخیره می‌شود و هیچ کاربر دیگری به آن دسترسی ندارد.\n"
                "بعد از ذخیره، سشن خودکار تست می‌شود."
            )
            return

        if sub == "logout":
            self.bot.send(
                "اکانت فریلنسر از ربات جدا شود؟ تا کوکی تازه ندهی اسکن و bid برای تو کار نمی‌کند.",
                keyboard=[[{"text": "✅ جدا کن", "callback_data": "logout:"}],
                          [{"text": "انصراف", "callback_data": "cancel:"}]],
            )
            return

        value = parts[2].strip() if len(parts) > 2 else ""
        if sub in ("user", "username"):
            if not value:
                self.bot.send("مثال: <code>/account user aryanhz</code>")
                return
            t.acc.username = value.lstrip("@")
        elif sub in ("uid", "id"):
            if not value.isdigit():
                self.bot.send("آی‌دی باید عدد باشد.")
                return
            t.acc.user_id = value
        else:
            self.bot.send("زیردستور ناشناخته. <code>/account</code> را بزن.")
            return

        t.acc.save(t.store)
        t.reload_api()
        self.bot.send(f"✅ اکانت ذخیره شد.\n{esc(t.acc.username)} · <code>{esc(t.acc.user_id)}</code>\n"
                      "برای اطمینان: <code>/account check</code>")

    def _set_cookies(self, t: Tenant, raw: str) -> None:
        cookies = parse_cookies(raw)
        if not cookies:
            self.bot.send("هیچ کوکی‌ای پیدا نشد. خط <code>Cookie:</code> را کامل بفرست.")
            return
        missing = [k for k in ("GETAFREE_USER_ID", "GETAFREE_AUTH_HASH_V2") if not cookies.get(k)]
        if missing:
            self.bot.send(
                "❌ کوکی ناقص است، این کلیدها نبود: " + ", ".join(missing) + "\n"
                "باید وارد freelancer.com شده باشی و کوکی‌ها را از همان دامنه بگیری."
            )
            return
        t.save_cookies(cookies)
        self.bot.send(f"✅ {len(cookies)} کوکی در پروفایل تو ذخیره شد. سشن را تست می‌کنم…")
        self.cmd_session(t)

    def cmd_profile(self, t: Tenant, text: str) -> None:
        value = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        if not value:
            self.bot.send(
                "🧑‍💻 <b>معرفی تو</b> — این متن فقط در پیشنهادهای تو استفاده می‌شود.\n\n"
                f"<pre>{esc(t.profile or '(خالی — پیش‌فرض عمومی استفاده می‌شود)')}</pre>\n\n"
                "<code>/profile مهارت‌ها، سابقه و سبک کارت را بنویس</code>"
            )
            return
        t.set_profile(value)
        self.bot.send("✅ ذخیره شد. از این به بعد پیشنهادها با معرفی خودت نوشته می‌شوند.")

    # ------------------------------------------------------------------ users
    def cmd_users(self, t: Tenant, text: str) -> None:
        """The user directory. Listing is open; changing roles is owner-only."""
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        target = parts[2].strip().split()[0] if len(parts) > 2 and parts[2].strip() else ""

        if sub in ("block", "unblock", "remove"):
            if not t.is_owner:
                self.bot.send("⛔️ فقط مالک ربات می‌تواند کاربران را مدیریت کند.")
                return
            if not target:
                self.bot.send("آی‌دی چت را هم بفرست.")
                return
            if self.db.user(target) is None:
                self.bot.send("این آیدی در لیست نیست. کاربر اول باید خودش <code>/start</code> بزند.")
                return
            if target == t.chat:
                self.bot.send("خودت را نمی‌توانی مسدود یا حذف کنی.")
                return
            if sub == "remove":
                self.db.delete_user(target)
                self.bot.send(f"🗑 <code>{esc(target)}</code> و همهٔ داده‌هایش حذف شد.")
            else:
                self.db.set_role(target, "blocked" if sub == "block" else "member")
                self.bot.send(f"{'⛔️' if sub == 'block' else '✅'} <code>{esc(target)}</code> "
                              f"{'مسدود' if sub == 'block' else 'فعال'} شد.")
            return

        lines = ["👥 <b>کاربران ربات</b>", ""]
        for r in self.db.users():
            mark = {"owner": "👑", "blocked": "⛔️"}.get(r["role"], "👤")
            mine = " ← خودت" if r["chat_id"] == t.chat else ""
            lines.append(f"{mark} <code>{esc(r['chat_id'])}</code> · {esc(r['name'] or '—')} "
                         f"· {r['role']}{mine}")
        if t.is_owner:
            lines += ["", "مسدود: <code>/users block &lt;chat_id&gt;</code>",
                      "فعال: <code>/users unblock &lt;chat_id&gt;</code>",
                      "حذف کامل: <code>/users remove &lt;chat_id&gt;</code>"]
        self.bot.send("\n".join(lines))

    # ---------------------------------------------------------------- waiting
    def set_waiting(self, chat_id: str, key: str) -> None:
        with self.waiting_lock:
            self.waiting[chat_id] = key

    def pop_waiting(self, chat_id: str) -> str:
        with self.waiting_lock:
            return self.waiting.pop(chat_id, "")

    # --------------------------------------------------------------- dispatch
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            who = str(((q.get("message") or {}).get("chat") or {}).get("id") or "")
            if not who:
                return
            t = self._resolve(who, q.get("from") or {})
            if not t:
                return
            with self.bot.to(who):
                self._handle_callback(t, q)
            return

        msg = update.get("message") or {}
        who = str((msg.get("chat") or {}).get("id") or "")
        if not who:
            return
        t = self._resolve(who, msg.get("from") or {})
        if not t:
            return
        with self.bot.to(who):
            self._handle_message(t, msg)

    def _resolve(self, chat_id: str, sender: dict) -> Tenant | None:
        """Register a newcomer, refuse a blocked one. The tenant is per update."""
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
        name = name or sender.get("username") or ""
        t, _registered = self.touch(chat_id, name)
        if t.role == "blocked":
            log.warning("blocked user %s tried to use the bot", chat_id)
            return None
        return t

    def _handle_callback(self, t: Tenant, q: dict) -> None:
        data = q.get("data", "")
        action, _, arg = data.partition(":")
        self.bot.answer_callback(q.get("id", ""))
        try:
            if action == "draft":
                self.make_draft(t, int(arg))
            elif action == "send":
                self.send_bid(t, int(arg))
            elif action == "drop":
                t.store.set_draft_status(int(arg), "discarded")
                self.bot.send(f"پیش‌نویس {arg} کنار گذاشته شد.")
            elif action == "price":
                self.bot.send(f"قیمت جدید را بفرست: <code>price {arg} 250 5</code>")
            elif action == "cfg":
                label = menus.SPEC[arg][3]
                self.set_waiting(t.chat, arg)
                self.bot.send(
                    f"مقدار تازه برای <b>{esc(label)}</b> را بفرست.\n"
                    f"مقدار فعلی: <code>{t.rt[arg]}</code>\n"
                    "/cancel برای انصراف"
                )
            elif action == "logout":
                t.clear_cookies()
                self.bot.send("🗑 اکانت از ربات جدا شد. تا کوکی تازه ندهی اسکن و bid برای تو کار نمی‌کند.")
            elif action == "confirmed":
                self.cmd_cleanup(t, confirm=True)
            elif action == "cancel":
                self.pop_waiting(t.chat)
                self.bot.send("لغو شد.")
        except (ValueError, KeyError):
            self.bot.send("این دکمه دیگر معتبر نیست. از منو دوباره شروع کن.")

    def _handle_message(self, t: Tenant, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        if not text:
            return
        chat_id = t.chat

        text = menus.LABEL_TO_CMD.get(text, text)
        low = text.lower()

        if low in ("/cancel", "cancel"):
            self.pop_waiting(chat_id)
            self.bot.send("لغو شد.")
            return

        # A pending prompt owns the next plain message: either one setting or a
        # pasted cookie blob, which is far too long to go through the SPEC table.
        key = self.pop_waiting(chat_id)
        if key and not low.startswith("/"):
            if key == "account:cookies":
                self._set_cookies(t, text)
                return
            try:
                value = t.rt.set(key, text)
            except (ValueError, KeyError):
                self.bot.send("مقدار نامعتبر. دوباره از ⚙️ تنظیمات امتحان کن.")
                return
            self.bot.send(f"✅ {esc(menus.SPEC[key][3])} شد <b>{esc(value)}</b>")
            return

        if low.startswith("/start"):
            self.cmd_menu(t, first=True)
        elif low.startswith("/menu"):
            self.cmd_menu(t)
        elif low.startswith("/scan"):
            self.cmd_scan(t)
        elif low.startswith("/status"):
            self.cmd_status(t)
        elif low.startswith("/drafts"):
            self.cmd_drafts(t)
        elif low.startswith("/settings"):
            self.cmd_settings(t)
        elif low.startswith("/logs"):
            self.cmd_logs()
        elif low.startswith("/session"):
            self.cmd_session(t)
        elif low.startswith("/autoscan"):
            self.cmd_autoscan(t, text)
        elif low.startswith("/account"):
            self.cmd_account(t, text)
        elif low.startswith("/users") or low.startswith("/admins"):
            self.cmd_users(t, text)
        elif low.startswith("/cleanup"):
            self.cmd_cleanup(t)
        elif low.startswith("/ai"):
            self.cmd_ai(t, text)
        elif low.startswith("/profile"):
            self.cmd_profile(t, text)
        elif low.startswith("/bids"):
            self._cmd_bids(t)
        elif low.startswith("price "):
            self._cmd_price(t, text)

    def _cmd_bids(self, t: Tenant) -> None:
        if not t.cookies:
            self.bot.send("❌ اول اکانت فریلنسر خودت را وصل کن: <code>/account cookies</code>")
            return
        try:
            bids, _ = t.api.my_bids()
        except FreelancerError as exc:
            self.bot.send(f"❌ سشن فریلنسر جواب نداد: <code>{esc(str(exc)[:120])}</code>\n"
                          "با <code>/account check</code> بررسی کن.")
            return
        if not bids:
            self.bot.send("bid ثبت‌شده‌ای نیست.")
            return
        body = "\n".join(
            f"#{b['id']} · ${b['amount']:.0f} · {b['period']} روز · پ‍روژه {b['project_id']}"
            for b in bids[:10]
        )
        self.bot.send(f"<b>bid‌های من</b>\n{body}")

    def _cmd_price(self, t: Tenant, text: str) -> None:
        parts = text.split()
        if not (len(parts) in (3, 4) and parts[1].isdigit()
                and parts[2].replace(".", "", 1).isdigit()):
            return
        pid = int(parts[1])
        amount = float(parts[2])
        period = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else t.period
        d = t.store.draft(pid)
        if not d:
            self.bot.send(f"پیش‌نویسی برای {pid} نیست.")
            return
        t.store.save_draft(pid, d["title"], amount, period, d["description"])
        kb = [[{"text": f"✅ تأیید و ارسال — ${amount:.0f} / {period} روز",
                "callback_data": f"send:{pid}"}]]
        self.bot.send(f"قیمت به <b>${amount:.0f}</b> و <b>{period} روز</b> تغییر کرد.", keyboard=kb)

    # ------------------------------------------------------------------ setup
    def register_commands(self) -> None:
        cmds = [
            ("start", "شروع و منو"),
            ("scan", "اسکن پروژه‌ها"),
            ("drafts", "پیش‌نویس‌های منتظر"),
            ("bids", "bid‌های من"),
            ("status", "وضعیت"),
            ("settings", "تنظیمات"),
            ("ai", "تنظیمات هوش مصنوعی"),
            ("account", "اکانت فریلنسر من"),
            ("profile", "معرفی من برای پیشنهاد"),
            ("users", "کاربران ربات"),
            ("logs", "آخرین رویدادها"),
        ]
        try:
            self.bot.call("setMyCommands",
                          commands=[{"command": c, "description": d} for c, d in cmds])
        except Exception:  # noqa: BLE001 - cosmetic, must never block startup
            log.warning("setMyCommands failed", exc_info=True)

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        me = self.bot.call("getMe")
        log.info("bot @%s up — %d user(s)", me.get("username"), len(self.db.users()))
        self.register_commands()
        offset = int(self.db.tenant(GLOBAL).get_meta("offset", 0) or 0)
        marker = self.cfg.health_path
        while True:
            try:
                # 15s long-poll: short enough that the heartbeat below refreshes well
                # inside the probe's 120s window even when nothing is arriving.
                for update in self.bot.updates(offset, timeout=15):
                    offset = update["update_id"] + 1
                    self.db.tenant(GLOBAL).set_meta("offset", offset)
                    self.last_poll = time.time()
                    try:
                        self.handle(update)
                    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                        log.exception("update failed")
                self.last_poll = time.time()
                # Heartbeat on disk: a probe inside the container reads this instead of
                # starting a second poll loop, and the timestamp proves it is advancing.
                try:
                    marker.write_text(str(int(self.last_poll)), encoding="utf-8")
                except OSError as exc:
                    log.warning("heartbeat not written: %s", exc)
            except (TelegramError, OSError) as exc:
                log.warning("poll error: %s", exc)
                time.sleep(5)


def start_scout_thread(app: App, interval_min: int = 30) -> None:
    """Periodic scan, one tenant at a time.

    Each pass re-reads the user table and builds a fresh Tenant per user, so a pass
    never shares cookies, a client or a store between users. A user is skipped when
    their own autoscan is off or their own interval has not elapsed yet.
    """

    def loop() -> None:
        while True:
            for row in app.db.users():
                chat_id = str(row["chat_id"])
                if row["role"] == "blocked":
                    continue
                try:
                    t = app.tenant(chat_id)
                    if not t.rt["scout"] or not t.cookies:
                        continue
                    last = int(t.store.get_meta("last_scan", 0) or 0)
                    if last and time.time() - last < max(1, int(t.rt["interval"])) * 60:
                        continue
                    with app.bot.to(chat_id):
                        items, pool_size = app.scan(t)
                        if not items:
                            continue
                        app.bot.send(f"🔎 <b>{len(items)}</b> پروژهٔ مناسب از {pool_size} پروژه.")
                        for p in items[: t.shortlist]:
                            t.store.mark_seen(p["id"], p["title"])
                            kb = [[{"text": f"✍️ پیشنهاد بده — ${p['budget_min_usd']:.0f}",
                                    "callback_data": f"draft:{p['id']}"}]]
                            app.bot.send(app.format_project(p), keyboard=kb)
                except Exception:  # noqa: BLE001 - one broken tenant must not stop the rest
                    log.exception("scout failed for %s", chat_id)
            time.sleep(max(1, int(interval_min or 30)) * 60)

    threading.Thread(target=loop, name="scout", daemon=True).start()
