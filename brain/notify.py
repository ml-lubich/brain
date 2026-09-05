"""Notifications out. macOS always, Telegram when configured."""

from __future__ import annotations

import os
import subprocess
import urllib.parse
import urllib.request


def _macos(title: str, body: str) -> None:
    script = (
        f'display notification {_osa(body)} with title {_osa(title)}'
    )
    subprocess.run(
        ["osascript", "-e", script], capture_output=True, check=False, timeout=15
    )


def _osa(text: str) -> str:
    """Quote a string for AppleScript."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _telegram(title: str, body: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{body}"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data, timeout=15):
            return True
    except Exception:
        return False  # a dead notification channel must never fail a tick


def send(title: str, body: str) -> None:
    _macos(title, body)
    _telegram(title, body)


def configured() -> str:
    return (
        "macOS + Telegram"
        if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        else "macOS only (no Telegram token)"
    )
