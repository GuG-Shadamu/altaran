"""Dashboard HTTP server: one static page plus a small JSON API."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .access import AccessVerifier

log = logging.getLogger("mailhub.web")
STATIC = Path(__file__).parent / "static"
DAY_MS = 86400 * 1000

DONE_STATUSES = {"delivered", "cancelled", "returned", "refunded"}
SHOP_ORDER = {"problem": 0, "out_for_delivery": 1, "shipped": 2, "ordered": 3}


def _row(r, labels, conn):
    data = json.loads(r["data"] or "{}")
    if r["category"] == "work" and data.get("needs_reply") and db.thread_last_is_mine(conn, r["thread_id"]):
        data["needs_reply"] = False  # I replied after it was classified
    return {
        "id": r["id"], "category": r["category"], "title": r["title"], "summary": r["summary"],
        "priority": r["priority"], "event_time": r["event_time"], "data": data,
        "account": r["account"], "account_label": labels.get(r["account"], r["account"]),
        "date": r["last_date"], "state": r["user_state"],
        "url": f"https://mail.google.com/mail/u/?authuser={r['account']}#all/{r['thread_id']}",
    }


_WORD = re.compile(r"[a-z0-9]{3,}|[\u4e00-\u9fff]")


def _words(s):
    return set(_WORD.findall((s or "").lower()))


def _ms(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000


def same_thing(appt_time, appt_title, ev):
    """Is a mail-extracted appointment the same as a calendar event?

    Gmail often adds flights/hotels/reservations to Calendar itself, so the
    same plan arrives twice. Timed: starts within 15 minutes. Date-only: same
    day and the titles share at least two words (one shared city name is
    not enough: "London trip" is not the "Marriott London" booking).
    """
    if not appt_time or not ev["start"]:
        return False
    if len(appt_time) > 10 and len(ev["start"]) > 10:
        return abs(_ms(appt_time) - _ms(ev["start"])) <= 15 * 60 * 1000
    if len(appt_time) == 10 and ev["start"][:10] == appt_time:
        return len(_words(appt_title) & _words(ev["title"])) >= 2
    return False


def calendar_entries(cfg, conn, labels, appts):
    """Calendar events plus mail appointments, one entry per real-world plan."""
    entries, seen = [], set()
    for e in conn.execute("SELECT * FROM events ORDER BY start, account").fetchall():
        sig = (e["title"], e["start"])
        if sig in seen:  # shared invite present in several of my calendars
            continue
        seen.add(sig)
        entries.append({
            "source": "calendar", "title": e["title"], "start": e["start"], "end": e["end"],
            "all_day": bool(e["all_day"]), "location": e["location"], "url": e["url"],
            "meet_url": e["meet_url"], "color": e["color"], "calendar": e["calendar_name"],
            "account": e["account"], "account_label": labels.get(e["account"], e["account"]),
        })
    events = list(entries)
    for it in appts:
        if it["data"].get("status") == "cancelled" or not it["event_time"]:
            continue
        if any(same_thing(it["event_time"], it["title"], ev) for ev in events):
            it["in_calendar"] = True
            continue
        end = it["data"].get("end")
        entries.append({
            "source": "mail", "title": it["title"], "start": it["event_time"],
            "end": end if end and len(end) == len(it["event_time"]) else None,
            "all_day": len(it["event_time"]) == 10, "location": it["data"].get("location"),
            "url": it["url"], "kind": it["data"].get("kind"), "item_id": it["id"],
            "account": it["account"], "account_label": it["account_label"],
        })
    entries.sort(key=lambda x: x["start"])
    return entries


def dashboard(cfg, conn):
    labels = {a.email: a.label for a in cfg.accounts}
    now = datetime.now(timezone.utc)
    now_ms = db.now_ms()
    today = datetime.now(cfg.tz).date().isoformat()
    cutoff = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def q(cat, days):
        rows = conn.execute(
            "SELECT * FROM items WHERE category = ? AND user_state = 'open' AND last_date > ?",
            (cat, now_ms - days * DAY_MS)).fetchall()
        return [_row(r, labels, conn) for r in rows]

    def upcoming(it):
        t = it["event_time"]
        if not t:
            return it["date"] > now_ms - 7 * DAY_MS
        return t >= (today if len(t) == 10 else cutoff)

    all_appts = q("appointment", 120)
    calendar = calendar_entries(cfg, conn, labels, all_appts)
    appts = [i for i in all_appts if upcoming(i)]
    appts.sort(key=lambda i: i["event_time"] or "9999")

    shop = [i for i in q("shopping", 45)
            if i["data"].get("status") not in DONE_STATUSES or i["date"] > now_ms - 3 * DAY_MS]
    shop.sort(key=lambda i: (SHOP_ORDER.get(i["data"].get("status"), 5),
                             i["event_time"] or "9999", -i["date"]))

    work = q("work", 30)
    work.sort(key=lambda i: (not i["data"].get("needs_reply"), i["priority"] != "high", -i["date"]))

    outreach = sorted(q("outreach", 30), key=lambda i: -i["date"])

    deals = [i for i in q("deal", 21) if not i["event_time"] or i["event_time"] >= today]
    deals.sort(key=lambda i: (i["priority"] != "high", i["event_time"] or "9999", -i["date"]))

    ads = sorted(q("ad", 3), key=lambda i: -i["date"])[:80]

    accounts = []
    for a in cfg.accounts:
        st = db.account_state(conn, a.email)
        accounts.append({"email": a.email, "label": a.label,
                         "last_sync": st["last_sync"] if st else None,
                         "error": st["last_error"] if st else "not synced yet"})
    pending = conn.execute("SELECT COUNT(*) FROM messages WHERE state = 0").fetchone()[0]

    return {"generated": now_ms, "timezone": cfg.tz.key, "accounts": accounts, "pending": pending,
            "calendar": calendar,
            "sections": {"appointment": appts, "shopping": shop, "work": work,
                         "outreach": outreach, "deal": deals, "ad": ads}}


def make_handler(cfg):
    verifier = (AccessVerifier(cfg.access_team, cfg.access_aud, cfg.access_emails)
                if cfg.access_required else None)
    if cfg.access_required and not (cfg.access_team and cfg.access_aud and cfg.access_emails):
        raise SystemExit("[web.access] required = true 但 team/aud/emails 没填全")

    class Handler(BaseHTTPRequestHandler):
        server_version = "mail-hub"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if verifier is None:
                return True
            token = self.headers.get("Cf-Access-Jwt-Assertion", "")
            if token and verifier.verify(token):
                return True
            self._send(403, {"error": "forbidden"})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/dashboard":
                conn = db.connect(cfg.db_path)
                try:
                    return self._send(200, dashboard(cfg, conn))
                finally:
                    conn.close()
            if path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            # A custom header forces a CORS preflight, so other sites can't POST here
            # with the Access cookie.
            if self.headers.get("X-MailHub") != "1":
                return self._send(400, {"error": "missing header"})
            m = re.fullmatch(r"/api/items/(\d+)/(done|open)", self.path)
            if not m:
                return self._send(404, {"error": "not found"})
            conn = db.connect(cfg.db_path)
            try:
                db.set_item_state(conn, int(m.group(1)), m.group(2))
                conn.commit()
            finally:
                conn.close()
            self._send(200, {"ok": True})

    return Handler


def serve(cfg):
    srv = ThreadingHTTPServer((cfg.web_host, cfg.web_port), make_handler(cfg))
    log.info("serving on http://%s:%d", cfg.web_host, cfg.web_port)
    srv.serve_forever()
