#!/usr/bin/env python3
"""Provision agents from company.yaml + secrets.yaml.

Usage: sudo ./hire.py <role> [<role> ...]        e.g. sudo ./hire.py hr labs

Per role: unix user <org>-<role>, /home/<user>/agent (config, soul,
triggers, mail_inbox), a sibling subconscious, permissions, a systemd unit
(enabled + started). Refuses roles that are already provisioned.

Run install.sh first. Renders {ROOT}/handbook.md and creates {ROOT}/shared/
on first use.
"""
import grp, json, os, pathlib, pwd, shutil, subprocess, sys

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent

if os.geteuid() != 0:
    sys.exit("run as root (sudo)")
if len(sys.argv) < 2:
    sys.exit(__doc__.strip())
for f in ("company.yaml", "secrets.yaml", "harness/agent.py", "venv/bin/python"):
    if not (ROOT / f).exists():
        sys.exit(f"missing {ROOT / f} — see install.sh / README")

company = yaml.safe_load((ROOT / "company.yaml").read_text())
secrets = yaml.safe_load((ROOT / "secrets.yaml").read_text())
ORG = company["org"]
NAME = company.get("name", ORG)
PREFIX = ORG
CEO = company["ceo"]["name"]
assert ORG.isalnum() and ORG.islower() and len(ORG) <= 12, \
    f"org must be lowercase alphanumeric, <=12 chars, got {ORG!r}"

os.chmod(ROOT / "secrets.yaml", 0o600)
os.chmod(ROOT / "company.yaml", 0o644)
# agents need to traverse into ROOT for the handbook, org chart and harness
mode = ROOT.stat().st_mode & 0o777
if mode & 0o005 != 0o005:
    sys.exit(f"{ROOT} is mode {oct(mode)} — agents can't reach it; chmod o+rx {ROOT} (and its parents)")

subprocess.run(["groupadd", "-f", PREFIX], check=True)
gid = grp.getgrnam(PREFIX).gr_gid

# One bot for the whole company; agents reach it through the mux. Validate
# the single token once (privacy must be off so it reads every topic).
TG_TOKEN = secrets.get("telegram_bot_token")
CHAT_ID = company.get("telegram_chat_id")
MUX_URL = company.get("mux_url", "http://127.0.0.1:8811")
# Agents reach the LLM through the proxy at proxy_url/<agent>/<provider>/v1
# (per-agent request logging). If proxy_url is unset, they talk to api_base.
PROXY_URL = company.get("proxy_url")
PROVIDER = company.get("provider")


def render(template, agent):
    text = (ROOT / template).read_text()
    for k, v in {"{{COMPANY}}": NAME, "{{company}}": PREFIX, "{{CEO}}": CEO,
                 "{{ROOT}}": str(ROOT), "{{AGENT}}": agent}.items():
        text = text.replace(k, v)
    return text


def assert_bot_settings(token):
    data = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
    if not data.get("ok"):
        sys.exit(f"getMe failed: {data}")
    bot = data["result"]
    issues = []
    if not bot.get("can_join_groups"):
        issues.append("In @BotFather: /setjoingroups -> Enable.")
    if not bot.get("can_read_all_group_messages"):
        issues.append("In @BotFather: /setprivacy -> Disable (privacy mode must be OFF).")
    if issues:
        for line in issues:
            print(f"FIX: {line}")
        sys.exit(f"bot @{bot.get('username')} settings need adjustment; rerun after fixing")


def chownr(p, uid, g, dmode, fmode):
    os.chown(p, uid, g); os.chmod(p, dmode)
    for root, dirs, files in os.walk(p):
        for d in dirs:
            q = os.path.join(root, d); os.chown(q, uid, g); os.chmod(q, dmode)
        for f in files:
            q = os.path.join(root, f); os.chown(q, uid, g); os.chmod(q, fmode)


# company-level files, rendered once
shared = ROOT / "shared"
shared.mkdir(exist_ok=True)
os.chown(shared, 0, gid); os.chmod(shared, 0o2775)
handbook = ROOT / "handbook.md"
if not handbook.exists():
    handbook.write_text(render("templates/handbook.md", f"{PREFIX}-*"))
    os.chmod(handbook, 0o644)
    print(f"wrote {handbook}")

if TG_TOKEN:
    assert_bot_settings(TG_TOKEN)  # privacy must be off so the one bot reads every topic

for role in sys.argv[1:]:
    spec = company["agents"].get(role)
    if spec is None:
        sys.exit(f"{role}: not in company.yaml agents")
    soul_t = ROOT / "templates" / "souls" / f"{spec.get('soul', role)}.md"
    if not soul_t.exists():
        sys.exit(f"{role}: no soul template at {soul_t}")
    # Telegram is configured when we have the company bot token, the
    # supergroup, and this agent's topic. The agent talks to the mux (not
    # api.telegram.org) using its own name as the routing key.
    chatful = bool(TG_TOKEN and CHAT_ID and spec.get("topic_id"))
    if TG_TOKEN and CHAT_ID and not spec.get("topic_id"):
        sys.exit(f"{role}: no topic_id in company.yaml — create the topic first (seed.py)")

    agent = f"{PREFIX}-{role}"
    H = pathlib.Path("/home") / agent
    A, S = H / "agent", H / "subconscious"
    if (A / "config.json").exists():
        # config.json is the commit marker, but it's written before the
        # service is started, so a prior run may have failed partway. Only
        # skip when the agent is actually running; otherwise fall through and
        # re-provision (every write below is idempotent / overwriting).
        if subprocess.run(["systemctl", "is-active", "--quiet", f"{agent}.service"]).returncode == 0:
            print(f"{agent}: already provisioned and running, skip")
            continue
        print(f"{agent}: resuming partial provision (config.json present, service not active)")

    if not H.exists():
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", agent], check=True)
    subprocess.run(["usermod", "-aG", PREFIX, agent], check=True)
    u = pwd.getpwnam(agent)
    gid_self = grp.getgrnam(agent).gr_gid

    for d in (A, A / "memory", A / "triggers", A / "mail_inbox"):
        d.mkdir(parents=True, exist_ok=True)

    api_base = (f"{PROXY_URL.rstrip('/')}/{agent}/{PROVIDER}/v1" if PROXY_URL and PROVIDER
                else company.get("api_base", "https://api.moonshot.ai/v1"))
    cfg = {"api_key": secrets["api_key"],
           "api_base": api_base,
           "model": company.get("model", "kimi-k2.6"),
           "context_tokens": company.get("context_tokens", 100000)}
    if chatful:
        cfg = {"telegram_token": agent,            # routing key for the mux
               "telegram_api_base": MUX_URL,
               "telegram_chat_id": str(CHAT_ID),
               "telegram_thread_id": str(spec["topic_id"]), **cfg}
    (A / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (A / "SOUL.md").write_text(render(f"templates/souls/{spec.get('soul', role)}.md", agent))
    (A / "MEMORY.md").write_text("")

    shutil.copytree(ROOT / "harness" / "opt" / "subconscious", S, dirs_exist_ok=True)
    # Route the subconscious through the proxy under its own tag (<agent>-sub)
    # so its token spend logs separately from the primary's and is filterable.
    sub_api_base = (f"{PROXY_URL.rstrip('/')}/{agent}-sub/{PROVIDER}/v1"
                    if PROXY_URL and PROVIDER else cfg["api_base"])
    (S / "config.json").write_text(json.dumps(
        {"api_key": cfg["api_key"], "api_base": sub_api_base,
         "model": cfg["model"], "context_tokens": cfg["context_tokens"],
         # The subconscious SOUL uses NUDGE + PRUNE; the harness copies these
         # from harness/opt/tools/ into <subconscious>/tools/ on boot.
         "opt": ["tools/nudge", "tools/stash_messages"]}, indent=2) + "\n")
    (S / "MEMORY.md").write_text("# Subconscious Memory\n\nNo findings yet.\n")

    # home traversable by the company group (mail drops), not listable;
    # agent dir group-readable (audits), configs owner-only, inbox group-writable
    os.chown(H, u.pw_uid, gid); os.chmod(H, 0o2710)
    chownr(A, u.pw_uid, gid, 0o2750, 0o640)
    chownr(S, u.pw_uid, gid_self, 0o750, 0o640)
    os.chmod(A / "config.json", 0o600)
    os.chmod(S / "config.json", 0o600)
    os.chmod(A / "mail_inbox", 0o2770)

    if spec.get("sudo"):
        s = pathlib.Path(f"/etc/sudoers.d/{agent}")
        s.write_text(f"{agent} ALL=(ALL) NOPASSWD:ALL\n")
        os.chmod(s, 0o440)

    pathlib.Path(f"/etc/systemd/system/{agent}.service").write_text(f"""[Unit]
Description={NAME} agent: {agent}
After=network.target

[Service]
Type=simple
User={agent}
Group={agent}
UMask=0027
WorkingDirectory={H}
ExecStart={ROOT}/venv/bin/python {ROOT}/harness/agent.py {A} {S}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", f"{agent}.service"], check=True)
    print(f"{agent}: hired (telegram={'via mux' if chatful else 'none — chat-less'} topic={spec.get('topic_id', '-')})")

print("done — agents check in via their Telegram topics; journalctl -u <agent> for logs")
