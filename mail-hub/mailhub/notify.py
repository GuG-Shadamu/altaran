"""Discord pushes: new high-priority items, and appointments starting soon."""
import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("mailhub.notify")

EMOJI = {"appointment": "📅", "shopping": "📦", "outreach": "🤝", "work": "💼", "deal": "🏷️", "ad": "📣"}


def _send(webhook, text):
    req = urllib.request.Request(
        webhook, data=json.dumps({"content": text[:1900]}).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "mail-hub"})
    urllib.request.urlopen(req, timeout=15).close()


def _when(item, tz):
    t = item["event_time"]
    if not t:
        return ""
    if len(t) == 10:
        return t
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz).strftime("%m-%d %a %H:%M")


def run(cfg, conn, remind_hours=24):
    if not cfg.discord_webhook:
        return 0
    labels = {a.email: a.label for a in cfg.accounts}
    sent = 0

    rows = conn.execute(
        """SELECT * FROM items WHERE priority = 'high' AND notified = 0 AND user_state = 'open'
           AND category != 'ad' ORDER BY last_date""").fetchall()
    for it in rows:
        when = _when(it, cfg.tz)
        text = (f"{EMOJI.get(it['category'], '•')} **{it['title']}**"
                f"{'  ·  ' + when if when else ''}  ·  {labels.get(it['account'], it['account'])}\n"
                f"{it['summary']}")
        try:
            _send(cfg.discord_webhook, text)
        except Exception as e:
            log.warning("discord push failed: %s", e)
            return sent
        conn.execute("UPDATE items SET notified = 1 WHERE id = ?", (it["id"],))
        sent += 1

    now = datetime.now(timezone.utc)
    soon = (now + timedelta(hours=remind_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT * FROM items WHERE category = 'appointment' AND reminded = 0
           AND user_state = 'open' AND length(event_time) > 10
           AND event_time > ? AND event_time <= ?""",
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"), soon)).fetchall()
    for it in rows:
        data = json.loads(it["data"] or "{}")
        if data.get("status") == "cancelled":
            continue
        loc = f"  ·  {data['location']}" if data.get("location") else ""
        try:
            _send(cfg.discord_webhook, f"⏰ 快到了：**{it['title']}**  ·  {_when(it, cfg.tz)}{loc}")
        except Exception as e:
            log.warning("discord push failed: %s", e)
            return sent
        conn.execute("UPDATE items SET reminded = 1, notified = 1 WHERE id = ?", (it["id"],))
        sent += 1
    conn.commit()
    return sent
