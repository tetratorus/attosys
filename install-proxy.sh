#!/bin/bash
# Clone + build the llm proxy (proxy/) at a pinned commit, plus Node.js and the
# build tools its native sqlite module needs. Idempotent. Called by setup.sh;
# can also be run standalone. Run as root.
set -euo pipefail
cd "$(dirname "$0")"

# llm proxy, pinned. Allows hyphens in agent tags so attosys unix names
# (acme-hr) route as /<agent>/<provider>/v1. Bump deliberately.
LLMPROXY_REPO=https://github.com/tetratorus/llmproxy
LLMPROXY_SHA=3f2143852711bef745cee9283c833db552a8beca

if [ ! -d proxy/.git ]; then
  rm -rf proxy
  git clone -q "$LLMPROXY_REPO" proxy
fi
git -C proxy fetch -q origin
git -C proxy checkout -q "$LLMPROXY_SHA"

if ! command -v node >/dev/null 2>&1; then
  echo "installing Node.js..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
apt-get install -y -qq build-essential python3 >/dev/null  # native better-sqlite3 build

cd proxy && npm install --no-audit --no-fund
echo "proxy installed (node $(node --version))"
