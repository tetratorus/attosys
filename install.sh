#!/bin/bash
# attosys install — run as root from the attosys checkout on a fresh Ubuntu host.
# Idempotent: safe to rerun. Clones the agent harness at a pinned commit, so a
# fresh install is reproducible and tracks a real upstream ref.
set -euo pipefail
cd "$(dirname "$0")"

# Agent harness, pinned. This commit includes telegram_api_base (so agents can
# be pointed at the local mux). Bump deliberately to take harness updates.
ATTOBOT_REPO=https://github.com/tetratorus/attobot
ATTOBOT_SHA=4d09c5830aded3c6752fc80ca2159329789d8691

apt-get update -qq
apt-get install -y -qq python3-venv python3-yaml python3-requests git

if [ ! -d harness/.git ]; then
  rm -rf harness
  git clone -q "$ATTOBOT_REPO" harness
fi
git -C harness fetch -q origin
git -C harness checkout -q "$ATTOBOT_SHA"

[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r harness/requirements.txt

echo "install ok. Next: sudo ./setup.sh"
