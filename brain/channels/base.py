"""What a channel is.

Adding a channel = drop one module in this package that subclasses Channel.
Discovery is automatic; nothing else needs editing.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod


def run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command, return stdout+stderr. Never raises — a dead channel
    should degrade to a visible error string, not take down the whole tick."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return f"({cmd[0]} not installed)"
    except subprocess.TimeoutExpired:
        return f"({cmd[0]} timed out after {timeout}s)"


class Channel(ABC):
    #: short id used in the queue and CLI output
    name: str = ""
    #: human label for the snapshot heading
    label: str = ""
    #: binary that must exist for this channel to work
    binary: str = ""
    #: True when the agent may propose replies that `brain approve` can send.
    #: False means the agent produces something the user approves elsewhere
    #: (email drafts live in Mail.app, so mail is sendable=False here).
    sendable: bool = True

    @abstractmethod
    def snapshot(self) -> str:
        """Recent activity, as plain text the agent will read."""

    def send(self, to: str, message: str) -> str:
        """Deliver an approved message. Only ever called by `brain approve`."""
        raise NotImplementedError(f"{self.name} cannot send")

    def available(self) -> tuple[bool, str]:
        """(ok, reason) — used by `brain doctor`."""
        if self.binary and not shutil.which(self.binary):
            return False, f"{self.binary} not on PATH"
        return True, "ok"

    def section(self) -> str:
        """The channel's slice of the snapshot document."""
        try:
            body = self.snapshot()
        except Exception as exc:  # a broken channel must not kill the tick
            body = f"(snapshot failed: {exc})"
        return f"\n## {self.label or self.name}\n{body}\n"
