#!/bin/bash
# atto-fleet-restart — controlled restart of atto-* services flagged by needrestart.
#
# needrestart is configured (/etc/needrestart/conf.d/50-atto.conf) to NEVER
# auto-restart atto-* units, while still reporting them. This script runs
# weekly via atto-fleet-restart.timer and restarts ONLY flagged units, in
# dependency order — bounding stale-lib exposure to <= 7 days without
# unattended mid-turn agent deaths.
#
# Restart order rationale:
#   1. atto-proxy — agents' LLM calls route through it; a brief drop is
#      absorbed by llm_w_retry. Get infra stable first.
#   2. atto-mux   — Telegram outage capped at 30s by TimeoutStopSec=30;
#      Telegram queues server-side, no message loss.
#   3. agents     — one at a time, 15s apart; fleet never fully down,
#      no LLM API stampede.
#
# Runs as root via systemd oneshot. Always exits 0 — the timer must not fail.
# Design doc: /opt/attosys/shared/fleet-restart/README.md

set -u

LOG=/var/log/atto-fleet-restart.log
ORDER="atto-proxy atto-mux atto-hr atto-sysadmin atto-labs atto-trainer"

log() { echo "[$(date -u +%Y%m%dT%H%M%S)] $*" >> "$LOG"; }

log "=== fleet-restart check started ==="

# Services needrestart detects as running stale libraries.
# Batch mode prints "NEEDRESTART-SVC: <unit>" lines.
flagged=$(needrestart -b 2>/dev/null | sed -n 's/^NEEDRESTART-SVC: //p' | sed 's/\.service$//' | sort -u)

# Narrow to atto units only — everything else is needrestart's own business.
flagged_atto=""
for unit in $flagged; do
    case "$unit" in
        atto-*) flagged_atto="$flagged_atto $unit" ;;
    esac
done
flagged_atto=$(echo $flagged_atto)  # trim

if [ -z "$flagged_atto" ]; then
    log "no atto services flagged by needrestart; nothing to do"
    exit 0
fi

log "flagged atto units: $(echo $flagged_atto | tr '\n' ' ')"

restarted=0
for unit in $ORDER; do
    if echo "$flagged_atto" | tr ' ' '\n' | grep -qx "$unit"; then
        log "restarting $unit ..."
        systemctl restart "$unit"
        rc=$?
        sleep 5
        state=$(systemctl is-active "$unit" 2>&1)
        if [ $rc -eq 0 ]; then
            log "$unit restarted; is-active=$state"
            restarted=$((restarted + 1))
        else
            log "ERROR: systemctl restart $unit exited $rc; is-active=$state"
        fi
        # settle gap: mux needs longer for Telegram reconnect
        if [ "$unit" = "atto-mux" ]; then sleep 30; else sleep 15; fi
    fi
done

# Flagged atto unit missing from ORDER (e.g. an agent hired after this
# script was last updated)? Log a warning — a human must add it.
for unit in $flagged_atto; do
    case " $ORDER " in
        *" $unit "*) ;;  # handled above
        *) log "WARNING: $unit flagged but not in ORDER — restart manually and update ORDER in $0" ;;
    esac
done

log "=== done: $restarted service(s) restarted ==="
exit 0
