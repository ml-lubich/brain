"""Channel registry. Every Channel subclass in this package is auto-discovered.

To add a channel: create `brain/channels/<name>.py` with a Channel subclass.
That is the whole procedure.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Channel, run

__all__ = ["Channel", "run", "all_channels", "get"]

_cache: dict[str, Channel] | None = None


def all_channels() -> dict[str, Channel]:
    global _cache
    if _cache is not None:
        return _cache

    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_") and mod.name != "base":
            importlib.import_module(f"{__name__}.{mod.name}")

    found: dict[str, Channel] = {}
    for cls in Channel.__subclasses__():
        if cls.name:
            found[cls.name] = cls()
    _cache = dict(sorted(found.items()))
    return _cache


def get(name: str) -> Channel:
    try:
        return all_channels()[name]
    except KeyError:
        known = ", ".join(all_channels()) or "none"
        raise KeyError(f"unknown channel {name!r} (have: {known})") from None
