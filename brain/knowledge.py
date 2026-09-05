"""Shared knowledge: markdown in git (source of truth) + SQLite FTS5 (derived index).

Two stores, on purpose:

  notes/*.md   git-tracked, mergeable, human-readable, THE data. Never deleted
               by this module.
  knowledge.db SQLite FTS5, gitignored, rebuilt from the markdown at any time.
               Derived. Losing it costs nothing but a reindex.

That split is what lets a binary search index coexist with `git pull`. Every
Claude Code session on the machine reads the same notes; `brain sync` moves them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config

NOTES_DIRNAME = "notes"

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
    slug UNINDEXED,
    title,
    tags,
    body,
    path UNINDEXED,
    updated UNINDEXED,
    tokenize = "porter unicode61"
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


@dataclass
class Note:
    slug: str
    title: str
    tags: list[str]
    body: str
    path: Path
    updated: str = ""

    @property
    def digest(self) -> str:
        """Content hash used to detect near-duplicate writes."""
        norm = re.sub(r"\s+", " ", self.body.strip().lower())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    def render(self) -> str:
        return (
            "---\n"
            f"title: {self.title}\n"
            f"tags: {', '.join(self.tags)}\n"
            f"updated: {self.updated or _today()}\n"
            "---\n\n"
            f"{self.body.strip()}\n"
        )


def _today() -> str:
    return dt.date.today().isoformat()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "note")[:60]


def notes_dir() -> Path:
    return config.KNOWLEDGE / NOTES_DIRNAME


def db_path() -> Path:
    return config.DIR / "knowledge.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.executescript(SCHEMA)
    return con


def parse(path: Path) -> Note:
    raw = path.read_text()
    title, tags, updated = path.stem.replace("-", " "), [], ""
    match = FRONTMATTER.match(raw)
    body = raw
    if match:
        body = raw[match.end():]
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "title" and value:
                title = value
            elif key == "tags" and value:
                tags = [t.strip() for t in value.split(",") if t.strip()]
            elif key == "updated":
                updated = value
    return Note(path.stem, title, tags, body.strip(), path, updated)


def all_notes() -> list[Note]:
    if not notes_dir().exists():
        return []
    return [parse(p) for p in sorted(notes_dir().glob("*.md"))]


def reindex() -> int:
    """Rebuild the search index from the markdown. Safe to run anytime —
    it only ever drops the DERIVED index, never a note."""
    con = connect()
    with con:
        con.execute("DELETE FROM notes")
        for note in all_notes():
            con.execute(
                "INSERT INTO notes (slug,title,tags,body,path,updated) VALUES (?,?,?,?,?,?)",
                (note.slug, note.title, " ".join(note.tags), note.body,
                 str(note.path), note.updated),
            )
        con.execute(
            "INSERT OR REPLACE INTO meta (key,value) VALUES ('reindexed',?)",
            (dt.datetime.now().isoformat(timespec="seconds"),),
        )
    count = con.execute("SELECT count(*) FROM notes").fetchone()[0]
    con.close()
    return count


def find_duplicate(body: str) -> Note | None:
    """An agent writing every 10 minutes will re-learn the same fact forever
    unless something stops it. Exact-content match is the cheap 90% of that."""
    probe = Note("", "", [], body, Path("."))
    for note in all_notes():
        if note.digest == probe.digest:
            return note
    return None


def learn(text: str, title: str = "", tags: list[str] | None = None,
          append: bool = False) -> tuple[Path, str]:
    """Write a note. Returns (path, action) where action is
    created | appended | duplicate. Never overwrites blindly."""
    notes_dir().mkdir(parents=True, exist_ok=True)
    tags = tags or []
    title = title or " ".join(text.split()[:8])
    path = notes_dir() / f"{slugify(title)}.md"

    existing = find_duplicate(text)
    if existing is not None:
        return existing.path, "duplicate"

    if path.exists():
        note = parse(path)
        if not append:
            # Same title, different content. Keep both — never clobber a note.
            stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
            path = notes_dir() / f"{slugify(title)}-{stamp}.md"
        else:
            note.body = f"{note.body}\n\n{text.strip()}"
            note.updated = _today()
            path.write_text(note.render())
            reindex()
            return path, "appended"

    path.write_text(Note(path.stem, title, tags, text, path, _today()).render())
    reindex()
    return path, "created"


def recall(query: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """Full-text search. Returns (title, snippet, path)."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT title, snippet(notes, 3, '[', ']', ' … ', 18), path "
            "FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # A bare word with FTS syntax chars in it — quote and retry.
        rows = con.execute(
            "SELECT title, snippet(notes, 3, '[', ']', ' … ', 18), path "
            "FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?",
            ('"' + query.replace('"', "") + '"', limit),
        ).fetchall()
    finally:
        con.close()
    return rows


def stats() -> dict[str, str]:
    con = connect()
    count = con.execute("SELECT count(*) FROM notes").fetchone()[0]
    row = con.execute("SELECT value FROM meta WHERE key='reindexed'").fetchone()
    con.close()
    return {
        "notes": str(count),
        "indexed": row[0] if row else "never",
        "dir": str(notes_dir()),
        "index": str(db_path()),
    }
