"""Minimal Gmail API client (read-only) over urllib, with installed-app OAuth.

No google-api-python-client: it costs ~80 MB of RAM for a handful of REST calls.
"""
import base64
import hashlib
import html
import json
import os
import re
import secrets
import select
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = f"{GMAIL_SCOPE} {CALENDAR_SCOPE}"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL_API = "https://www.googleapis.com/calendar/v3"


class GmailError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GmailError(e.code, e.read().decode(errors="replace")) from None


def _client(cfg):
    with open(cfg.client_secrets) as f:
        c = json.load(f)
    c = c.get("installed") or c.get("web") or c
    return c["client_id"], c["client_secret"]


def _token_path(cfg, email):
    return cfg.token_dir / f"{email.lower()}.json"


def _save_token(cfg, email, tok):
    cfg.token_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.token_dir, 0o700)
    path = _token_path(cfg, email)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


# --- interactive authorization --------------------------------------------

def authorize(cfg, email):
    """Run the OAuth consent flow for one account and store its refresh token.

    Opens a loopback listener. If the browser is on another machine the
    redirect will fail to load; paste that URL from the address bar instead.
    """
    client_id, client_secret = _client(cfg)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                done.set()
                msg = "mail-hub: authorized, you can close this tab."
            else:
                msg = "mail-hub: unexpected request."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "include_granted_scopes": "true", "access_type": "offline", "prompt": "consent",
        "login_hint": email, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print(f"\n用 {email} 登录并授权（只读）：\n\n{url}\n")
    print("授权后浏览器会跳到 127.0.0.1。如果那个页面打不开（浏览器不在这台机器上），")
    print("把地址栏里的完整 URL 粘贴到这里回车：")
    while not done.is_set():
        r, _, _ = select.select([sys.stdin], [], [], 0.5)
        if r:
            line = sys.stdin.readline().strip()
            q = urllib.parse.parse_qs(urllib.parse.urlparse(line).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                break
            print("这个 URL 里没有有效的 code/state，再试一次：")
    srv.shutdown()

    tok = _post_form(TOKEN_URL, {
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if "refresh_token" not in tok:
        raise SystemExit("Google 没有返回 refresh_token，去 myaccount.google.com/permissions 删掉旧授权后重试。")

    g = Gmail(cfg, email, token={"refresh_token": tok["refresh_token"],
                                 "access_token": tok["access_token"],
                                 "scope": tok.get("scope", ""),
                                 "expires_at": time.time() + tok.get("expires_in", 3600) - 60})
    actual = g.profile()["emailAddress"]
    if actual.lower() != email.lower():
        raise SystemExit(f"授权的是 {actual}，不是 {email}。请用正确的账号重新授权。")
    _save_token(cfg, email, g.token)
    missing = [n for s, n in ((GMAIL_SCOPE, "Gmail"), (CALENDAR_SCOPE, "Calendar"))
               if s not in g.token["scope"]]
    if missing:
        print(f"⚠ {email} 已授权，但没有勾选：{', '.join(missing)}。需要的话重新跑一次 auth 并勾上。")
    else:
        print(f"✓ {email} 已授权（Gmail + Calendar，只读）")


# --- API client --------------------------------------------------------------

class Gmail:
    def __init__(self, cfg, email, token=None):
        self.cfg = cfg
        self.email = email
        if token is None:
            with open(_token_path(cfg, email)) as f:
                token = json.load(f)
        self.token = token

    def _access_token(self):
        if self.token.get("access_token") and self.token.get("expires_at", 0) > time.time():
            return self.token["access_token"]
        client_id, client_secret = _client(self.cfg)
        tok = _post_form(TOKEN_URL, {
            "refresh_token": self.token["refresh_token"], "client_id": client_id,
            "client_secret": client_secret, "grant_type": "refresh_token",
        })
        self.token["access_token"] = tok["access_token"]
        self.token["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        _save_token(self.cfg, self.email, self.token)
        return self.token["access_token"]

    def has_scope(self, scope):
        # Tokens from before calendar support carry no "scope" field: Gmail only.
        return scope in self.token.get("scope", GMAIL_SCOPE)

    def _get(self, path, base=API, **params):
        url = f"{base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._access_token()}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code in (429, 500, 503) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 401 and attempt == 0:
                    self.token["access_token"] = None
                    continue
                raise GmailError(e.code, body) from None

    def profile(self):
        return self._get("profile")

    def list_ids(self, query, limit):
        ids, page = [], None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(500, limit - len(ids))}
            if page:
                params["pageToken"] = page
            r = self._get("messages", **params)
            ids += [m["id"] for m in r.get("messages", [])]
            page = r.get("nextPageToken")
            if not page:
                break
        return ids

    def history(self, start_id):
        """Return (message ids added since start_id, latest history id).

        Raises GmailError(404) when start_id is too old; caller does a full resync.
        """
        ids, page, latest = [], None, start_id
        while True:
            params = {"startHistoryId": start_id, "historyTypes": "messageAdded", "maxResults": 500}
            if page:
                params["pageToken"] = page
            r = self._get("history", **params)
            for h in r.get("history", []):
                for ma in h.get("messagesAdded", []):
                    ids.append(ma["message"]["id"])
            latest = r.get("historyId", latest)
            page = r.get("nextPageToken")
            if not page:
                return list(dict.fromkeys(ids)), latest

    def message(self, msg_id):
        return self._get(f"messages/{msg_id}", format="full")

    def calendars(self):
        return self._get("users/me/calendarList", base=CAL_API, minAccessRole="reader").get("items", [])

    def events(self, calendar_id, time_min, time_max):
        out, page = [], None
        while True:
            params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if page:
                params["pageToken"] = page
            r = self._get(f"calendars/{urllib.parse.quote(calendar_id, safe='')}/events",
                          base=CAL_API, **params)
            out += r.get("items", [])
            page = r.get("nextPageToken")
            if not page or len(out) > 2000:
                return out


# --- parsing -----------------------------------------------------------------

def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


_TAGS = re.compile(r"<(script|style|head)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def html_to_text(s):
    s = re.sub(r"<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = html.unescape(_TAGS.sub(" ", s))
    s = re.sub(r"[ \t ‌​]+", " ", s)
    return "\n".join(line.strip() for line in s.splitlines() if line.strip())


def extract_body(payload):
    plain, rich = [], []

    def walk(p):
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_b64(data))
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    if plain and sum(len(x.strip()) for x in plain) > 40:
        text = "\n".join(plain)
    elif rich:
        text = html_to_text("\n".join(rich))
    else:
        text = "\n".join(plain)
    text = re.sub(r"https?://\S{60,}", "[link]", text)  # tracking URLs are noise
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_message(account, raw, body_limit=4000):
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    labels = raw.get("labelIds", [])
    bulk = int("list-unsubscribe" in headers or "CATEGORY_PROMOTIONS" in labels
               or headers.get("precedence", "").lower() in ("bulk", "list"))
    return {
        "id": raw["id"],
        "account": account,
        "thread_id": raw.get("threadId"),
        "internal_date": int(raw.get("internalDate", 0)),
        "from_addr": addr.lower(),
        "from_name": name,
        "to_addr": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "snippet": html.unescape(raw.get("snippet", "")),
        "labels": ",".join(labels),
        "body": extract_body(raw.get("payload", {}))[:body_limit],
        "bulk": bulk,
        "is_sent": int("SENT" in labels),
        "state": 0,
    }
