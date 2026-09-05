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

mkdir -p "$STATE/jobs" "$LOGDIR" "$STATE/bin" "$HOME/Library/LaunchAgents"

# launchd cannot exec a script under ~/Desktop, ~/Documents or ~/Downloads: macOS TCC
# denies it and every tick dies with exit 126 "Operation not permitted" — silently, since
# a dead agent looks identical to an idle one. Copy the runner somewhere unprotected and
# point launchd there. (Cost us 9 hours of a dead keepalive on 2026-09-04.)
RUNNER="$STATE/bin/agent-keepalive.sh"
cp "$HERE/agent-keepalive.sh" "$RUNNER"
chmod +x "$RUNNER"

sed -e "s|__SCRIPT__|$RUNNER|g" \
    -e "s|__LOGDIR__|$LOGDIR|g" \
    -e "s|__PATH__|$PATH|g" \
    "$HERE/$LABEL.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL — every 600s, runner $RUNNER, jobs in $STATE/jobs, logs in $LOGDIR"
echo "verify it actually runs:  launchctl print gui/$(id -u)/$LABEL | grep \"last exit code\""
echo "add a job:  $HERE/add-job.sh <name> <cwd> <done-marker-relative-path> '<prompt>'"
