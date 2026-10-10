"""Discord notifications through a channel webhook (plan.md §17). One function, ``send()``.

    from pipeline_v2 import notify
    notify.send("Bulk download started", "91,469 files (38.9 GB)", "info", {"Connections": 3})

    python -m pipeline_v2.notify                  # send a test message
    python -m pipeline_v2.notify --level error    # test message that @-mentions you

Reads ``DISCORD_WEBHOOK_URL`` (and optionally ``DISCORD_USER_ID``) from pipeline_v2/.env. Each message
is an embed: a title, a colored bar per level, optional fields, and the sending machine's name in the
footer. ``warn`` and ``error`` messages @-mention DISCORD_USER_ID so they push to your phone.

A notification can never break the pipeline: a missing URL, Discord being down or a rejected message
logs a warning and returns False. Standard library only, so the downloader stays dependency-free.
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import socket
import urllib.error
import urllib.request

from pipeline_v2 import config

log = logging.getLogger("notify")

COLORS = {"info": 0x3B82F6, "ok": 0x22C55E, "warn": 0xEAB308, "error": 0xEF4444}  # blue/green/yellow/red
MENTION = {"warn", "error"}       # levels that @-mention DISCORD_USER_ID
USER_AGENT = "delispice-pipeline (notify.py)"   # Discord rejects urllib's default User-Agent
EMBED_MAX = 6000                  # Discord's limit for all text in one embed; longer messages are refused


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + "…"


def send(title: str, message: str = "", level: str = "info", fields: dict | None = None) -> bool:
    """Post one embed to the webhook. True if Discord accepted it; never raises."""
    if os.environ.get("PIPELINE_V2_NOTIFY") == "0":      # tests: PIPELINE_V2_NOTIFY=0 mutes Discord
        log.info("notifications off (PIPELINE_V2_NOTIFY=0); not sent: %s", title)
        return False
    url = None
    try:
        s = config.secrets() if config.SECRETS_ENV.exists() else {}
        url = s.get("DISCORD_WEBHOOK_URL")
        if not url:
            log.warning("no DISCORD_WEBHOOK_URL in %s; not sent: %s", config.SECRETS_ENV.name, title)
            return False
        if not url.startswith("https://"):
            log.warning("DISCORD_WEBHOOK_URL must start with https:// (no quotes); not sent: %s", title)
            return False
        footer = socket.gethostname()
        embed = {"title": _cut(title, 256), "color": COLORS.get(level, COLORS["info"]),
                 "footer": {"text": footer}}
        used = len(embed["title"]) + len(footer)
        for k, v in list((fields or {}).items())[:25]:   # Discord allows 25 fields
            name, value = _cut(str(k), 256), _cut(str(v), 1024) or "-"
            if used + len(name) + len(value) > EMBED_MAX - 500:   # leave room for the message
                break
            embed.setdefault("fields", []).append({"name": name, "value": value, "inline": True})
            used += len(name) + len(value)
        if message:
            embed["description"] = _cut(message, min(4096, EMBED_MAX - used))
        body: dict = {"embeds": [embed]}
        user = s.get("DISCORD_USER_ID")
        if user and level in MENTION:     # a mention only pings from the message text, not the embed
            body["content"] = f"<@{user}>"
            body["allowed_mentions"] = {"users": [user]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except urllib.error.HTTPError as e:   # Discord's reply says what it didn't like; never log the URL
        log.warning("Discord refused %r: HTTP %s %s", title, e.code, e.read()[:300].decode(errors="replace"))
    except Exception as e:
        detail = str(e).replace(url, "<webhook URL>") if url else str(e)
        log.warning("Discord notification %r not sent: %s: %s", title, type(e).__name__, detail)
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Send a test message to the Discord webhook")
    ap.add_argument("--level", choices=sorted(COLORS), default="info")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = send(f"Test message ({a.level})", "If you can read this, pipeline_v2 notifications work.",
              a.level, {"Level": a.level, "Mentions you": "yes" if a.level in MENTION else "no"})
    print("sent" if ok else "NOT sent (see the warning above)")
    raise SystemExit(0 if ok else 1)
