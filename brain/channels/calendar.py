"""Today's calendar, via EventKit.

Not AppleScript. Enumerating `every event of c whose start date ≥ …` across
the eleven calendars on this Mac takes **68 seconds**, and narrowing it to four
named calendars still takes twenty — against a ten-minute tick with per-command
timeouts, either would stall every cycle. The same query through EventKit,
compiled with swiftc, answers in 0.2s.

So the work lives in helpers/calendar-today.swift. The channel runs the
compiled binary when it exists and interprets the source (~6s, mostly compile)
when it does not, so a fresh checkout still works before anything is built.

Read-only: it reports the day, including the Meet/Zoom link on each event.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import Channel, run

#: Built by `brain install`; the fallback below covers a fresh checkout.
HELPER_BIN = Path.home() / ".local" / "bin" / "brain-calendar"
HELPER_SRC = Path(__file__).resolve().parents[2] / "helpers" / "calendar-today.swift"

DENIED = "access not granted"


class Calendar(Channel):
    name = "cal"
    label = "Calendar (today)"
    binary = ""  # resolved below — either the built helper or swift itself
    sendable = False

    def _command(self) -> list[str]:
        if HELPER_BIN.exists():
            return [str(HELPER_BIN)]
        return ["swift", str(HELPER_SRC)]

    def snapshot(self) -> str:
        # Generous timeout: the fallback path pays a Swift compile every call.
        return run(self._command(), timeout=90)

    def available(self) -> tuple[bool, str]:
        if not HELPER_BIN.exists() and not shutil.which("swift"):
            return False, "neither brain-calendar nor swift is available"
        out = self.snapshot()
        if DENIED in out:
            return False, "Calendar access not granted — System Settings › Privacy › Calendars"
        return True, "ok" if HELPER_BIN.exists() else "ok (interpreting source; run `brain install` to compile)"
