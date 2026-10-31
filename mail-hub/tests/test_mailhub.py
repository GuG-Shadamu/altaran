import base64
import hashlib
import json
import random
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from mailhub import access, classify, db, demo, gmail, web
from mailhub.config import Account, Config


def make_cfg(tmp, **kw):
    base = dict(
        path=Path(tmp) / "config.toml", db_path=Path(tmp) / "t.db", tz=ZoneInfo("America/Toronto"),
        lookback_days=14, max_initial_messages=100,
        accounts=[Account("a@gmail.com", "A"), Account("b@gmail.com", "B")],
        client_secrets=Path(tmp) / "cs.json", token_dir=Path(tmp) / "tokens",
        llm={"backend": "claude_cli", "batch_size": 2}, classify_per_tick=50,
        web_host="127.0.0.1", web_port=0, access_required=False, access_team="", access_aud="",
        access_emails=[], discord_webhook="", about_me="", deal_interests="",
    )
    base.update(kw)
    return Config(**base)


def msg(id, thread="t1", date=1000, sent=0, labels="INBOX", account="a@gmail.com"):
    return {"id": id, "account": account, "thread_id": thread, "internal_date": date,
            "from_addr": "x@y.com", "from_name": "X", "to_addr": "", "subject": f"s{id}",
            "snippet": "snip", "labels": labels, "body": "body", "bulk": 0, "is_sent": sent,
            "state": 2 if sent else 0}


class ParseTest(unittest.TestCase):
    def test_parse_output_with_noise(self):
        text = 'Sure!\n```json\n{"items": [{"id": "1", "category": "ad"}, {"nope": 1}]}\n```'
        self.assertEqual(classify.parse_output(text), [{"id": "1", "category": "ad"}])

    def test_parse_output_strips_think(self):
        text = '<think>{"items": []} hmm</think>{"items": [{"id": "9"}]}'
        self.assertEqual(classify.parse_output(text), [{"id": "9"}])

    def test_parse_output_rejects_garbage(self):
        with self.assertRaises(ValueError):
            classify.parse_output("no json here")

    def test_normalize_time(self):
        tz = ZoneInfo("America/Toronto")
        self.assertEqual(classify.normalize_time("2026-11-13T18:30:00-05:00", tz), "2026-11-13T23:30:00Z")
        self.assertEqual(classify.normalize_time("2026-11-13T18:30:00", tz), "2026-11-13T23:30:00Z")
        self.assertEqual(classify.normalize_time("2026-11-13", tz), "2026-11-13")
        self.assertIsNone(classify.normalize_time("next tuesday", tz))

    def test_extract_body_prefers_plain_and_falls_back_to_html(self):
        enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": enc("<style>x{}</style><p>Hello&nbsp;<b>there</b></p><p>Row 2</p>")}},
        ]}
        self.assertEqual(gmail.extract_body(payload), "Hello there\nRow 2")
        payload["parts"].append({"mimeType": "text/plain", "body": {"data": enc("Plain text version that is long enough to win")}})
        self.assertEqual(gmail.extract_body(payload), "Plain text version that is long enough to win")


class ItemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def item(self, date, **kw):
        it = {"key": "shopping:order:1", "category": "shopping", "account": "a@gmail.com",
              "title": "t", "summary": "", "data": {}, "message_id": "m", "thread_id": "t",
              "last_date": date}
        it.update(kw)
        return it

    def test_merge_keeps_newest(self):
        self.assertEqual(db.upsert_item(self.conn, self.item(200, title="shipped")), "new")
        self.assertEqual(db.upsert_item(self.conn, self.item(100, title="ordered")), "stale")
        self.assertEqual(db.upsert_item(self.conn, self.item(300, title="delivered")), "updated")
        row = self.conn.execute("SELECT title FROM items").fetchone()
        self.assertEqual(row["title"], "delivered")

    def test_newer_mail_reopens_done_item(self):
        db.upsert_item(self.conn, self.item(100))
        item_id = self.conn.execute("SELECT id FROM items").fetchone()[0]
        db.set_item_state(self.conn, item_id, "done")
        db.upsert_item(self.conn, self.item(db.now_ms() + 1000))
        self.assertEqual(self.conn.execute("SELECT user_state FROM items").fetchone()[0], "open")

    def test_work_key_is_thread_and_my_reply_clears_needs_reply(self):
        m = msg("1", thread="T", date=100)
        db.upsert_message(self.conn, m)
        db.upsert_message(self.conn, msg("2", thread="T", date=200, sent=1))
        out = {"id": "1", "category": "work", "title": "x", "key": "ignored", "needs_reply": True}
        it = classify.to_item(self.cfg, self.conn, m, out)
        self.assertEqual(it["key"], "work:thread:a@gmail.com:T")
        self.assertFalse(it["data"]["needs_reply"])

    def test_other_is_dropped_and_unknown_category_is_other(self):
        m = msg("1")
        self.assertIsNone(classify.to_item(self.cfg, self.conn, m, {"id": "1", "category": "other"}))
        self.assertIsNone(classify.to_item(self.cfg, self.conn, m, {"id": "1", "category": "weird"}))

    def test_classify_pending_with_fake_llm(self):
        for i in range(3):
            db.upsert_message(self.conn, msg(str(i), thread=f"t{i}", date=100 + i))
        db.upsert_message(self.conn, msg("p", thread="tp", date=50, labels="INBOX,CATEGORY_PROMOTIONS"))
        self.conn.commit()

        def fake(cfg, prompt):
            ids = [e["id"] for e in json.loads(prompt[prompt.index("邮件：\n") + 4:])]
            # Leave message "2" out to exercise the retry path.
            return json.dumps({"items": [
                {"id": i, "category": {"0": "appointment", "1": "shopping", "p": "deal"}[i],
                 "title": f"T{i}", "start": "2026-11-13T09:00:00", "status": "shipped",
                 "key": f"k{i}", "expires": "2026-11-20"}
                for i in ids if i != "2"]})

        with mock.patch.object(classify.llm, "complete", side_effect=fake) as m:
            stats = classify.classify_pending(self.cfg, self.conn)
        self.assertEqual(m.call_count, 3)  # batch_size 2 -> two normal batches + one promo batch
        self.assertEqual(stats["new"], 3)
        cats = dict(self.conn.execute("SELECT category, event_time FROM items").fetchall())
        self.assertEqual(cats["appointment"], "2026-11-13T14:00:00Z")
        self.assertEqual(cats["deal"], "2026-11-20")
        st = self.conn.execute("SELECT state, attempts FROM messages WHERE id = '2'").fetchone()
        self.assertEqual((st["state"], st["attempts"]), (0, 1))

    def test_failed_batches_give_up_after_three_attempts(self):
        db.upsert_message(self.conn, msg("1"))
        self.conn.commit()
        with mock.patch.object(classify.llm, "complete", side_effect=RuntimeError("boom")):
            for _ in range(3):
                classify.classify_pending(self.cfg, self.conn)
        self.assertEqual(self.conn.execute("SELECT state FROM messages").fetchone()[0], 3)


# --- Cloudflare Access JWT -------------------------------------------------------

def _is_prime(n):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    for _ in range(20):
        x = pow(random.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _rsa_key(bits=1024):
    e = 65537
    while True:
        p, q = (next(c for c in iter(lambda: random.getrandbits(bits // 2) | (1 << (bits // 2 - 1)) | 1, None)
                     if _is_prime(c)) for _ in range(2))
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(n, d, header, payload):
    msg = f"{_b64e(json.dumps(header).encode())}.{_b64e(json.dumps(payload).encode())}"
    k = (n.bit_length() + 7) // 8
    t = access._SHA256_PREFIX + hashlib.sha256(msg.encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return f"{msg}.{_b64e(sig)}"


class AccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        random.seed(7)
        cls.n, cls.e, cls.d = _rsa_key()
        jwk = {"kid": "k1", "kty": "RSA", "n": _b64e(cls.n.to_bytes((cls.n.bit_length() + 7) // 8, "big")),
               "e": _b64e(cls.e.to_bytes(3, "big"))}
        cls.v = access.AccessVerifier("team", "AUD", ["me@x.com"], fetch=lambda: {"keys": [jwk]})

    def token(self, **kw):
        payload = {"aud": ["AUD"], "iss": "https://team.cloudflareaccess.com", "email": "me@x.com",
                   "exp": time.time() + 600}
        payload.update(kw)
        return _sign(self.n, self.d, {"alg": "RS256", "kid": "k1"}, payload)

    def test_valid(self):
        self.assertEqual(self.v.verify(self.token()), "me@x.com")

    def test_rejections(self):
        self.assertIsNone(self.v.verify(self.token(aud=["other"])))
        self.assertIsNone(self.v.verify(self.token(email="someone@else.com")))
        self.assertIsNone(self.v.verify(self.token(exp=time.time() - 5)))
        self.assertIsNone(self.v.verify(self.token(iss="https://evil.cloudflareaccess.com")))
        h, p, s = self.token().split(".")
        forged = json.loads(base64.urlsafe_b64decode(p + "=="))
        forged["email"] = "me@x.com"
        forged["aud"] = ["AUD"]
        tampered = f"{h}.{_b64e(json.dumps({**forged, 'x': 1}).encode())}.{s}"
        self.assertIsNone(self.v.verify(tampered))
        self.assertIsNone(self.v.verify("garbage"))
        self.assertIsNone(self.v.verify(self.token().rsplit(".", 1)[0] + ".AAAA"))


# --- HTTP ------------------------------------------------------------------------

class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        demo.seed(self.cfg)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, path, method="GET", headers=None):
        r = urllib.request.Request(self.base + path, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_dashboard_and_done(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Mail Hub", body)
        code, body = self.req("/api/dashboard")
        data = json.loads(body)
        secs = data["sections"]
        self.assertEqual(set(secs), {"appointment", "shopping", "work", "outreach", "deal", "ad"})
        self.assertTrue(all(len(v) for v in secs.values()))
        # out_for_delivery and problem sort before shipped
        self.assertIn(secs["shopping"][0]["data"]["status"], ("problem", "out_for_delivery"))
        target = secs["work"][0]["id"]

        self.assertEqual(self.req(f"/api/items/{target}/done", "POST")[0], 400)  # no CSRF header
        self.assertEqual(self.req(f"/api/items/{target}/done", "POST", {"X-MailHub": "1"})[0], 200)
        secs = json.loads(self.req("/api/dashboard")[1])["sections"]
        self.assertNotIn(target, [i["id"] for i in secs["work"]])

    def test_access_required_blocks_without_jwt(self):
        cfg = make_cfg(self.tmp.name, access_required=True, access_team="t", access_aud="a",
                       access_emails=["me@x.com"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/dashboard")
            self.assertEqual(ctx.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
