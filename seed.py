#!/usr/bin/env python3
"""Seed the company's Telegram side: discover the supergroup, create one forum
topic per agent, and write the ids back into company.yaml.

Usage: ./seed.py

Needs company.yaml (org + agents) and secrets.yaml (telegram_bot_token). A
bot cannot create a Telegram group, so the one manual step is: make a
supergroup with Topics enabled and add the bot as admin. seed.py detects the
group automatically (from the bot-added event — no message needed), then
creates a topic named after each agent and records chat_id + topic_id.

Idempotent: agents that already have a topic_id are skipped; an existing
telegram_chat_id is reused.
"""
import json
import pathlib
import sys
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent
COMPANY = ROOT / "company.yaml"
SECRETS = ROOT / "secrets.yaml"
PLACEHOLDER_CHAT = "-1001234567890"

for f in (COMPANY, SECRETS):
    if not f.exists():
        sys.exit(f"missing {f} — copy the .example and fill it in")

company = yaml.safe_load(COMPANY.read_text())
secrets = yaml.safe_load(SECRETS.read_text())
ORG = company["org"]
TOKEN = secrets.get("telegram_bot_token")
if not TOKEN or TOKEN.startswith("1111111111:"):
    sys.exit("set telegram_bot_token in secrets.yaml")
API = f"https://api.telegram.org/bot{TOKEN}"


def call(method, http="POST", **kw):
    fn = requests.post if http == "POST" else requests.get
    data = fn(f"{API}/{method}", timeout=40, **kw).json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data}")
    return data["result"]


def validate_bot():
    me = call("getMe", "GET")
    if not me.get("can_read_all_group_messages"):
        sys.exit("bot privacy is ON — in @BotFather: /setprivacy -> Disable, then rerun")
    print(f"bot @{me['username']} ok (privacy off)")
    return me


def discover_chat():
    """Return the supergroup chat id: from config if set, else by watching for
    the bot to be added to a group (no message from anyone required)."""
    cid = str(company.get("telegram_chat_id") or "")
    if cid and cid != PLACEHOLDER_CHAT:
        return cid
    print("No group configured yet. Add the bot as ADMIN to a supergroup with\n"
          "Topics enabled — I'll detect it automatically (Ctrl-C to abort)...")
    allowed = json.dumps(["message", "my_chat_member", "channel_post"])
    while True:
        ups = call("getUpdates", data={"timeout": 25, "allowed_updates": allowed})
        for u in ups:
            obj = u.get("my_chat_member") or u.get("message") or u.get("channel_post") or {}
            chat = obj.get("chat") or {}
            if chat.get("type") in ("supergroup", "group"):
                print(f"detected group: {chat.get('title')!r} ({chat['id']})")
                return str(chat["id"])


def create_topics(chat_id):
    """Create one forum topic per agent that lacks a topic_id."""
    changed = False
    for role, spec in (company.get("agents") or {}).items():
        if spec.get("topic_id"):
            continue
        name = f"{ORG}-{role}"
        res = call("createForumTopic", data={"chat_id": chat_id, "name": name})
        spec["topic_id"] = int(res["message_thread_id"])
        print(f"  topic for {name}: {spec['topic_id']}")
        changed = True
    return changed


def main():
    validate_bot()
    chat_id = discover_chat()
    company["telegram_chat_id"] = chat_id
    try:
        changed = create_topics(chat_id)
    except RuntimeError as e:
        if "manage" in str(e).lower() or "rights" in str(e).lower() or "admin" in str(e).lower():
            sys.exit(f"{e}\n-> the bot must be admin with 'Manage Topics' in the group")
        raise
    COMPANY.write_text(yaml.safe_dump(company, sort_keys=False, allow_unicode=True))
    print(f"wrote {COMPANY} (chat_id + {('new ' if changed else 'no new ')}topic_ids)")
    print("next: sudo ./hire.py " + " ".join(company.get("agents") or {}))


if __name__ == "__main__":
    main()
