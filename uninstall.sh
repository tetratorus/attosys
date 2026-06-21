#!/bin/bash
# attosys uninstall — remove everything setup.sh + hire.py created on this host.
#
#   sudo ./uninstall.sh             # remove the runtime + durable state; keep repo
#   sudo ./uninstall.sh --keep-state  # keep /var/lib/attosys (topic-id state, so
#                                     # a reinstall reuses existing forum topics)
#   sudo ./uninstall.sh --purge     # also delete /opt/attosys (the repo itself)
#   sudo ./uninstall.sh -y          # skip the confirmation prompt
#
# Idempotent: safe to rerun. Reads company.yaml if present to learn the org
# slug + agents; otherwise infers them from installed systemd units so a
# half-deleted install (or a missing company.yaml) can still be cleaned up.
#
# By default this is a true uninstall — a clean slate. Durable topic-id state
# at /var/lib/attosys/<org>.yaml is dropped, so the next setup.sh creates a
# fresh set of forum topics. Pass --keep-state if you're reinstalling on the
# same supergroup and want to reuse the existing topics (otherwise you'd get
# duplicates, since Telegram has no list-topics API).
#
# The one thing it cannot remove is the Telegram side (forum topics in your
# supergroup) — a bot can't bulk-delete topics. Delete them by hand in Telegram
# if you care, or just leave the supergroup.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./uninstall.sh"; exit 1; }

PURGE=0
KEEP_STATE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --purge|-p) PURGE=1 ;;
    --keep-state) KEEP_STATE=1 ;;
    -y|--yes)   ASSUME_YES=1 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg (try --help)"; exit 1 ;;
  esac
done

# --- discover orgs + agents --------------------------------------------------
# Each org maps to: <org>-mux.service, optional <org>-proxy.service, and one
# <org>-<agent>.service per agent. We prefer company.yaml; fall back to the
# unit files so a missing/broken company.yaml doesn't block cleanup.
declare -A ORG_AGENTS   # org -> "agent1 agent2 ..."
ORGS=()
if [ -f "$ROOT/company.yaml" ] && command -v python3 >/dev/null 2>&1; then
  ORG=$(python3 -c "import yaml;print(yaml.safe_load(open('$ROOT/company.yaml'))['org'])" 2>/dev/null || true)
  if [ -n "$ORG" ]; then
    ORGS+=("$ORG")
    ORG_AGENTS["$ORG"]=$(python3 -c "
import yaml
c=yaml.safe_load(open('$ROOT/company.yaml'))
print(' '.join((c.get('agents') or {}).keys()))" 2>/dev/null || true)
  fi
fi
# fall back / supplement: scan installed <org>-mux.service units
for f in /etc/systemd/system/*-mux.service; do
  [ -f "$f" ] || continue
  o=$(basename "$f" -mux.service)
  found=0
  for x in "${ORGS[@]:-}"; do [ "$x" = "$o" ] && found=1; done
  [ "$found" -eq 0 ] && { ORGS+=("$o"); ORG_AGENTS["$o"]=""; }
done

if [ ${#ORGS[@]} -eq 0 ]; then
  echo "no attosys install found (no company.yaml, no <org>-mux.service)."
  # Still drop durable state by default — it may be the only leftover from a
  # half-uninstalled attosys, and a "clean slate" must include it.
  if [ "$KEEP_STATE" -ne 1 ] && [ -d /var/lib/attosys ]; then
    rm -rf /var/lib/attosys
    echo "  removed durable state: /var/lib/attosys/"
  fi
  if [ "$PURGE" -eq 1 ]; then
    echo "--purge: removing $ROOT anyway"
    rm -rf "$ROOT"
  fi
  exit 0
fi

# --- confirm ----------------------------------------------------------------
if [ "$ASSUME_YES" -ne 1 ]; then
  echo "About to remove for org(s): ${ORGS[*]}"
  echo "  - systemd units: <org>-mux, <org>-proxy, <org>-<agent>"
  echo "  - agent unix users + homes: /home/<org>-<agent>"
  echo "  - shared group: <org>"
  echo "  - sudoers files: /etc/sudoers.d/<org>-<agent>"
  echo "  - generated state: venv/ harness/ proxy/ shared/ handbook.md company.yaml secrets.yaml"
  [ "$PURGE" -eq 1 ] && echo "  - the repo itself: $ROOT"
  if [ "$KEEP_STATE" -eq 1 ]; then
    echo "  - keeping durable topic-id state at /var/lib/attosys/ (reinstall reuses topics)"
  else
    echo "  - durable topic-id state: /var/lib/attosys/<org>.yaml (next install makes NEW topics)"
  fi
  read -rp "Proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy] ]] || { echo "aborted"; exit 1; }
fi

set +e   # don't abort on a missing user / failed userdel; clean up best-effort
for ORG in "${ORGS[@]}"; do
  echo "=== removing org: $ORG ==="

  # discover agents for this org if we don't have them from company.yaml
  AGENTS="${ORG_AGENTS[$ORG]}"
  if [ -z "$AGENTS" ]; then
    AGENTS=""
    for f in /etc/systemd/system/${ORG}-*.service; do
      [ -f "$f" ] || continue
      n=$(basename "$f" .service)
      n=${n#${ORG}-}
      [ "$n" != "mux" ] && [ "$n" != "proxy" ] && AGENTS="$AGENTS $n"
    done
  fi

  # stop + disable + remove units (mux + proxy + one per agent)
  for u in mux proxy $AGENTS; do
    unit="${ORG}-${u}.service"
    if [ -f "/etc/systemd/system/${unit}" ]; then
      systemctl disable --now "$unit" 2>/dev/null || true
      rm -f "/etc/systemd/system/${unit}"
      echo "  removed unit: $unit"
    fi
  done

  # remove sudoers files for agents that had sudo
  for a in $AGENTS; do
    rm -f "/etc/sudoers.d/${ORG}-${a}" 2>/dev/null && echo "  removed sudoers: ${ORG}-${a}"
  done

  # delete agent unix users + homes (subconscious dir lives under the home)
  for a in $AGENTS; do
    user="${ORG}-${a}"
    if id "$user" >/dev/null 2>&1; then
      userdel -r "$user" 2>/dev/null || true
      rm -rf "/home/$user" 2>/dev/null || true
      id "$user" >/dev/null 2>&1 && userdel "$user" 2>/dev/null || true
      echo "  removed user: $user"
    fi
  done

  # remove the shared group
  if getent group "$ORG" >/dev/null 2>&1; then
    groupdel "$ORG" 2>/dev/null || true
    echo "  removed group: $ORG"
  fi
done
systemctl daemon-reload
set -e

# --- remove generated state inside the repo (keep the scripts) --------------
echo "=== removing generated state ==="
rm -rf "$ROOT/venv" "$ROOT/harness" "$ROOT/proxy" "$ROOT/shared" \
       "$ROOT/handbook.md" "$ROOT/company.yaml" "$ROOT/secrets.yaml" \
       "$ROOT/mux/__pycache__" "$ROOT/__pycache__"
echo "  cleaned: venv/ harness/ proxy/ shared/ handbook.md company.yaml secrets.yaml"

# --- durable topic-id state (dropped by default; --keep-state preserves) -----
if [ "$KEEP_STATE" -eq 1 ]; then
  for ORG in "${ORGS[@]}"; do
    [ -f "/var/lib/attosys/${ORG}.yaml" ] && echo "  kept durable state: /var/lib/attosys/${ORG}.yaml (reinstall reuses topics)"
  done
else
  # Nuke the whole directory, not just files for discovered orgs — catches
  # orphan state from orgs whose units/config are already gone.
  if [ -d /var/lib/attosys ]; then
    rm -rf /var/lib/attosys
    echo "  removed durable state: /var/lib/attosys/"
  fi
fi

if [ "$PURGE" -eq 1 ]; then
  echo "=== purging repo ==="
  cd /
  rm -rf "$ROOT"
  echo "  removed: $ROOT"
  echo "done — attosys fully removed."
else
  echo "done — runtime removed. The repo scripts remain at $ROOT;"
  echo "  re-run sudo ./setup.sh to reinstall, or sudo ./uninstall.sh --purge to delete the repo too."
fi
