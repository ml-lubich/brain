"""Wake headless Claude, but only when something actually changed.

The whole point of the fingerprint: an idle inbox must cost zero tokens.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import subprocess

from . import config
from .channels import all_channels

# The background agent may read, edit, and draft. It may not send. These are
# passed to --disallowedTools as well as being absent from --allowedTools,
# because this boundary is the only thing standing between "drafts a reply"
# and "emails a client at 3am unattended".
FORBIDDEN = ["Bash(imail send:*)", "Bash(imsg send:*)", "Bash(wa send:*)"]

ALLOWED = [
    "Read",
    "Edit",
    "Write",
    "Bash(imail draft:*)",
    "Bash(imail list:*)",
    "Bash(imsg read:*)",
    "Bash(imsg chats:*)",
    "Bash(wa chats:*)",
    "Bash(cat:*)",
    "Bash(echo:*)",
]

PROMPT = """\
You are Misha's always-on inbox agent, running headless on a timer.

Read {context} — a snapshot of his email, iMessage, and WhatsApp.
Read {todo} for current project state.

For each item that genuinely needs a reply from Misha:

EMAIL -> create a real unsent Mail.app draft:
  imail draft --to <addr> --from {sender} --subject "Re: ..." --body "..."
  Never run `imail send`. Drafts only. He approves by hitting send on his phone.

{sendable} -> do NOT send. Append one JSON line per proposal to {queue}:
  {{"ch":"<channel>","to":"<handle>","re":"<what they said, 1 line>","msg":"<proposed reply>"}}

PROJECTS -> if anything changes the state of his consulting work, interview
pipeline, or active projects, update the matching section of {todo} in place.
Keep it tight. Do not create new files.

Rules:
- Skip newsletters, receipts, notifications, automated mail. Silence is a valid outcome.
- Match his voice: short, direct, lowercase-ish, no corporate filler, no em dashes.
- Booking CTA when relevant: mishalubich.com
- Never send. Never delete. Never touch Apple Notes.
- Finish with ONE line exactly: "N drafts, M proposals" (0 0 if nothing was worth doing).
"""


def log(message: str) -> None:
    config.ensure_dirs()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with config.LOG.open("a") as fh:
        fh.write(f"{stamp} {message}\n")


def snapshot() -> str:
    """Build the document the agent reads. First line is a timestamp, which is
    excluded from the fingerprint so the clock alone never triggers work."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"# Inbox snapshot {stamp}"]
    parts += [ch.section() for ch in all_channels().values()]
    doc = "\n".join(parts)
    config.ensure_dirs()
    config.CONTEXT.write_text(doc)
    return doc


def fingerprint(doc: str) -> str:
    body = "\n".join(doc.splitlines()[1:])  # drop the timestamp line
    return hashlib.sha256(body.encode()).hexdigest()


def changed(doc: str) -> bool:
    fp = fingerprint(doc)
    previous = config.STATE.read_text().strip() if config.STATE.exists() else ""
    if fp == previous:
        return False
    config.STATE.write_text(fp)
    return True


def build_prompt() -> str:
    sendable = " / ".join(
        c.label.split(" (")[0].upper()
        for c in all_channels().values()
        if c.sendable
    ) or "QUEUED CHANNELS"
    return PROMPT.format(
        context=config.CONTEXT,
        todo=config.TODO,
        queue=config.QUEUE,
        sender=config.FROM_ADDRESS,
        sendable=sendable,
    )


def invoke(timeout: int = 900) -> str:
    """Run claude -p with the send tools removed. Returns its trailing output."""
    cmd = ["claude", "-p", build_prompt(), "--allowedTools", *ALLOWED,
           "--disallowedTools", *FORBIDDEN]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            check=False, cwd=str(config.HOME),
        )
    except FileNotFoundError:
        return "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return f"claude timed out after {timeout}s"
    return (proc.stdout + proc.stderr).strip()


def digest(timeout: int = 600) -> str:
    prompt = (
        f"Read {config.TODO} and {config.MEMORY}. Write a 10-line end-of-day "
        "briefing for Misha: what moved today, what is blocked and on whom, and "
        "the single highest-leverage thing to do tomorrow across his consulting "
        "work, interview pipeline, and active projects. Plain text, no preamble, "
        "no markdown headers."
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "Read", "Bash(cat:*)"],
            capture_output=True, text=True, timeout=timeout, check=False,
            cwd=str(config.HOME),
        )
    except FileNotFoundError:
        return "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return f"claude timed out after {timeout}s"
    text = (proc.stdout + proc.stderr).strip()
    config.ensure_dirs()
    config.DIGEST.write_text(text)
    return text
