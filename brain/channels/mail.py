"""Apple Mail via the imail CLI.

Not sendable on purpose: approval for email is Mail.app's own draft folder.
The agent writes unsent drafts, they sync to the phone, the human hits send.
"""

from __future__ import annotations

from .base import Channel, run


class Mail(Channel):
    name = "mail"
    label = "Email (newest first)"
    binary = "imail"
    sendable = False  # drafts only — see module docstring

    def snapshot(self) -> str:
        return run(["imail", "list", "--limit", "15", "--json"], timeout=60)

    def available(self) -> tuple[bool, str]:
        ok, why = super().available()
        if not ok:
            return ok, why
        out = run(["imail", "doctor"], timeout=30).lower()
        if "error" in out or "not reachable" in out:
            return False, "Mail.app not reachable (imail doctor)"
        return True, "ok"
