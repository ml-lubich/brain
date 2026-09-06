#!/usr/bin/env bash
# agent-keepalive — relaunch a headless Claude Code job when it is not running.
#
# Covers the failure the in-session mechanisms cannot: the Claude session itself dying
# (5-hour usage limit, crash, terminal closed). A launchd agent runs this every few
# minutes; it re-fires `claude -p` for a job only when that job is unfinished AND no
# claude process is already working in its directory.
#
# Job spec: a directory of JSON files, one per job:
#   {"cwd": "/path/to/repo", "prompt": "what to continue doing", "done": "relative/marker"}
# The job is finished when `cwd/done` exists — the agent writes that marker itself when
# the work is complete, which is what stops the keepalive.
#
# Env:
#   KEEPALIVE_JOBS_DIR   default ~/.claude-keepalive/jobs
#   KEEPALIVE_LOG_DIR    default ~/.claude-keepalive/logs
#   KEEPALIVE_MAX_FIRES  default 60   — per-job lifetime cap, a runaway backstop
#   KEEPALIVE_CLAUDE     default `claude`
#   KEEPALIVE_DRY_RUN    1 = report what it would do, launch nothing
#
# Self-check: ./agent-keepalive.sh --selftest
set -uo pipefail

JOBS_DIR="${KEEPALIVE_JOBS_DIR:-$HOME/.claude-keepalive/jobs}"
LOG_DIR="${KEEPALIVE_LOG_DIR:-$HOME/.claude-keepalive/logs}"
MAX_FIRES="${KEEPALIVE_MAX_FIRES:-60}"
CLAUDE_BIN="${KEEPALIVE_CLAUDE:-claude}"

_log() { printf '%s %s\n' "$(date +%FT%T)" "$*" >> "$LOG_DIR/keepalive.log"; }

# A job is done when its marker file exists. Marker path is relative to the job's cwd.
job_done() {
  local cwd="$1" marker="$2"
  [ -n "$marker" ] && [ -e "$cwd/$marker" ]
}

# Already working? Any claude process whose command line mentions this cwd.
job_running() {
  # Is a claude process already WORKING IN this directory?
  #
  # This used to grep the process command line for the path. That silently never matched: a
  # `claude -p` worker does not carry its cwd in argv, so on 2026-09-05 the agent fired ten
  # times in a row (15:36-17:26) and spawned duplicate workers on the same repo, burning half
  # the lifetime fire budget. A process's real cwd is the thing to ask about, and lsof answers
  # it. Note an INTERACTIVE session started from elsewhere still will not match — by design,
  # this guard exists to stop spawn-on-spawn pile-ups, which is the runaway risk.
  local cwd="$1" pid pcwd
  for pid in $(pgrep -x claude 2>/dev/null); do
    pcwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [ "$pcwd" = "$cwd" ] && return 0
  done
  return 1
}

# Lifetime cap so a broken job cannot spawn forever.
fires_exhausted() {
  local count_file="$1" max="$2"
  local n
  n=$(cat "$count_file" 2>/dev/null || echo 0)
  [ "$n" -ge "$max" ]
}

bump_fires() {
  local count_file="$1"
  local n
  n=$(cat "$count_file" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$count_file"
}

_fire_job() {
  local name="$1" cwd="$2" prompt="$3"
  local out="$LOG_DIR/$name.out"
  _log "fire $name in $cwd"
  ( cd "$cwd" && nohup "$CLAUDE_BIN" -p "$prompt" >> "$out" 2>&1 & ) 2>/dev/null
}

_run_job() {
  local spec="$1"
  local name cwd prompt marker count_file
  name=$(basename "$spec" .json)
  cwd=$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cwd",""))' "$spec")
  prompt=$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("prompt",""))' "$spec")
  marker=$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("done",""))' "$spec")
  count_file="$LOG_DIR/$name.fires"
  [ -d "$cwd" ] || { _log "skip $name: cwd missing ($cwd)"; return 0; }
  job_done "$cwd" "$marker" && { _log "done $name (marker $marker)"; return 0; }
  job_running "$cwd" && { _log "busy $name"; return 0; }
  fires_exhausted "$count_file" "$MAX_FIRES" && { _log "capped $name at $MAX_FIRES fires"; return 0; }
  [ "${KEEPALIVE_DRY_RUN:-0}" = "1" ] && { _log "dry-run would fire $name"; return 0; }
  bump_fires "$count_file"
  _fire_job "$name" "$cwd" "$prompt"
}

_selftest() {
  local tmp; tmp=$(mktemp -d)
  LOG_DIR="$tmp"; mkdir -p "$tmp/cwd"
  job_done "$tmp/cwd" "marker" && { echo "FAIL: absent marker reported done"; return 1; }
  : > "$tmp/cwd/marker"
  job_done "$tmp/cwd" "marker" || { echo "FAIL: present marker not reported done"; return 1; }
  job_done "$tmp/cwd" "" && { echo "FAIL: empty marker treated as done"; return 1; }
  echo 5 > "$tmp/c"
  fires_exhausted "$tmp/c" 5 || { echo "FAIL: cap not detected at limit"; return 1; }
  fires_exhausted "$tmp/c" 6 && { echo "FAIL: cap tripped below limit"; return 1; }
  fires_exhausted "$tmp/absent" 1 && { echo "FAIL: missing count treated as exhausted"; return 1; }
  bump_fires "$tmp/c"; [ "$(cat "$tmp/c")" = "6" ] || { echo "FAIL: bump did not increment"; return 1; }
  job_running "$tmp/definitely-not-a-real-cwd-$$" && { echo "FAIL: phantom process reported"; return 1; }
  rm -rf "$tmp"
  echo "selftest ok"
}

main() {
  mkdir -p "$JOBS_DIR" "$LOG_DIR"
  [ "${1:-}" = "--selftest" ] && { _selftest; return $?; }
  local found=0
  for spec in "$JOBS_DIR"/*.json; do
    [ -e "$spec" ] || continue
    found=1
    _run_job "$spec"
  done
  [ "$found" = "0" ] && _log "no job specs in $JOBS_DIR"
  return 0
}

main "$@"
