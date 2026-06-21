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

Durable state: chat_id + per-agent topic_ids are also mirrored to
/var/lib/attosys/<org>.yaml. uninstall.sh drops this by default (true clean
slate); --keep-state preserves it so a reinstall reuses existing forum
topics instead of minting duplicates (Telegram has no "list topics" API
and createForumTopic doesn't fail on duplicate names). Delete that file
(or the whole /var/lib/attosys) to force fresh topic creation.
"""
import json
import os
import pathlib
import sys
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent
COMPANY = ROOT / "company.yaml"
SECRETS = ROOT / "secrets.yaml"
PLACEHOLDER_CHAT = "-1001234567890"

# Durable state survives uninstall.sh (which only removes the repo's
# company.yaml/secrets.yaml). Holding chat_id + topic_ids here means a
# reinstall reuses existing forum topics instead of minting duplicates.
STATE_DIR = pathlib.Path("/var/lib/attosys")

# Exit code signalling "Telegram group not ready yet" — setup.sh treats this
# as a soft skip (installs mux + proxy, defers hiring) so the user can re-run
# setup.sh once they've added the bot to a supergroup with Topics.
EXIT_NOT_READY = 2

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
        err = RuntimeError(f"{method} failed: {data}")
        err.response = data  # attach the raw Telegram response for callers
        raise err
    return data["result"]


def validate_bot():
    me = call("getMe", "GET")
    if not me.get("can_read_all_group_messages"):
        sys.exit("bot privacy is ON — in @BotFather: /setprivacy -> Disable, then rerun")
    print(f"bot @{me['username']} ok (privacy off)")
    return me


def discover_chat():
    """Return the supergroup chat id: from config if set, else by watching for
    any activity in a supergroup the bot is in.

    Detection works via any of: the bot being added to a group (my_chat_member
    event), or anyone sending a message in a group the bot is already in
    (message event). So if the bot is already a member of a supergroup, the
    user just sends any message there — no need to remove/re-add the bot.

    Only supergroups are accepted — Topics don't work on basic groups. If the
    user adds the bot to a basic group, we tell them to convert it and exit
    EXIT_NOT_READY so they can re-run once fixed. Ctrl-C also defers.
    """
    cid = str(company.get("telegram_chat_id") or "")
    if cid and cid != PLACEHOLDER_CHAT:
        return cid
    print("Looking for your supergroup. If the bot is already a member,\n"
          "just send any message in that supergroup — I'll detect it.\n"
          "If not, add the bot as admin to a supergroup with Topics enabled.\n"
          "  (Ctrl-C to defer; re-run `sudo ./setup.sh` later)")
    allowed = json.dumps(["message", "my_chat_member", "channel_post"])
    try:
        while True:
            # Long-poll Telegram for up to 25s. getUpdates can occasionally
            # time out on the wire (slow network, Telegram hiccup); retry the
            # poll instead of crashing the whole setup with a traceback.
            try:
                ups = call("getUpdates", data={"timeout": 25, "allowed_updates": allowed})
            except requests.exceptions.RequestException as e:
                print(f"  getUpdates poll error ({e.__class__.__name__}), retrying...")
                time.sleep(2)
                continue
            except RuntimeError as e:
                # 409 Conflict = another getUpdates is running (e.g. the mux
                # shutting down). Retry after a brief wait.
                if "409" in str(e) or "Conflict" in str(e):
                    print("  getUpdates conflict (another instance running), retrying...")
                    time.sleep(2)
                    continue
                raise
            for u in ups:
                obj = u.get("my_chat_member") or u.get("message") or u.get("channel_post") or {}
                chat = obj.get("chat") or {}
                ctype = chat.get("type")
                if ctype == "supergroup":
                    print(f"detected supergroup: {chat.get('title')!r} ({chat['id']})")
                    return str(chat["id"])
                if ctype == "group":
                    print(f"detected basic group: {chat.get('title')!r} ({chat['id']})")
                    sys.exit(
                        "This is a basic group, not a supergroup. Topics only work on\n"
                        "supergroups. In Telegram: open the group → Edit → Convert to\n"
                        "Supergroup, then re-run `sudo ./setup.sh`.")
    except KeyboardInterrupt:
        print("\nDeferred — no group detected. Re-run `sudo ./setup.sh` after adding\n"
              "the bot as admin to a supergroup with Topics enabled.")
        sys.exit(EXIT_NOT_READY)


def create_topics(chat_id):
    """Create one forum topic per agent that lacks a topic_id.

    Common failures (bot not admin, bot kicked, Topics disabled) exit with
    EXIT_NOT_READY and a clear message so the user can fix the group and
    re-run setup.sh instead of hitting a traceback.
    """
    changed = False
    for role, spec in (company.get("agents") or {}).items():
        if spec.get("topic_id"):
            continue
        name = f"{ORG}-{role}"
        try:
            res = call("createForumTopic", data={"chat_id": chat_id, "name": name})
        except RuntimeError as e:
            msg = str(e).lower()
            resp = getattr(e, "response", None) or {}
            desc = (resp.get("description") or "").lower()
            if "kicked" in desc or "not a member" in desc:
                print(f"Bot was kicked or is not a member of the supergroup ({chat_id}).\n"
                      "Re-add it as admin with 'Manage Topics', then re-run `sudo ./setup.sh`.")
                sys.exit(EXIT_NOT_READY)
            if "manage" in msg or "rights" in msg or "admin" in msg or "forbidden" in desc:
                print(f"Bot lacks permission to manage topics in the supergroup ({chat_id}).\n"
                      "Promote the bot to admin with 'Manage Topics', then re-run `sudo ./setup.sh`.")
                sys.exit(EXIT_NOT_READY)
            raise
        spec["topic_id"] = int(res["message_thread_id"])
        print(f"  topic for {name}: {spec['topic_id']}")
        changed = True
    return changed, chat_id


def main():
    validate_bot()
    merge_durable_state()          # reuse chat_id + topic_ids from a prior install
    chat_id = discover_chat()
    company["telegram_chat_id"] = chat_id
    # Persist chat_id immediately so a crash in create_topics (or a Ctrl-C)
    # doesn't lose it and force re-discovery on the next run.
    COMPANY.write_text(yaml.safe_dump(company, sort_keys=False, allow_unicode=True))
    changed, chat_id = create_topics(chat_id)
    COMPANY.write_text(yaml.safe_dump(company, sort_keys=False, allow_unicode=True))
    save_durable_state(chat_id)    # mirror so a reinstall reuses these topics
    print(f"wrote {COMPANY} (chat_id + {('new ' if changed else 'no new ')}topic_ids)")
    print("next: sudo ./hire.py " + " ".join(company.get("agents") or {}))


def merge_durable_state():
    """Pull chat_id + per-agent topic_ids from /var/lib/attosys/<org>.yaml into
    the in-memory company when company.yaml lacks them. This is what makes a
    reinstall reuse existing topics instead of creating duplicates."""
    state_file = STATE_DIR / f"{ORG}.yaml"
    if not state_file.exists():
        return
    try:
        st = yaml.safe_load(state_file.read_text()) or {}
    except Exception:
        return
    if not company.get("telegram_chat_id") or company["telegram_chat_id"] == PLACEHOLDER_CHAT:
        if st.get("chat_id"):
            company["telegram_chat_id"] = str(st["chat_id"])
            print(f"  reused chat_id from durable state: {st['chat_id']}")
    for role, spec in (company.get("agents") or {}).items():
        if spec.get("topic_id"):
            continue
        tid = (st.get("agents") or {}).get(role)
        if tid:
            spec["topic_id"] = int(tid)
            print(f"  reused topic_id for {ORG}-{role} from durable state: {tid}")


def save_durable_state(chat_id):
    """Mirror chat_id + per-agent topic_ids to /var/lib/attosys/<org>.yaml."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "org": ORG,
        "chat_id": str(chat_id),
        "agents": {role: spec["topic_id"] for role, spec in (company.get("agents") or {}).items()
                   if spec.get("topic_id") is not None},
    }
    (STATE_DIR / f"{ORG}.yaml").write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=True))
    os.chmod(STATE_DIR / f"{ORG}.yaml", 0o600)


if __name__ == "__main__":
    main()
