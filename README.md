# Freelancer Manager

Telegram bot that finds Freelancer.com projects, writes a bid, and posts it **only after you press a button**.

Self-contained: one directory, one SQLite file, no external services. Move it to any
server with `git clone` + `bash deploy/install.sh`, or run it as a container.

## How it works

1. **Scan** — pulls active projects for 17 search terms, filters them, ranks by bid count then age.
2. **Shortlist** — top 3 go to Telegram with a `✍️ پیشنهاد بده` button.
3. **Draft** — fetches the full brief, asks the LLM for a bid, stores it in SQLite.
4. **Confirm** — the draft message carries `✅ تأیید و ارسال` / `✏️ ویرایش قیمت` / `🗑 دور بریز`.
5. **Bid** — only the ✅ button calls `POST /projects/0.1/bids/`. Nothing is auto-posted.

A background scout re-scans every `FL_INTERVAL` minutes and stays silent when nothing qualifies.

## Management menu

`/start` (or `/menu`) pins a persistent keyboard with every section:

```
🔎 اسکن            📝 پیش‌نویس‌ها
📊 وضعیت           ⚙️ تنظیمات
🤖 هوش مصنوعی      🔑 سشن فریلنسر
📋 لاگ             🧹 پاک‌سازی
```

Buttons just send a command, so text and taps share one dispatcher. Sections:

- **وضعیت** — seen projects, pending drafts, placed bids, last scan age and result, live AI endpoint, active budget.
- **تنظیمات** — one button per knob (budget floor/ceiling, max competing bids, shortlist size, scan interval, delivery period, milestone, background scanning on/off). Tapping asks for a value; the next message is stored, validated, applied to the live config and persisted in `meta`. `/cancel` aborts.
- **هوش مصنوعی** — see below.
- **سشن فریلنسر** — `verify_session()`, because an expired cookie jar still answers HTTP 200.
- **لاگ** — the last log lines from an in-process ring buffer, so `/logs` works without journald.
- **پاک‌سازی** — clears the seen list behind an inline confirm, so the next scan can rediscover the current pool.

## AI settings (`/ai`)

The model endpoint is **not** in the code. It lives in the `meta` table and is edited
from the chat:

```
/ai                                   show current base URL, model, masked key
/ai key sk-...                        API key (also written to secrets/ai_key.txt)
/ai base https://api.example.com/v1   any [OI]-compatible endpoint
/ai model gpt-4o-mini                 model name
/ai temp 0.3                          temperature
/ai maxtokens 900                     max response tokens
/ai timeout 180                       request timeout, seconds
/ai test                              one real round-trip to the model
/ai reset                             back to the defaults below
```

Precedence on startup: saved settings in SQLite → `FL_AI_*` from `.env` →
`secrets/ai_key.txt` → built-in defaults (`https://ai.metft.ir/v1`, `glm-5.3-flash`).
A fresh server therefore only needs the bot token; the rest can be set from `/ai`.

## Deploy on a new server

Docker is the supported install. Nothing runs on the host: no venv, no systemd unit.

```bash
git clone https://github.com/Aryanhasanzadeh/freelancer-manager.git
cd freelancer-manager
cp .env.example .env
$EDITOR .env                    # FL_BOT_TOKEN required, FL_CHAT_ID recommended
mkdir -p data secrets
printf '%s' "$COOKIES" > secrets/cookies.txt   # Freelancer session cookies
docker compose up -d --build
docker compose logs -f
```

`FL_CHAT_ID` pins the owner. Set it before the first start: the owner is a tenant
row in `data/fl.db` and the fallback "first chat becomes owner" only applies when no
id is pinned, so a recreated container keeps the same owner either way but a pinned
id cannot be raced.

State (`data/fl.db`, `secrets/`) is bind-mounted, so the container is disposable.
`secrets/` is mounted read-only, so the process cannot rewrite its own token.

**One poller only.** Two processes polling the same token steal updates from each
other and each chat is answered by whichever wins. `docker compose up -d` always
keeps a single `bot` service; if this host previously ran the systemd install, move
it over with:

```bash
sudo bash deploy/dockerize.sh
```

That disables and stops `freelancer-bot.service`, hands `data/` and `secrets/` to the
container user (uid 10001 / gid 999), then starts the stack and waits for the
healthcheck. A rollback is `systemctl enable --now freelancer-bot` after
`docker compose down`.

### Health, logs, tests

```bash
docker compose ps                                  # health column comes from the probe
docker compose exec bot python -m freelancer_manager health
docker compose logs -f --tail 100
docker compose --profile test run --rm tests       # the isolation suite, in the image
```

The container writes `data/.heartbeat` on every poll pass and a probe fails when it
is older than 120s, so a wedged process is reported unhealthy instead of looking
alive just because the container still exists. `restart: unless-stopped` turns that
into a self-heal.

## Filters

| Rule | Default | Env |
|---|---|---|
| Budget floor/ceiling (USD) | 10 / 800 | `FL_BUDGET_MIN`, `FL_BUDGET_MAX` |
| Max competing bids | 60 | `FL_MAX_BIDS` |
| Shortlist size | 3 | `FL_SHORTLIST` |
| Rejected topics | design, writing, translation, data entry, tutoring, trading bots, … | — in `scan.py` |
| Language | English only | — |

Skills are matched against `MY_SKILLS` in `scan.py`; a project must share at least one.

## Commands

```
/menu            pin the management keyboard
/scan            scan now
/drafts          drafts waiting for approval
/bids            my bids, straight from the API
/status          counters, last scan, active settings
/settings        edit budget, interval, shortlist, ...
/ai              model endpoint settings
/session         check the Freelancer session
/logs            last log lines
/cleanup         clear the seen list (asks first)
price 40802006 250 5    change amount and period, re-offer the send button
40802006         write a draft for that project id
```

## Layout

```
freelancer_manager/
  api.py         Freelancer.com client (cookies + freelancer-auth-v2 header)
  scan.py        pure filter/rank functions — no network, easy to test
  aisettings.py  AI endpoint settings, one chat call, /ai test ping
  runtime.py     budget/interval/shortlist/period — editable from /settings
  menu.py        the pinned keyboard, label→command map, status/settings text
  logbuf.py      in-process log ring behind /logs
  proposal.py    LLM drafting
  store.py       SQLite: tenant-scoped seen, drafts, bids, meta
  tenant.py      one chat's account, cookies, AI settings and knobs
  bot.py         dispatcher, scanning, drafting, bidding
  tg.py          Telegram transport, per-chat reply routing
tests/
  dry_run.py       scan + draft with Telegram output captured in memory
  dispatch_test.py isolation suite: synthetic updates through the real dispatcher,
                   then the deployment contract (owner pin, heartbeat, compose)
deploy/
  dockerize.sh     move a host from the systemd install to the container one
  install.sh       legacy venv + systemd installer (kept for rollback)
  freelancer-bot.service
Dockerfile, docker-compose.yml   container deployment (the supported one)
.env.example     every knob, documented
```

## Session expiry

Freelancer cookies expire. `verify_session()` compares the API's `self` username with
`FL_USERNAME` — a logged-out session still answers HTTP 200, so a status check alone
proves nothing. When it fails, re-export `secrets/cookies.txt` from the browser:

```
GETAFREE_USER_ID=...
GETAFREE_AUTH_HASH_V2=...
XSRF-TOKEN=...
```

The bot also needs the `freelancer-auth-v2: <uid>;<decoded hash>` header — cookies alone
make POST endpoints answer "unauthenticated".

## Tests

```bash
cd freelancer-manager
python3 -m freelancer_manager whoami            # session check
python3 -m freelancer_manager scan             # CLI scan, prints ids
python3 tests/dry_run.py 40802006              # draft, nothing sent
python3 tests/dispatch_test.py                 # dispatcher + /ai, nothing sent
```

## Service

```bash
systemctl status freelancer-bot
journalctl -u freelancer-bot -f
systemctl restart freelancer-bot
```
