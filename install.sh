#!/bin/bash
# attosys install — run as root from the attosys checkout on a fresh Ubuntu host.
# Idempotent: safe to rerun. Clones the agent harness at a pinned commit, so a
# fresh install is reproducible and tracks a real upstream ref.
set -euo pipefail
cd "$(dirname "$0")"

# Agent harness, pinned. Carries telegram_api_base (so agents route Telegram
# through the local mux) and spawns each extra dir (the subconscious) as its
# own process. Bump deliberately to take harness updates.
ATTOBOT_REPO=https://github.com/tetratorus/attobot
ATTOBOT_SHA=afd79c1cb2321f47c0c8a7a1be4087638afc8424

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
