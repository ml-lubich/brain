"""Keeps the agent alive without ever destroying anything.

Two rules shape every line here:

1. **Surgical kills only.** This Mac runs many interactive `claude` sessions.
   The watchdog will only ever signal a PID that a brain tick itself recorded in
   `tick.pid`, and only after confirming that PID is still the same `claude -p`
   child. It never pattern-matches its way to a kill. Killing the user's live
   session because it "looked stale" is the failure mode that matters most.

2. **A circuit breaker, not a restart loop.** A permanently broken job restarted
   every 5 minutes burns API quota and achieves nothing. After a small number of
   attempts the watchdog stops acting and only reports.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import config, notify, service

# tick runs every POLL_SECONDS; treat it as stalled only after several missed
# cycles so a slow Claude call or a sleeping Mac never trips it.
STALE_MULTIPLIER = 3
# claude -p is given 900s inside agent.invoke; allow generous headroom past that
# before considering the child hung.
CHILD_HUNG_SECONDS = 1200
MAX_ATTEMPTS = 3          # per window, then alert-only
BREAKER_WINDOW = 6 * 3600  # seconds

PIDFILE = "tick.pid"
BREAKER = "breaker.json"
LASTRUN = "watchdog.last"
LASTOK = "tick.ok"


def _path(name: str) -> Path:
    return config.DIR / name


def beat() -> None:
    """Called by every tick, including the ones that decide to do nothing.
    A tick that correctly skips is still a healthy tick."""
    config.ensure_dirs()
    config.HEARTBEAT.write_text(str(int(time.time())))


def mark_ok() -> None:
    """A tick that actually reached Claude and got an answer.

    Deliberately separate from the heartbeat. `beat()` fires before Claude is
    invoked, which is what stops a rate-limited API from tripping the watchdog
    into a restart loop — but it also means a tick whose Claude call fails
    forever still looks alive. This is the signal that tells them apart.
    """
    config.ensure_dirs()
    _path(LASTOK).write_text(str(int(time.time())))


def productive_age() -> float | None:
    """Seconds since Claude last answered, or None if it never has."""
    path = _path(LASTOK)
    if not path.exists():
        return None
    try:
        return time.time() - float(path.read_text().strip())
    except ValueError:
        return None


def watchdog_stale() -> bool:
    """Has the watchdog itself stopped running?

    Nothing watches the watchdog, so the tick watches it instead — each job
    checks the other, which costs nothing and needs no third daemon.
    """
    age = _last_run_age()
    return age is not None and age > watchdog_interval() * 4


def heartbeat_age() -> float | None:
    """Seconds since the last tick, or None if it has never run."""
    if not config.HEARTBEAT.exists():
        return None
    try:
        return time.time() - float(config.HEARTBEAT.read_text().strip())
    except ValueError:
        return None


def stale_after() -> int:
    return config.POLL_SECONDS * STALE_MULTIPLIER


def watchdog_interval() -> int:
    """Mirrors what service.install() writes for the watchdog job."""
    return max(120, config.POLL_SECONDS // 2)


def _last_run_age() -> float | None:
    path = _path(LASTRUN)
    if not path.exists():
        return None
    try:
        return time.time() - float(path.read_text().strip())
    except ValueError:
        return None


def mark_run() -> None:
    _path(LASTRUN).write_text(str(int(time.time())))


def slept() -> bool:
    """True when the Mac was asleep or off rather than the tick being stuck.

    launchd suspends StartInterval jobs during sleep, so after an overnight the
    heartbeat looks hours old — but so does the watchdog's own last run. A real
    stall looks different: the watchdog kept firing on schedule while the tick
    did not. Comparing the two is what tells them apart, with no pmset parsing.
    """
    gap = _last_run_age()
    if gap is None:
        return False          # first ever run: judge on the heartbeat alone
    return gap > watchdog_interval() * 2


def record_child(pid: int) -> None:
    _path(PIDFILE).write_text(json.dumps({"pid": pid, "started": int(time.time())}))


def clear_child() -> None:
    _path(PIDFILE).unlink(missing_ok=True)


def _proc_cmdline(pid: int) -> str:
    proc = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True, check=False, timeout=15)
    return proc.stdout.strip()


def hung_child() -> tuple[int, int] | None:
    """(pid, age) of a recorded claude child that has outlived its timeout.

    Every guard here exists to make certain we cannot hit an interactive session:
    the PID must be one we recorded, still alive, still a `claude` process, and
    still running with `-p` (headless). Any doubt returns None.
    """
    pidfile = _path(PIDFILE)
    if not pidfile.exists():
        return None
    try:
        data = json.loads(pidfile.read_text())
        pid, started = int(data["pid"]), int(data["started"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None

    age = int(time.time() - started)
    if age < CHILD_HUNG_SECONDS:
        return None

    cmd = _proc_cmdline(pid)
    if not cmd:
        clear_child()  # already gone
        return None
    if "claude" not in cmd or " -p" not in f" {cmd}":
        # PID was recycled onto something else. Never signal it.
        clear_child()
        return None
    return pid, age


def terminate_child(pid: int, grace: int = 20) -> str:
    """SIGTERM, wait, then SIGKILL. Only ever the recorded headless child."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_child()
        return "child already gone"
    except PermissionError:
        return f"cannot signal pid {pid} (permission)"

    for _ in range(grace):
        time.sleep(1)
        if not _proc_cmdline(pid):
            clear_child()
            return f"terminated pid {pid} gracefully"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    clear_child()
    return f"killed pid {pid} after {grace}s grace"


def _breaker() -> dict:
    path = _path(BREAKER)
    if not path.exists():
        return {"attempts": [], "opened": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"attempts": [], "opened": None}


def _save_breaker(state: dict) -> None:
    _path(BREAKER).write_text(json.dumps(state))


def breaker_state() -> tuple[bool, int]:
    """(open, recent_attempts). Open means: stop acting, only report."""
    state = _breaker()
    now = time.time()
    recent = [t for t in state.get("attempts", []) if now - t < BREAKER_WINDOW]
    if recent != state.get("attempts"):
        state["attempts"] = recent
        _save_breaker(state)
    return len(recent) >= MAX_ATTEMPTS, len(recent)


def record_attempt() -> None:
    state = _breaker()
    state.setdefault("attempts", []).append(time.time())
    _save_breaker(state)


def reset_breaker() -> None:
    _save_breaker({"attempts": [], "opened": None})


def check() -> dict:
    """Full health read. Never changes anything."""
    age = heartbeat_age()
    loaded = service.loaded()
    is_open, attempts = breaker_state()
    hung = hung_child()
    return {
        "heartbeat_age": age,
        "stale": age is None or age > stale_after(),
        "stale_after": stale_after(),
        "jobs_loaded": loaded,
        "jobs_missing": [l for l in (service.TICK, service.DIGEST) if l not in loaded],
        "hung_child": hung,
        "breaker_open": is_open,
        "attempts": attempts,
        "productive_age": productive_age(),
        "watchdog_age": _last_run_age(),
    }


def run() -> list[str]:
    """One watchdog pass. Returns what it did, in order.

    Recovery ladder, non-destructive at every rung:
      1. hung headless child      -> SIGTERM, then SIGKILL after grace
      2. launchd job not loaded   -> reload it
      3. loaded but heartbeat stale -> kick one tick by hand
      4. breaker open             -> do nothing, alert only
    """
    config.ensure_dirs()
    state = check()
    actions: list[str] = []
    was_asleep = slept()
    mark_run()

    if was_asleep and state["stale"] and not state["jobs_missing"]:
        # The machine was suspended. Everything is stale, nothing is broken.
        # Let the next scheduled tick catch up on its own.
        actions.append(
            f"resumed after a {_fmt(_last_run_age() or 0)} gap (sleep/shutdown) — "
            "not treating a stale heartbeat as a stall"
        )
        _log(actions)
        return actions

    if state["hung_child"]:
        pid, age = state["hung_child"]
        actions.append(f"hung claude child pid {pid} ({age}s) -> {terminate_child(pid)}")

    if state["jobs_missing"]:
        if state["breaker_open"]:
            actions.append(f"jobs missing {state['jobs_missing']} but breaker OPEN — not reloading")
        else:
            record_attempt()
            actions += [f"reload: {line}" for line in service.install()]

    elif state["stale"]:
        if state["breaker_open"]:
            actions.append(
                f"heartbeat stale ({_fmt(state['heartbeat_age'])}) but breaker OPEN "
                f"after {state['attempts']} attempts — alerting only"
            )
        else:
            record_attempt()
            actions.append(f"heartbeat stale ({_fmt(state['heartbeat_age'])}) -> kicking a tick")
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{service.TICK}"],
                           capture_output=True, check=False, timeout=30)
    else:
        actions.append(f"healthy (last tick {_fmt(state['heartbeat_age'])} ago)")

    acted = any(not a.startswith("healthy") for a in actions)
    if acted:
        _log(actions)
        notify.send("brain watchdog", actions[0][:180])
    return actions


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _log(actions: list[str]) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with config.LOG.open("a") as fh:
        for action in actions:
            fh.write(f"{stamp} watchdog: {action}\n")
