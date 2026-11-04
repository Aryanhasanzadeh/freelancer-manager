"""Package entry point: python -m freelancer_manager [run|scan|bids]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import logbuf
from .config import Config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


HEALTH_MAX_AGE = 120  # seconds; the poll loop must beat well inside this


def health(cfg: Config, app=None) -> int:
    """Container probe: is the poll loop alive right now?

    Reads the heartbeat the loop writes on every pass. Reading a file costs nothing
    on Telegram's side, whereas getMe on every probe would open a second session
    beside the running one.
    """
    import time

    beat = cfg.health_path
    if not beat.exists():
        print(f"unhealthy: no heartbeat at {beat}", file=sys.stderr)
        return 1
    try:
        age = int(time.time() - float(beat.read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError) as exc:
        print(f"unhealthy: unreadable heartbeat: {exc}", file=sys.stderr)
        return 1
    if age > HEALTH_MAX_AGE:
        print(f"unhealthy: heartbeat is {age}s old", file=sys.stderr)
        return 1
    if app is not None:
        counts = app.db.counts()
        print(f"ok heartbeat={age}s users={counts['users']} drafts={counts['drafts']}")
    else:
        print(f"ok heartbeat={age}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="freelancer-manager")
    ap.add_argument("cmd", nargs="?", default="run",
                    choices=["run", "scan", "bids", "whoami", "health"])
    ap.add_argument("--interval", type=int, default=None,
                    help="minutes between background scans (default FL_INTERVAL, 0 = manual only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # /logs reads this ring, so a bot without journal access can still explain itself.
    logbuf.install()

    cfg = Config()
    if not cfg.bot_token:
        print("missing FL_BOT_TOKEN / secrets/bot_token.txt", file=sys.stderr)
        return 2

    from .api import Client, FreelancerError
    from .bot import App

    if args.cmd == "whoami":
        print(Client(cfg).verify_session())
        return 0

    app = App(cfg)
    if args.cmd == "health":
        return health(cfg, app)
    if args.cmd == "scan":
        t = app.tenant(str(cfg.chat_id or "0"))
        items, pool = app.scan(t)
        print(f"pool={pool} eligible={len(items)}")
        for p in items:
            print(f"{p['id']} | {p['bids']}b ${p['budget_min_usd']:.0f}-{p['budget_max_usd']:.0f}"
                  f" | {p['title'][:70]}")
        return 0
    if args.cmd == "bids":
        bids, _ = app.tenant(str(cfg.chat_id or "0")).api.my_bids()
        for b in bids:
            print(b["id"], b["project_id"], b["amount"], b["period"])
        return 0

    # --interval wins, then FL_INTERVAL from .env, then no background scanning.
    interval = args.interval if args.interval is not None else int(_env("FL_INTERVAL", "30") or 0)
    if interval:
        from .bot import start_scout_thread
        start_scout_thread(app, interval)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
