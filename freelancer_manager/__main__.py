"""Package entry point: python -m freelancer_manager [run|scan|bids]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import Config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def main() -> int:
    ap = argparse.ArgumentParser(prog="freelancer-manager")
    ap.add_argument("cmd", nargs="?", default="run",
                    choices=["run", "scan", "bids", "whoami"])
    ap.add_argument("--interval", type=int, default=None,
                    help="minutes between background scans (default FL_INTERVAL, 0 = manual only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
    if args.cmd == "scan":
        items, pool = app.scan()
        print(f"pool={pool} eligible={len(items)}")
        for p in items:
            print(f"{p['id']} | {p['bids']}b ${p['budget_min_usd']:.0f}-{p['budget_max_usd']:.0f}"
                  f" | {p['title'][:70]}")
        return 0
    if args.cmd == "bids":
        bids, _ = app.api.my_bids()
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
