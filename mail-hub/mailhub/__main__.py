"""mail-hub CLI.

  python3 -m mailhub auth <email>   authorize one Gmail account (read-only)
  python3 -m mailhub tick           sync + classify + notify (run by the systemd timer)
  python3 -m mailhub serve          run the dashboard
  python3 -m mailhub status         per-account sync state and counts
  python3 -m mailhub demo           fill a throwaway DB with sample data to preview the page
"""
import argparse
import fcntl
import logging
import sys

from . import classify, config, db, notify, sync


def cmd_auth(cfg, args):
    from .gmail import authorize
    if not cfg.account(args.email):
        sys.exit(f"{args.email} 不在 config 的 [[accounts]] 里，先加进去")
    authorize(cfg, args.email)


def cmd_tick(cfg, args):
    # Timer runs can overlap if a big initial sync is slow; only one at a time.
    lock = open(cfg.db_path.parent / "tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another tick is running, skipping")
        return
    conn = db.connect(cfg.db_path)
    if not args.no_sync:
        sync.sync_all(cfg, conn)
    stats = classify.classify_pending(cfg, conn)
    logging.info("classify: %s", stats)
    sent = notify.run(cfg, conn)
    if sent:
        logging.info("discord: %d pushed", sent)


def cmd_serve(cfg, args):
    from . import web
    web.serve(cfg)


def cmd_status(cfg, args):
    conn = db.connect(cfg.db_path)
    for a in cfg.accounts:
        st = db.account_state(conn, a.email)
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE account = ?", (a.email,)).fetchone()[0]
        tok = (cfg.token_dir / f"{a.email.lower()}.json").exists()
        print(f"{a.label:12} {a.email:32} token={'✓' if tok else '✗'} messages={n:<5} "
              f"history={st['history_id'] if st else '-'} error={st['last_error'] if st else '-'}")
    for state, label in ((0, "pending"), (1, "classified"), (2, "skipped"), (3, "failed")):
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE state = ?", (state,)).fetchone()[0]
        print(f"messages {label:10} {n}")
    for r in conn.execute("SELECT category, COUNT(*) n FROM items WHERE user_state = 'open' GROUP BY category"):
        print(f"items    {r['category']:10} {r['n']}")


def cmd_demo(cfg, args):
    from . import demo
    demo.seed(cfg)
    print(f"sample data written to {cfg.db_path}")


def main():
    ap = argparse.ArgumentParser(prog="mailhub")
    ap.add_argument("--config", help="default ~/.config/mail-hub/config.toml or $MAILHUB_CONFIG")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("auth")
    p.add_argument("email")
    p = sub.add_parser("tick")
    p.add_argument("--no-sync", action="store_true", help="only classify what is already stored")
    sub.add_parser("serve")
    sub.add_parser("status")
    sub.add_parser("demo")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    {"auth": cmd_auth, "tick": cmd_tick, "serve": cmd_serve,
     "status": cmd_status, "demo": cmd_demo}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
