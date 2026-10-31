"""Incremental sync of every configured account into SQLite."""
import logging

from . import db
from .gmail import Gmail, GmailError, parse_message

log = logging.getLogger("mailhub.sync")

# Rule-level skips: never worth an LLM call.
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT", "CHAT", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}


def _store(conn, g, account, ids):
    known = {r[0] for r in conn.execute(
        f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    n = 0
    for mid in ids:
        if mid in known:
            continue
        try:
            raw = g.message(mid)
        except GmailError as e:
            if e.status == 404:  # deleted between list and get
                continue
            raise
        m = parse_message(account, raw)
        labels = set(m["labels"].split(","))
        if m["is_sent"] or labels & SKIP_LABELS:
            m["state"] = 2
        db.upsert_message(conn, m)
        n += 1
        if n % 50 == 0:
            conn.commit()
    conn.commit()
    return n


def sync_account(cfg, conn, email):
    g = Gmail(cfg, email)
    st = db.account_state(conn, email)
    history_id = st["history_id"] if st else None

    if history_id:
        try:
            ids, latest = g.history(history_id)
            n = _store(conn, g, email, ids)
            db.save_account_state(conn, email, history_id=latest)
            conn.commit()
            return n
        except GmailError as e:
            if e.status != 404:
                raise
            log.warning("%s: history %s expired, doing full resync", email, history_id)

    # Take the history id *before* listing, so nothing that arrives during
    # the initial listing is missed by the next incremental sync.
    latest = g.profile()["historyId"]
    ids = g.list_ids(f"newer_than:{cfg.lookback_days}d -in:chats", cfg.max_initial_messages)
    n = _store(conn, g, email, ids)
    db.save_account_state(conn, email, history_id=latest)
    conn.commit()
    return n


def sync_all(cfg, conn):
    total = 0
    for a in cfg.accounts:
        try:
            n = sync_account(cfg, conn, a.email)
            log.info("%s: %d new messages", a.email, n)
            total += n
        except Exception as e:  # one broken account must not stop the others
            log.exception("%s: sync failed", a.email)
            db.save_account_state(conn, a.email, error=str(e)[:500])
            conn.commit()
    return total
