"""Paths and settings. Everything lives under ~/.config/brain."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
DIR = Path(os.environ.get("BRAIN_HOME", HOME / ".config" / "brain"))

CONFIG_ENV = DIR / "config.env"
QUEUE = DIR / "queue.jsonl"
CONTEXT = DIR / "context.md"
STATE = DIR / "state"
LOG = DIR / "brain.log"
DIGEST = DIR / "digest.txt"

TODO = Path(os.environ.get("AGENT_TODO", HOME / ".config" / "agent-todo" / "TODO.md"))
MEMORY = HOME / ".claude" / "projects" / "-Users-mlubich" / "memory" / "MEMORY.md"

# The wa CLI cannot locate its bridge without this.
WA_REPO = Path(os.environ.get("WA_REPO", HOME / "dev" / "whatsapp-mcp"))

POLL_SECONDS = int(os.environ.get("BRAIN_POLL_SECONDS", "600"))
DIGEST_HOUR = int(os.environ.get("BRAIN_DIGEST_HOUR", "22"))

# Identity the agent drafts as.
FROM_ADDRESS = os.environ.get("BRAIN_FROM", "michaelle.lubich@gmail.com")


def ensure_dirs() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    QUEUE.touch(exist_ok=True)


def load_env() -> dict[str, str]:
    """Read config.env (KEY=value lines) into os.environ and return it."""
    found: dict[str, str] = {}
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if value:
                found[key] = value
                os.environ.setdefault(key, value)
    os.environ.setdefault("WA_REPO", str(WA_REPO))
    return found


CONFIG_TEMPLATE = """\
# brain config — sourced on every run. Secrets only. Not in git.

# Telegram = phone notifications + remote approve/deny + voice-note prompts.
# Setup (2 min, one time):
#   1. Telegram -> @BotFather -> /newbot -> copy the token
#   2. Message your new bot once ("hi")
#   3. curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \\
#        | jq '.result[0].message.chat.id'
#   4. Paste both below and uncomment.
# TELEGRAM_TOKEN=
# TELEGRAM_CHAT_ID=

# Override where the WhatsApp bridge repo lives, if it ever moves.
# WA_REPO=
"""
