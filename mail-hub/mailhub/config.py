"""Load config.toml. Paths with ~ are expanded; secrets come from env vars."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PATH = "~/.config/mail-hub/config.toml"


def _p(s):
    return Path(os.path.expanduser(s))


@dataclass
class Account:
    email: str
    label: str


@dataclass
class Config:
    path: Path
    db_path: Path
    tz: ZoneInfo
    lookback_days: int
    max_initial_messages: int
    accounts: list
    client_secrets: Path
    token_dir: Path
    llm: dict
    classify_per_tick: int
    web_host: str
    web_port: int
    access_required: bool
    access_team: str
    access_aud: str
    access_emails: list
    discord_webhook: str
    about_me: str
    deal_interests: str
    raw: dict = field(repr=False, default_factory=dict)

    def account(self, email):
        for a in self.accounts:
            if a.email.lower() == email.lower():
                return a
        return None


def load(path=None):
    path = _p(path or os.environ.get("MAILHUB_CONFIG") or DEFAULT_PATH)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    g = raw.get("general", {})
    goog = raw.get("google", {})
    llm = raw.get("llm", {})
    web = raw.get("web", {})
    acc = web.get("access", {})
    disc = raw.get("discord", {})
    prefs = raw.get("prefs", {})

    accounts = [Account(a["email"], a.get("label") or a["email"].split("@")[0])
                for a in raw.get("accounts", [])]

    return Config(
        path=path,
        db_path=_p(g.get("db_path", "~/.local/share/mail-hub/mailhub.db")),
        tz=ZoneInfo(g.get("timezone", "America/Toronto")),
        lookback_days=int(g.get("lookback_days", 14)),
        max_initial_messages=int(g.get("max_initial_messages", 300)),
        accounts=accounts,
        client_secrets=_p(goog.get("client_secrets", "~/.config/mail-hub/client_secret.json")),
        token_dir=_p(goog.get("token_dir", "~/.config/mail-hub/tokens")),
        llm=llm,
        classify_per_tick=int(llm.get("max_messages_per_tick", 120)),
        web_host=web.get("host", "127.0.0.1"),
        web_port=int(web.get("port", 8095)),
        access_required=bool(acc.get("required", True)),
        access_team=acc.get("team", ""),
        access_aud=acc.get("aud", ""),
        access_emails=[e.lower() for e in acc.get("emails", [])],
        discord_webhook=os.environ.get(disc.get("webhook_url_env", "MAILHUB_DISCORD_WEBHOOK"), ""),
        about_me=prefs.get("about_me", ""),
        deal_interests=prefs.get("deal_interests", ""),
        raw=raw,
    )
