"""GitHub via the gh CLI.

The other thing on this Mac that sits waiting on a human: a review someone
asked for, and his own PRs — especially the red ones.

Read-only on purpose. `sendable = False`, and `send()` raises: a background
agent that could comment or merge as ml-lubich is a different risk class from
one that drafts a reply for approval. It reports; he acts.
"""

from __future__ import annotations

import shutil

from .base import Channel, run

# Compact by design — this text is pasted into a prompt on every tick, and the
# agent needs "who is waiting on me", not a changelog.
_FIELDS = "number,title,repository,updatedAt,url"
_LIMIT = "15"


class GitHub(Channel):
    name = "gh"
    label = "GitHub (review requests & own PRs)"
    binary = "gh"
    sendable = False

    def snapshot(self) -> str:
        requested = run(
            ["gh", "search", "prs", "--review-requested=@me", "--state=open",
             f"--limit={_LIMIT}", f"--json={_FIELDS}"],
            timeout=60,
        )
        mine = run(
            ["gh", "search", "prs", "--author=@me", "--state=open",
             f"--limit={_LIMIT}", f"--json={_FIELDS}"],
            timeout=60,
        )
        return (
            f"### Review requested of me\n{requested or '(none)'}\n\n"
            f"### My open PRs\n{mine or '(none)'}"
        )

    def available(self) -> tuple[bool, str]:
        if not shutil.which(self.binary):
            return False, "gh not on PATH"
        status = run(["gh", "auth", "status"], timeout=20)
        if "not logged" in status.lower():
            return False, "not authenticated — run `gh auth login`"
        return True, "ok"
