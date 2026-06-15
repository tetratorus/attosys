#!/bin/bash
# attosys setup — one command, bare Ubuntu to a running agent-only company.
#   sudo ./setup.sh
# Idempotent: re-run any time. Skips steps already done.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./setup.sh"; exit 1; }

# --- 1. deps + harness venv -------------------------------------------------
./install.sh

# --- 2. guided configuration (only on first run) ----------------------------
ask() {  # ask VAR "prompt" "regex" ; loops until the answer matches regex
  local var="$1" prompt="$2" re="${3:-.}" val=""
  while :; do
    read -rp "  $prompt: " val
    [[ "$val" =~ $re ]] && break || echo "  ! invalid, try again"
  done
  printf -v "$var" '%s' "$val"
}

if [ ! -f company.yaml ]; then
  echo
  echo "=== Configure your company ==="
  ask ORG       "org slug (lowercase, <=12 chars; names users/group/units/topics)" '^[a-z0-9]{1,12}$'
  ask NAME      "display name (free text, e.g. Acme Corp)"                          '.+'
  ask CEO_NAME  "your name (the CEO)"                                               '.+'
  echo
  echo "  Your Telegram numeric id — message @userinfobot, it replies with it."
  ask CEO_TGID  "your telegram user id (number)"                                    '^[0-9]+$'
  echo
  echo "  LLM provider — any OpenAI-compatible endpoint."
  ask MODEL     "model (e.g. kimi-k2.6)"                                            '.+'
  ask API_BASE  "api base url (e.g. https://api.moonshot.ai/v1)"                    '^https?://.+'
  ask API_KEY   "api key"                                                          '.+'
  echo
  echo "  Telegram bot + group (the one manual Telegram step — a bot cannot"
  echo "  create a group or list its groups):"
  echo "    1. @BotFather -> /newbot -> copy the token; then /setprivacy -> Disable"
  echo "    2. Create a supergroup, enable Topics in its settings"
  echo "    3. Add the bot to the group and promote it to admin (Manage Topics)"
  echo "  attosys does the rest (finds the group, makes the topics) automatically."
  ask BOT_TOKEN "bot token (digits:letters)"                                        '^[0-9]+:[A-Za-z0-9_-]+$'

  MUX_PORT=8811
  ATTOSYS_ROOT="$ROOT" ORG="$ORG" NAME="$NAME" CEO_NAME="$CEO_NAME" CEO_TGID="$CEO_TGID" \
  MODEL="$MODEL" API_BASE="$API_BASE" API_KEY="$API_KEY" BOT_TOKEN="$BOT_TOKEN" MUX_PORT="$MUX_PORT" \
  python3 - <<'PY'
import os, yaml
org = os.environ["ORG"]
company = {
    "org": org, "name": os.environ["NAME"],
    "ceo": {"name": os.environ["CEO_NAME"], "telegram_user_id": int(os.environ["CEO_TGID"])},
    "telegram_chat_id": "",
    "mux_url": f"http://127.0.0.1:{os.environ['MUX_PORT']}",
    "model": os.environ["MODEL"], "api_base": os.environ["API_BASE"],
    "agents": {
        "hr":       {"sudo": True, "description": "Head of HR and Chief of Staff."},
        "sysadmin": {"description": "Systems Administrator. Owns the substrate."},
        "labs":     {"description": "Exploration and builder agent."},
        "trainer":  {"description": "Company trainer."},
    },
}
open("company.yaml", "w").write(yaml.safe_dump(company, sort_keys=False, allow_unicode=True))
open("secrets.yaml", "w").write(yaml.safe_dump(
    {"api_key": os.environ["API_KEY"], "telegram_bot_token": os.environ["BOT_TOKEN"]},
    sort_keys=False))
PY
  chmod 600 secrets.yaml
  echo "wrote company.yaml + secrets.yaml"
else
  echo "company.yaml exists — using it (delete it to reconfigure)"
fi

ORG=$(python3 -c "import yaml;print(yaml.safe_load(open('company.yaml'))['org'])")
MUX_PORT=$(python3 -c "import yaml,urllib.parse as u;print(u.urlparse(yaml.safe_load(open('company.yaml'))['mux_url']).port or 8811)")
PROXY_PORT=$(python3 -c "import yaml,urllib.parse as u;c=yaml.safe_load(open('company.yaml'));p=c.get('proxy_url');print(u.urlparse(p).port if p else '')")

# --- 3. seed Telegram: discover the group, create one topic per agent --------
# (runs before the mux so they don't both consume getUpdates)
./seed.py

# --- 4. install + start the mux ---------------------------------------------
cat > "/etc/systemd/system/${ORG}-mux.service" <<UNIT
[Unit]
Description=${ORG} telegram mux (one bot -> all agents)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${ROOT}/mux/mux.py
Environment=ATTOSYS_ROOT=${ROOT}
Environment=MUX_PORT=${MUX_PORT}
WorkingDirectory=${ROOT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "${ORG}-mux.service"
echo "mux running: ${ORG}-mux on 127.0.0.1:${MUX_PORT}"

# --- 5. install + start the llm proxy (if configured) -----------------------
if [ -n "$PROXY_PORT" ]; then
  ./install-proxy.sh
  NODE=$(command -v node)
  cat > "/etc/systemd/system/${ORG}-proxy.service" <<UNIT
[Unit]
Description=${ORG} llm proxy (per-agent request logging)
After=network.target

[Service]
Type=simple
ExecStart=${NODE} ${ROOT}/proxy/server.js
Environment=PORT=${PROXY_PORT}
Environment=LLMPROXY_DB=${ROOT}/proxy/requests.db
WorkingDirectory=${ROOT}/proxy
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "${ORG}-proxy.service"
  echo "proxy running: ${ORG}-proxy on 127.0.0.1:${PROXY_PORT}"
fi

# --- 6. hire the genesis fleet ----------------------------------------------
./hire.py hr sysadmin labs trainer

echo
echo "done — agents check in on their Telegram topics. Logs:"
echo "  journalctl -u ${ORG}-hr -f       (an agent)"
echo "  journalctl -u ${ORG}-mux -f      (the mux)"
