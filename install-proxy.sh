#!/bin/bash
# Clone + build the llm proxy (proxy/) from latest upstream, plus Node.js and the
# build tools its native sqlite module needs. Idempotent. Called by setup.sh;
# can also be run standalone. Run as root.
set -euo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive   # silence debconf whiptail in non-tty shells

# llm proxy. Allows hyphens in agent tags so attosys unix names (acme-hr) route
# as /<agent>/<provider>/v1.
LLMPROXY_REPO=https://github.com/tetratorus/llmproxy

if [ ! -d proxy/.git ]; then
  rm -rf proxy
  git clone -q "$LLMPROXY_REPO" proxy
fi
git -C proxy fetch -q origin
LLMPROXY_BRANCH="$(git -C proxy remote show origin | awk '/HEAD branch/ {print $NF}')"
git -C proxy checkout -q -B "$LLMPROXY_BRANCH" "origin/$LLMPROXY_BRANCH"
git -C proxy pull -q --ff-only origin "$LLMPROXY_BRANCH"

if ! command -v node >/dev/null 2>&1; then
  echo "installing Node.js..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
# On some distros (e.g. Ubuntu's nodejs apt package) npm is a separate
# package and isn't pulled in by node. npm is required for `npm install` below.
if ! command -v npm >/dev/null 2>&1; then
  echo "installing npm..."
  apt-get install -y -qq npm
fi
apt-get install -y -qq build-essential python3 >/dev/null  # native better-sqlite3 build

cd proxy && npm install --no-audit --no-fund
echo "proxy installed (node $(node --version))"
