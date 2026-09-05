"""iMessage via the imsg CLI."""

from __future__ import annotations

from .base import Channel, run


class IMessage(Channel):
    name = "imsg"
    label = "iMessage (recent)"
    binary = "imsg"

    def snapshot(self) -> str:
        return run(["imsg", "read", "--limit", "25"], timeout=60)

    def send(self, to: str, message: str) -> str:
        return run(["imsg", "send", to, message], timeout=60)

    def available(self) -> tuple[bool, str]:
        ok, why = super().available()
        if not ok:
            return ok, why
        out = run(["imsg", "doctor"], timeout=30).lower()
        if "full disk access" in out and ("missing" in out or "denied" in out):
            return False, "Full Disk Access not granted (imsg doctor)"
        return True, "ok"
