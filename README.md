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

Option A — systemd (venv, nothing global):

```bash
git clone https://github.com/Aryanhasanzadeh/freelancer-manager.git
cd freelancer-manager
sudo bash deploy/install.sh          # venv + unit + enable + start
$EDITOR .env                         # FL_BOT_TOKEN, FL_CHAT_ID (optional)
$EDITOR secrets/cookies.txt          # Freelancer cookies
sudo systemctl restart freelancer-bot
```

The installer is idempotent and refuses Python < 3.10. Then press Start on the bot in
Telegram — the first chat to send `/start` is bound automatically.

Option B — Docker:

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose logs -f
```

State (`data/`, `secrets/`) is bind-mounted, so the container is disposable.

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
/scan            scan now
/drafts          drafts waiting for approval
/bids            my bids, straight from the API
/ai              model endpoint settings
price 40802006 250 5    change amount and period, re-offer the send button
40802006         write a draft for that project id
```

## Layout

```
freelancer_manager/
  api.py         Freelancer.com client (cookies + freelancer-auth-v2 header)
  scan.py        pure filter/rank functions — no network, easy to test
  aisettings.py  AI endpoint settings, one chat call, /ai test ping
  proposal.py    LLM drafting
  store.py       SQLite: seen, drafts, bids, meta
  bot.py         dispatcher, scanning, drafting, bidding
  tg.py          Telegram transport
tests/
  dry_run.py       scan + draft with Telegram output captured in memory
  dispatch_test.py synthetic updates through the real dispatcher
deploy/
  install.sh       fresh-server installer (venv + systemd unit)
  freelancer-bot.service
Dockerfile, docker-compose.yml   container deployment
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
