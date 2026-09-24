"""launchd install/uninstall. `brain install` replaces hand-written plists."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config

AGENTS = Path.home() / "Library" / "LaunchAgents"
TICK = "com.mlubich.brain"
DIGEST = "com.mlubich.brain-digest"
WATCH = "com.mlubich.brain-watchdog"
SYNC = "com.mlubich.brain-sync"
REPLY = "com.mlubich.brain-reply"
REPLY_TIMES = [(8, 0), (14, 0), (19, 0)]  # morning, afternoon, evening
ALL = (TICK, DIGEST, WATCH, SYNC, REPLY)

# launchd hands jobs a minimal PATH, so it must be stated explicitly or every
# CLI the channels shell out to silently vanishes.
PATH = ":".join([
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])

PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>{exe}</string><string>{command}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{path}</string>
    <key>WA_REPO</key><string>{wa_repo}</string>
  </dict>
  {schedule}
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{out}</string>
  <key>StandardErrorPath</key><string>{err}</string>
</dict></plist>
"""


def _exe() -> str:
    return shutil.which("brain") or str(Path(os.sys.executable).parent / "brain")


def _write(label: str, command: str, schedule: str) -> Path:
    AGENTS.mkdir(parents=True, exist_ok=True)
    path = AGENTS / f"{label}.plist"
    path.write_text(PLIST.format(
        label=label, exe=_exe(), command=command, path=PATH,
        wa_repo=config.WA_REPO, schedule=schedule,
        out=config.DIR / f"{command}.out", err=config.DIR / f"{command}.err",
    ))
    return path


def _launchctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True,
                          check=False, timeout=30)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def install(poll: int | None = None, hour: int | None = None) -> list[str]:
    """Write both plists and load them. Idempotent."""
    config.ensure_dirs()
    poll = poll or config.POLL_SECONDS
    hour = config.DIGEST_HOUR if hour is None else hour
    # The watchdog runs on its own timer, deliberately offset from the tick so a
    # single wedged process cannot take both down.
    plists = [
        _write(TICK, "tick", f"<key>StartInterval</key><integer>{poll}</integer>"),
        _write(DIGEST, "digest",
               "<key>StartCalendarInterval</key><dict>"
               f"<key>Hour</key><integer>{hour}</integer>"
               "<key>Minute</key><integer>0</integer></dict>"),
        _write(WATCH, "watchdog",
               f"<key>StartInterval</key><integer>{max(120, poll // 2)}</integer>"),
        _write(SYNC, "sync", f"<key>StartInterval</key><integer>{max(900, poll * 3)}</integer>"),
        _write(REPLY, "reply", "<key>StartCalendarInterval</key><array>" + "".join(
            f"<dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer></dict>"
            for h, m in REPLY_TIMES) + "</array>"),
    ]
    uid = os.getuid()
    results = []
    for path in plists:
        _launchctl("bootout", f"gui/{uid}/{path.stem}")  # ignore "not loaded"
        code, out = _launchctl("bootstrap", f"gui/{uid}", str(path))
        results.append(f"{path.stem}: {'loaded' if code == 0 else out or 'failed'}")
    return results


def uninstall() -> list[str]:
    uid = os.getuid()
    results = []
    for label in ALL:
        code, out = _launchctl("bootout", f"gui/{uid}/{label}")
        results.append(f"{label}: {'unloaded' if code == 0 else 'was not loaded'}")
        (AGENTS / f"{label}.plist").unlink(missing_ok=True)
    return results


def loaded() -> list[str]:
    _, out = _launchctl("list")
    return [line.split()[-1] for line in out.splitlines()
            if line.strip().endswith(ALL)]
