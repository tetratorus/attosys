#!/bin/bash
# attosys install — run as root from the attosys checkout on a fresh Ubuntu host.
# Idempotent: safe to rerun. Clones the agent harness and pulls the latest
# upstream default branch.
set -euo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive   # silence debconf whiptail in non-tty shells

# Agent harness. Carries telegram_api_base (so agents route Telegram through
# the local mux) and spawns each extra dir (the subconscious) as its own
# process.
ATTOBOT_REPO=https://github.com/tetratorus/attobot

apt-get update -qq
apt-get install -y -qq python3-venv python3-yaml python3-requests git

if [ ! -d harness/.git ]; then
  rm -rf harness
  git clone -q "$ATTOBOT_REPO" harness
fi
git -C harness fetch -q origin
ATTOBOT_BRANCH="$(git -C harness remote show origin | awk '/HEAD branch/ {print $NF}')"
git -C harness checkout -q -B "$ATTOBOT_BRANCH" "origin/$ATTOBOT_BRANCH"
git -C harness pull -q --ff-only origin "$ATTOBOT_BRANCH"

[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r harness/requirements.txt

echo "install ok. Next: sudo ./setup.sh"
