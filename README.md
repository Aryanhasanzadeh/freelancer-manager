# Freelancer Manager

Telegram bot that finds Freelancer.com projects, writes a bid, and posts it **only after you press a button**.

Replaces the old patchwork of one-off scripts in `~/.hermes/cache/scratch/fl/` (diag1..13.py, dump.py, bid.py).

## How it works

1. **Scan** — pulls active projects for 17 search terms, filters them, ranks by bid count then age.
2. **Shortlist** — top 3 go to Telegram with a `✍️ پیشنهاد بده` button.
3. **Draft** — fetches the full brief, asks the LLM for a bid, stores it in SQLite.
4. **Confirm** — the draft message carries `✅ تأیید و ارسال` / `✏️ ویرایش قیمت` / `🗑 دور بریز`.
5. **Bid** — only the ✅ button calls `POST /projects/0.1/bids/`. Nothing is auto-posted.

A background scout re-scans every 180 minutes and stays silent when nothing qualifies.

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
price 40802006 250 5    change amount and period, re-offer the send button
40802006         write a draft for that project id
```

## Layout

```
freelancer_manager/
  api.py       Freelancer.com client (cookies + freelancer-auth-v2 header)
  scan.py      pure filter/rank functions — no network, easy to test
  proposal.py  LLM drafting
  store.py     SQLite: seen, drafts, bids, meta
  bot.py       dispatcher, scanning, drafting, bidding
  tg.py        Telegram transport
tests/
  dry_run.py       scan + draft with Telegram output captured in memory
  dispatch_test.py synthetic updates through the real dispatcher
secrets/         bot_token.txt, cookies.txt, ai_key.txt, chat_id.txt (chmod 600)
data/fl.db       state
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
cd /opt/freelancer-manager
python3 -m freelancer_manager whoami            # session check
python3 -m freelancer_manager scan             # CLI scan, prints ids
python3 tests/dry_run.py 40802006              # draft, nothing sent
python3 tests/dispatch_test.py                 # dispatcher, nothing sent
```

## Service

```bash
systemctl status freelancer-bot
journalctl -u freelancer-bot -f
systemctl restart freelancer-bot
```
