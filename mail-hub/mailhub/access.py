"""Verify Cloudflare Access JWTs (RS256) with the standard library only.

Same policy as the workbench: the tunnel only forwards requests that passed
Access, and the app checks the signed assertion again so a request that
reaches the port some other way is still rejected.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request

# DER prefix of DigestInfo for SHA-256 (RFC 8017, section 9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rsa_sha256_verify(n, e, message, signature):
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


class AccessVerifier:
    def __init__(self, team, aud, emails, fetch=None):
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.aud = aud
        self.emails = {e.lower() for e in emails}
        self._fetch = fetch or self._fetch_certs
        self._keys = {}
        self._keys_at = 0
        self._lock = threading.Lock()

    def _fetch_certs(self):
        with urllib.request.urlopen(f"{self.issuer}/cdn-cgi/access/certs", timeout=10) as r:
            return json.load(r)

    def _key(self, kid):
        with self._lock:
            age = time.time() - self._keys_at
            if kid in self._keys and age < 3600:
                return self._keys[kid]
            if age < 30:
                return self._keys.get(kid)  # just refetched; don't hammer it on unknown kids
            certs = self._fetch()
            self._keys = {k["kid"]: (int.from_bytes(_b64d(k["n"]), "big"),
                                     int.from_bytes(_b64d(k["e"]), "big"))
                          for k in certs.get("keys", []) if k.get("kty") == "RSA"}
            self._keys_at = time.time()
            return self._keys.get(kid)

    def verify(self, token):
        """Return the verified email, or None."""
        try:
            h64, p64, s64 = token.split(".")
            header = json.loads(_b64d(h64))
            payload = json.loads(_b64d(p64))
            sig = _b64d(s64)
        except (ValueError, AttributeError):
            return None
        if header.get("alg") != "RS256":
            return None
        try:
            key = self._key(header.get("kid"))
        except OSError:
            return None
        if not key or not rsa_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode(), sig):
            return None
        now = time.time()
        aud = payload.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if (self.aud not in aud or payload.get("iss") != self.issuer
                or payload.get("exp", 0) < now or payload.get("nbf", 0) > now + 60):
            return None
        email = (payload.get("email") or "").lower()
        return email if email in self.emails else None
