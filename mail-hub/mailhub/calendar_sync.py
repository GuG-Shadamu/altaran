"""Pull Google Calendar events for every account into the events table."""
import logging
from datetime import datetime, timedelta, timezone

from . import db
from .gmail import CALENDAR_SCOPE, Gmail, GmailError

log = logging.getLogger("mailhub.calendar")

# Generated calendars that would drown out real plans.
AUTO_CALENDARS = ("#contacts@group.v.calendar.google.com", "#holiday@group.v.calendar.google.com",
                  "#weeknum@group.v.calendar.google.com")


def _utc(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_event(account, cal, ev):
    """Calendar API event -> events row, or None for cancelled/declined ones."""
    if ev.get("status") == "cancelled":
        return None
    me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
    if me and me.get("responseStatus") == "declined":
        return None
    start, end = ev.get("start", {}), ev.get("end", {})
    all_day = "date" in start
    try:
        s = start["date"] if all_day else _utc(start["dateTime"])
        e = end.get("date") if all_day else (_utc(end["dateTime"]) if "dateTime" in end else None)
    except (KeyError, ValueError):
        return None
    meet = ev.get("hangoutLink") or next(
        (p.get("uri") for p in ev.get("conferenceData", {}).get("entryPoints", [])
         if p.get("entryPointType") == "video"), None)
    return {
        "id": ev["id"], "account": account, "calendar_id": cal["id"],
        "calendar_name": cal.get("summaryOverride") or cal.get("summary", ""),
        "color": cal.get("backgroundColor"), "title": ev.get("summary") or "(无标题)",
        "start": s, "end": e, "all_day": int(all_day), "location": ev.get("location"),
        "url": ev.get("htmlLink"), "meet_url": meet,
    }


def sync_account(cfg, conn, email):
    g = Gmail(cfg, email)
    if not g.has_scope(CALENDAR_SCOPE):
        return None
    now = datetime.now(timezone.utc)
    tmin = (now - timedelta(days=cfg.calendar_days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmax = (now + timedelta(days=cfg.calendar_days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for cal in g.calendars():
        name = (cal.get("summaryOverride") or cal.get("summary", "")).lower()
        if (cal.get("hidden") or cal["id"].endswith(AUTO_CALENDARS)
                or name in cfg.calendar_exclude or cal["id"].lower() in cfg.calendar_exclude):
            continue
        try:
            items = g.events(cal["id"], tmin, tmax)
        except GmailError as e:
            log.warning("%s: calendar %s: %s", email, name, e)
            continue
        rows += [r for r in (parse_event(email, cal, ev) for ev in items) if r]
    # The same shared event shows up under several accounts; keep one copy per account.
    conn.execute("DELETE FROM events WHERE account = ?", (email,))
    conn.executemany(
        """INSERT OR REPLACE INTO events (id, account, calendar_id, calendar_name, color, title,
               start, end, all_day, location, url, meet_url)
           VALUES (:id, :account, :calendar_id, :calendar_name, :color, :title,
                   :start, :end, :all_day, :location, :url, :meet_url)""", rows)
    conn.commit()
    return len(rows)


def sync_all(cfg, conn):
    if not cfg.calendar_enabled:
        return
    for a in cfg.accounts:
        try:
            n = sync_account(cfg, conn, a.email)
            if n is None:
                log.info("%s: token has no calendar scope, run `auth` again to add it", a.email)
            else:
                log.info("%s: %d calendar events", a.email, n)
        except Exception as e:
            log.exception("%s: calendar sync failed", a.email)
            db.save_account_state(conn, a.email, error=f"calendar: {e}"[:500])
            conn.commit()
