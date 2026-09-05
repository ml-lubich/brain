"""WhatsApp via the local bridge.

The wa CLI has no message-content command — only chats/contacts/send — so the
snapshot reads the bridge's SQLite store directly. Sending still goes through
`wa send` so the bridge owns delivery.
"""

from __future__ import annotations

import sqlite3

from .. import config
from .base import Channel, run

RECENT = """
SELECT m.timestamp,
       COALESCE(NULLIF(c.name, ''), m.chat_jid) AS chat,
       CASE m.is_from_me WHEN 1 THEN 'me' ELSE 'them' END AS who,
       substr(m.content, 1, 300) AS msg
  FROM messages m
  LEFT JOIN chats c ON c.jid = m.chat_jid
 WHERE m.content <> '' AND m.chat_jid <> 'status@broadcast'
 ORDER BY m.timestamp DESC
 LIMIT 30
"""


class WhatsApp(Channel):
    name = "wa"
    label = "WhatsApp (recent messages)"
    binary = "wa"

    @property
    def db(self):
        return config.WA_REPO / "whatsapp-bridge" / "store" / "messages.db"

    def snapshot(self) -> str:
        if not self.db.exists():
            return f"(bridge db missing at {self.db} — set WA_REPO or run `wa up`)"
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            rows = con.execute(RECENT).fetchall()
        finally:
            con.close()
        if not rows:
            return "(no messages)"
        return "\n".join(f"{ts}  {chat}  {who}: {msg}" for ts, chat, who, msg in rows)

    def send(self, to: str, message: str) -> str:
        return run(["wa", "send", to, message], timeout=60)

    def available(self) -> tuple[bool, str]:
        ok, why = super().available()
        if not ok:
            return ok, why
        if not self.db.exists():
            return False, f"bridge db missing at {self.db}"
        if "up" not in run(["wa", "status"], timeout=30):
            return False, "bridge daemon down (`wa up`)"
        return True, "ok"
