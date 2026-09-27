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
import difflib
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

    def to_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "title": self.title,
            "tags": list(self.tags),
            "body": self.body,
            "path": str(self.path),
            "updated": self.updated or _today(),
        }

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


def get_note(slug_or_title: str) -> Note | None:
    """Retrieve a note by exact slug, filename, or case-insensitive title."""
    probe = slug_or_title.strip()
    slug = slugify(probe)
    target = notes_dir() / f"{slug}.md"
    if target.exists():
        return parse(target)
    # Check direct filename match if user passed e.g. "foo.md"
    if (notes_dir() / probe).exists():
        return parse(notes_dir() / probe)
    # Fallback to linear scan by title or slug
    probe_lower = probe.lower()
    for note in all_notes():
        if note.slug == probe_lower or note.title.lower() == probe_lower:
            return note
    return None


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


def _archived(path: Path) -> bool:
    """Ensure the current text of `path` exists in git history, and say whether
    it does. Overwriting is only safe once something else holds the old copy.
    """
    from . import sync as gitsync
    if not gitsync.is_repo():
        return False
    if gitsync.dirty():
        gitsync.commit(f"snapshot before editing {path.name}")
    return not gitsync.dirty()


def learn(text: str, title: str = "", tags: list[str] | None = None,
          append: bool = False, force: bool = False) -> tuple[Path, str]:
    """Write a note.

    Returns (path, action): created | appended | updated | duplicate | similar.
    Nothing is ever lost — an in-place edit is committed first, so the previous
    version stays in git history.
    """
    notes_dir().mkdir(parents=True, exist_ok=True)
    tags = tags or []
    title = title or " ".join(text.split()[:8])
    path = notes_dir() / f"{slugify(title)}.md"

    if not force:
        exact = find_duplicate(text)
        if exact is not None:
            return exact.path, "duplicate"
        if not append:
            near, _ratio = similar(text, title)
            if near is not None and near.path != path:
                return near.path, "similar"

    if path.exists():
        note = parse(path)
        if not append and not _archived(path):
            # No git history here, so an in-place edit would be the only copy.
            # Fork instead: never overwrite text that nothing else preserves.
            stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
            path = notes_dir() / f"{slugify(title)}-{stamp}.md"
        else:
            note.body = f"{note.body}\n\n{text.strip()}" if append else text.strip()
            note.tags = sorted(set(note.tags) | set(tags))
            note.updated = _today()
            path.write_text(note.render())
            reindex()
            return path, "appended" if append else "updated"

    path.write_text(Note(path.stem, title, tags, text, path, _today()).render())
    reindex()
    return path, "created"


STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in",
    "on", "for", "with", "that", "this", "it", "as", "at", "by", "from", "not",
}

SELECT = ("SELECT title, snippet(notes, 3, '«', '»', ' … ', 18), path "
          "FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?")


def terms(text: str, limit: int = 12) -> list[str]:
    """Distinctive words, longest first — identifiers and paths beat filler.

    Leading punctuation is stripped: a term like `--prod` is FTS5's NOT
    operator, which turns the whole query into a syntax error and silently
    returns zero rows. Zero rows is exactly the state that makes an agent
    believe nothing is known and write a duplicate.
    """
    words = re.findall(r"[A-Za-z0-9_.-]{3,}", text.lower())
    seen, out = set(), []
    for word in sorted(words, key=len, reverse=True):
        word = word.strip("-._")
        if len(word) < 3 or word in STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out[:limit]


def _fts_or(words: list[str]) -> str:
    """Quote every term so hyphens and dots are literals, not operators."""
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words)


def _match(con, expression: str, limit: int) -> list[tuple[str, str, str]]:
    try:
        return con.execute(SELECT, (expression, limit)).fetchall()
    except sqlite3.OperationalError:
        return []


def recall(query: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """Full-text search. Returns (title, snippet, path).

    On a thin result set, retry with the terms OR'd. FTS5 will not match a
    paraphrase, and the symptom is not slowness — it is an agent reading zero
    rows as "nothing is known" and writing a near-duplicate. Widening on a miss
    is the cheap half of the fix; deliberate tags are the other half.
    """
    con = connect()
    try:
        rows = _match(con, query, limit)
        if len(rows) < 3:
            words = terms(query)
            if words:
                widened = _match(con, _fts_or(words), limit)
                seen = {r[2] for r in rows}
                rows += [r for r in widened if r[2] not in seen]
        if not rows:
            rows = _match(con, '"' + query.replace('"', "") + '"', limit)
    finally:
        con.close()
    return rows[:limit]


def all_tags() -> dict[str, int]:
    """Tags in use, with counts. A closed vocabulary is the semantic bridge
    that FTS5 cannot give you and embeddings would be overkill for."""
    counts: dict[str, int] = {}
    for note in all_notes():
        for tag in note.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# Calibrated on real note pairs rather than guessed. A reworded duplicate of an
# existing note measured difflib 0.70 / term-overlap 0.31; unrelated notes in the
# same store measured 0.22-0.24 / 0.00. Both signals must agree, which keeps a
# shared writing style from flagging genuinely new material.
SIMILAR_RATIO = 0.55
SIMILAR_OVERLAP = 0.15


def _overlap(a: str, b: str) -> float:
    """Jaccard over distinctive terms. Survives paraphrase where character
    diffing wobbles, and goes to zero between unrelated notes."""
    ta, tb = set(terms(a, 30)), set(terms(b, 30))
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def similar(text: str, title: str = "", threshold: float = SIMILAR_RATIO):
    """(note, ratio) of an existing note that already says this.

    Byte-identical hashing catches nothing an LLM produces — every real
    duplicate is a reword. Fuzzy-compare, but only against the handful of
    candidates FTS already ranked, so this stays bounded as the corpus grows.
    """
    probe = _normalise(text)
    candidates = recall(" ".join(terms(f"{title} {text}")) or text, limit=5)
    best, best_ratio = None, 0.0
    for _, _, path in candidates:
        note = parse(Path(path))
        ratio = difflib.SequenceMatcher(None, _normalise(note.body), probe).ratio()
        if ratio > best_ratio and _overlap(note.body, text) >= SIMILAR_OVERLAP:
            best, best_ratio = note, ratio
    return (best, best_ratio) if best_ratio >= threshold else (None, best_ratio)


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
