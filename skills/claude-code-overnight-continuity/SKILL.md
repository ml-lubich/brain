---
name: claude-code-overnight-continuity
description: Keep a long unattended Claude Code job running across the 5-hour usage-limit reset, process crashes, and machine load spikes. Use when a job must survive hours without a human watching.
metadata:
  type: reference
---

# Overnight continuity for unattended Claude Code work

Goal: a multi-hour job (a live batch, an ingest, a regression sweep) keeps making progress
while nobody is watching — across the 5-hour usage-limit window, a crashed child process,
and a wedged-but-alive process.

## Do not build this from scratch

Auto-resume after the usage limit already exists. Checked 2026-09-04:

| Tool | Shape | Notes |
|------|-------|-------|
| [terryso/claude-auto-resume](https://github.com/terryso/claude-auto-resume) | shell script | probes, parses the reset time, sleeps, `claude --resume` from the session's cwd |
| [cheapestinference/claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) | tmux wrapper | also handles 529/5xx overload and safeguard false positives, exponential backoff |
| [Anonymousmirror/claude-auto-continue](https://github.com/Anonymousmirror/claude-auto-continue) | PTY proxy | transparent wrapper, sends "continue" when the limit lifts |
| [cys1750/claude-auto-resume](https://github.com/cys1750/claude-auto-resume) | Windows | not for macOS |

Upstream feature requests: anthropics/claude-code#36320, #35744.

## Four layers, not one

Each layer covers a failure the others miss. Use all four.

1. **In-session cron (`CronCreate`)** — an hourly keepalive prompt. Jobs fire only while the
   REPL is idle and are session-only (in memory, gone when Claude exits, auto-expire after
   7 days). This is what carries you across a usage-limit window: the hour ticks keep
   retrying and the first one after the reset succeeds.
2. **Self-paced loop (`/loop` → `ScheduleWakeup`)** — the working tick that actually inspects
   state and fixes things. Pair it with a persistent `Monitor` so a stall, crash, or process
   death wakes you immediately instead of at the next heartbeat.
3. **OS-level supervisor for the work itself** — the long job must not depend on the agent
   being alive. Launch it detached (`nohup` + `start_new_session`) under its own retry
   supervisor that resumes from a progress file, and have it abort loudly on a disk floor.
4. **OS-level agent keepalive (`tools/agent-keepalive.sh` + launchd)** — the same pattern
   this repo's own `brain/service.py` uses (a launchd `StartInterval` waking `claude -p`),
   generalised to arbitrary jobs. If you are already running `brain`, reuse its plist
   conventions rather than inventing a third scheme.

   This is the layer the other three cannot provide: it relaunches the *agent itself* when
   the Claude session dies (usage limit, crash, closed terminal). A launchd agent runs it
   every 600 s; for each job spec it fires `claude -p <prompt>` in that job's cwd only when
   the job is unfinished **and** no claude process is already working there.

```bash
skills/claude-code-overnight-continuity/tools/install.sh          # load the launchd agent
tools/add-job.sh <name> <cwd> <done-marker> '<continuation prompt>'
tools/agent-keepalive.sh --selftest                              # assert-based checks
KEEPALIVE_DRY_RUN=1 tools/agent-keepalive.sh                     # show intent, launch nothing
tools/install.sh --uninstall                                     # stop it
```

The **job ends itself**: the keepalive stops firing once `<cwd>/<done-marker>` exists, so the
continuation prompt must instruct the agent to write that marker when the work is genuinely
finished and verified. Three guards keep it from running away — a busy check (`pgrep` for a
claude process in that cwd), a lifetime fire cap (`KEEPALIVE_MAX_FIRES`, default 60), and the
done marker.

## Non-obvious lessons (learned the hard way, 2026-09-04)

- **Liveness is not progress.** A GIL-spinning hang keeps the PID alive and stops logging,
  so a PID check reports "healthy" forever. Add a stall guard: kill the child after N minutes
  of zero log growth, then relaunch. Give startup a longer budget than the steady-state loop
  or you will kill a legitimately slow boot in a restart loop.
- **An append-only log poisons completion detection.** If every run appends to one log, a
  previous run's "finished" markers make the supervisor declare victory and abandon the
  current run. Scope completion to the current run (write a relaunch marker, read only from
  that offset forward).
- **When in doubt, relaunch.** A false relaunch is cheap when the job skips already-finished
  work; a false "done" costs the whole night.
- **Single-flight your restarts.** Two supervisors (the cron tick and the loop tick) can fire
  at the same instant and the second restart kills the first one's fresh child. `mkdir` is
  atomic — use it as the lock, and reclaim it if it is stale.
- **Make the long job resumable in batches.** A progress file plus per-batch commits turns a
  crash at hour six into a ten-minute loss instead of a restart from zero.
- **Recycle the process periodically.** Some native stores never release virtual address
  space (Kuzu's vmem allocator, for one). Exiting with a "more work remains" code and letting
  the supervisor relaunch resets it for free.
- **launchd cannot exec anything under ~/Desktop, ~/Documents or ~/Downloads.** macOS TCC
  denies it and every tick exits 126 "Operation not permitted" — and a dead agent looks
  exactly like an idle one, so it fails silently. Install the runner under
  `~/.claude-keepalive/bin/` and point the plist there. After installing, do not trust a
  manual run: check `launchctl print gui/$(id -u)/<label> | grep "last exit code"` and
  confirm a fresh line in the log. A keepalive you believe in but never verified is worse
  than none, because you stop watching the thing it was supposed to watch.
- **A busy check must ask about the process's cwd, not its argv.** `pgrep -fl claude | grep
  <path>` looks correct and silently never matches: a `claude -p` worker does not carry its
  working directory in its command line. On 2026-09-05 that made the guard a no-op — the agent
  fired ten times in a row and spawned duplicate workers on the same repo, burning half the
  lifetime fire budget before anyone noticed. Ask `lsof -a -p <pid> -d cwd -Fn` instead. Know
  the residual limit too: an *interactive* session started from a different directory still
  will not match, so this guard stops spawn-on-spawn pile-ups, not a human's own session.
- **A done-marker is a loaded gun.** Writing it early stops the keepalive silently — the log
  just says `done <job>` every interval and looks healthy. One was written prematurely and the
  agent skipped for 30 minutes before it was caught. Only write the marker when the work is
  genuinely finished, and if you inherit a run, check for the marker FIRST.
- **Watch your own load.** Fanning out subagents while the supervised job is running starves
  it. Load average 69 on a laptop means the batch you are babysitting is the thing you
  starved. Cap concurrency, or stagger the audit work.

## Checklist before walking away

- [ ] The work runs detached with its own retry supervisor and a progress file.
- [ ] A stall guard exists, with a longer startup budget.
- [ ] Completion detection is scoped to the current run.
- [ ] Restart is single-flight locked.
- [ ] Hourly cron keepalive is scheduled (covers the usage-limit window).
- [ ] A monitor is armed for stall / crash / death / completion.
- [ ] One command prints full status in under 25 lines.
- [ ] Disk headroom is checked by the supervisor, and it aborts rather than filling the disk.
- [ ] The launchd agent keepalive is installed with a job spec, and the continuation prompt
      tells the agent to write the done marker when the work is finished and verified.
