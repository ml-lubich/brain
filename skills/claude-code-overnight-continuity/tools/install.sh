#!/usr/bin/env bash
# Install the agent-keepalive launchd agent (macOS, per-user, no sudo).
# Idempotent: re-running re-renders the plist and reloads the agent.
#
#   ./install.sh              install / reload
#   ./install.sh --uninstall  remove the agent (job specs and logs are kept)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LABEL=com.mlubich.agent-keepalive
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.claude-keepalive"
LOGDIR="$STATE/logs"

[ "$(uname -s)" = "Darwin" ] || { echo "macOS only (launchd)" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL (job specs kept in $STATE/jobs)"
  exit 0
fi

mkdir -p "$STATE/jobs" "$LOGDIR" "$HOME/Library/LaunchAgents"
sed -e "s|__SCRIPT__|$HERE/agent-keepalive.sh|g" \
    -e "s|__LOGDIR__|$LOGDIR|g" \
    -e "s|__PATH__|$PATH|g" \
    "$HERE/$LABEL.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL — every 600s, jobs in $STATE/jobs, logs in $LOGDIR"
echo "add a job:  $HERE/add-job.sh <name> <cwd> <done-marker-relative-path> '<prompt>'"
