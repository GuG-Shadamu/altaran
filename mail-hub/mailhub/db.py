"""SQLite storage: raw messages, extracted dashboard items, per-account sync state."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email        TEXT PRIMARY KEY,
    history_id   TEXT,
    last_sync    INTEGER,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    thread_id     TEXT,
    internal_date INTEGER,
    from_addr     TEXT,
    from_name     TEXT,
    to_addr       TEXT,
    subject       TEXT,
    snippet       TEXT,
    labels        TEXT,
    body          TEXT,
    bulk          INTEGER DEFAULT 0,
    is_sent       INTEGER DEFAULT 0,
    -- 0 = pending, 1 = classified, 2 = skipped by rules, 3 = failed
    state         INTEGER DEFAULT 0,
    attempts      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_state ON messages(state, internal_date);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    category    TEXT NOT NULL,
    account     TEXT,
    title       TEXT,
    summary     TEXT,
    data        TEXT,
    event_time  TEXT,
    priority    TEXT DEFAULT 'normal',
    message_id  TEXT,
    thread_id   TEXT,
    first_seen  INTEGER,
    last_date   INTEGER,
    user_state  TEXT DEFAULT 'open',
    done_at     INTEGER,
    notified    INTEGER DEFAULT 0,
    reminded    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_cat ON items(category, user_state);
"""

# Categories the item can be reopened in when a newer email arrives after "done".
REOPENABLE = {"work", "outreach", "shopping", "appointment"}


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now_ms():
    return int(time.time() * 1000)


def upsert_message(conn, m):
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (id, account, thread_id, internal_date, from_addr, from_name, to_addr,
            subject, snippet, labels, body, bulk, is_sent, state)
           VALUES (:id, :account, :thread_id, :internal_date, :from_addr, :from_name, :to_addr,
                   :subject, :snippet, :labels, :body, :bulk, :is_sent, :state)""",
        m,
    )


def upsert_item(conn, item):
    """Insert or merge an item by its key. Returns 'new', 'updated' or 'stale'.

    An older email never overwrites a newer one (Gmail sync order is not
    chronological). A newer email on an item the user marked done reopens it.
    """
    row = conn.execute("SELECT * FROM items WHERE key = ?", (item["key"],)).fetchone()
    data = json.dumps(item.get("data") or {}, ensure_ascii=False)
    if row is None:
        conn.execute(
            """INSERT INTO items (key, category, account, title, summary, data, event_time,
                                  priority, message_id, thread_id, first_seen, last_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["key"], item["category"], item["account"], item["title"], item["summary"],
             data, item.get("event_time"), item.get("priority", "normal"),
             item["message_id"], item.get("thread_id"), now_ms(), item["last_date"]),
        )
        return "new"
    if (row["last_date"] or 0) > item["last_date"]:
        return "stale"

    reopen = (row["user_state"] == "done" and item["category"] in REOPENABLE
              and item["last_date"] > (row["done_at"] or 0))
    priority_up = item.get("priority") == "high" and row["priority"] != "high"
    conn.execute(
        """UPDATE items SET category = ?, title = ?, summary = ?, data = ?, event_time = ?,
                  priority = ?, message_id = ?, last_date = ?,
                  user_state = CASE WHEN ? THEN 'open' ELSE user_state END,
                  notified = CASE WHEN ? OR ? THEN 0 ELSE notified END
           WHERE id = ?""",
        (item["category"], item["title"], item["summary"], data,
         item.get("event_time") or row["event_time"], item.get("priority", "normal"),
         item["message_id"], item["last_date"], reopen, reopen, priority_up, row["id"]),
    )
    return "updated"


def set_item_state(conn, item_id, state):
    conn.execute(
        "UPDATE items SET user_state = ?, done_at = ? WHERE id = ?",
        (state, now_ms() if state == "done" else None, item_id),
    )


def account_state(conn, email):
    return conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()


def save_account_state(conn, email, history_id=None, error=None):
    conn.execute(
        """INSERT INTO accounts (email, history_id, last_sync, last_error) VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
             history_id = COALESCE(excluded.history_id, history_id),
             last_sync = CASE WHEN excluded.last_error IS NULL THEN excluded.last_sync ELSE last_sync END,
             last_error = excluded.last_error""",
        (email, history_id, now_ms(), error),
    )


def thread_last_is_mine(conn, thread_id):
    row = conn.execute(
        "SELECT is_sent FROM messages WHERE thread_id = ? ORDER BY internal_date DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return bool(row and row["is_sent"])
