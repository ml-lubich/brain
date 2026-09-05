"""The approval queue: one JSON object per line, oldest first.

This is deliberately a flat file. It holds proposals the agent wants to send on
a sendable channel, and `brain approve` is the only code path that sends them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config


@dataclass
class Proposal:
    ch: str
    to: str
    re: str = ""
    msg: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, line: str) -> "Proposal":
        raw = json.loads(line)
        known = {"ch", "to", "re", "msg"}
        return cls(
            ch=str(raw.get("ch", "")),
            to=str(raw.get("to", "")),
            re=str(raw.get("re", "")),
            msg=str(raw.get("msg", "")),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def dumps(self) -> str:
        return json.dumps(
            {"ch": self.ch, "to": self.to, "re": self.re, "msg": self.msg, **self.extra},
            ensure_ascii=False,
        )


def load() -> list[Proposal]:
    if not config.QUEUE.exists():
        return []
    out = []
    for line in config.QUEUE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Proposal.parse(line))
        except (json.JSONDecodeError, TypeError):
            continue  # a malformed line should not block the rest of the queue
    return out


def save(items: list[Proposal]) -> None:
    config.QUEUE.write_text("".join(p.dumps() + "\n" for p in items))


def add(p: Proposal) -> None:
    with config.QUEUE.open("a") as fh:
        fh.write(p.dumps() + "\n")


def pop(index: int) -> Proposal:
    """Remove and return the 1-based item at `index`."""
    items = load()
    if not 1 <= index <= len(items):
        raise IndexError(f"no item {index} (queue has {len(items)})")
    item = items.pop(index - 1)
    save(items)
    return item


def peek(index: int) -> Proposal:
    items = load()
    if not 1 <= index <= len(items):
        raise IndexError(f"no item {index} (queue has {len(items)})")
    return items[index - 1]


def count() -> int:
    return len(load())
