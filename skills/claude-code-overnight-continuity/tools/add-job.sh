#!/usr/bin/env bash
# Register a keepalive job:  ./add-job.sh <name> <cwd> <done-marker> '<prompt>'
# The job stops firing once <cwd>/<done-marker> exists, so the agent ends its own loop.
set -euo pipefail
[ "$#" -eq 4 ] || { echo "usage: $0 <name> <cwd> <done-marker-relative> '<prompt>'" >&2; exit 1; }
JOBS="${KEEPALIVE_JOBS_DIR:-$HOME/.claude-keepalive/jobs}"
mkdir -p "$JOBS"
/usr/bin/python3 - "$1" "$2" "$3" "$4" "$JOBS" <<'PY'
import json, pathlib, sys
name, cwd, done, prompt, jobs = sys.argv[1:6]
path = pathlib.Path(jobs) / f"{name}.json"
path.write_text(json.dumps({"cwd": cwd, "prompt": prompt, "done": done}, indent=2) + "\n")
print(f"wrote {path}")
PY
