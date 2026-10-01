"""Session index and multi-agent transcript recall.

Detects, scans, and searches session transcripts across:
- Antigravity / Gemini CLI (`transcript.jsonl`)
- OpenAI Codex (`rollout-*.jsonl`)
- Claude Code (`sessions/*.json` or claude-mem)
- Cursor / OpenCode sessions
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SessionType = Literal["antigravity", "codex", "claude", "cursor", "opencode", "unknown"]

@dataclass
class SessionInfo:
    session_id: str
    session_type: SessionType
    path: Path
    timestamp: str
    snippet: str

def detect_session_type(path: Path) -> SessionType:
    name = path.name.lower()
    if name == "transcript.jsonl" or name == "transcript_full.jsonl":
        return "antigravity"
    if "codex" in str(path).lower() or name.startswith("rollout-"):
        return "codex"
    if "claude" in str(path).lower():
        return "claude"
    if "cursor" in str(path).lower():
        return "cursor"
    if "opencode" in str(path).lower():
        return "opencode"
    
    # Inspect content header if needed
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline()
            if "USER_EXPLICIT" in first_line or "PLANNER_RESPONSE" in first_line:
                return "antigravity"
            if "codex" in first_line:
                return "codex"
            if "messages" in first_line or "claude" in first_line:
                return "claude"
    except Exception:
        pass

    return "unknown"

def default_session_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / ".gemini" / "antigravity-cli" / "brain",
        home / ".codex" / "sessions",
        home / ".claude" / "sessions",
        home / ".cursor",
    ]
    return [p for p in roots if p.exists()]

def scan_sessions(roots: list[Path] | None = None) -> list[Path]:
    if roots is None:
        roots = default_session_roots()
    session_files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.json*"):
            if p.is_file():
                if p.name.endswith(".key"):
                    continue
                session_files.append(p)
    return session_files

def _extract_text(line: str) -> str:
    try:
        data = json.loads(line)
        if isinstance(data, dict):
            content = data.get("content") or data.get("payload", {})
            if isinstance(content, dict):
                return json.dumps(content)
            return str(content)
    except Exception:
        pass
    return line

def search_sessions(query: str, search_paths: list[Path] | None = None, limit: int = 20) -> list[SessionInfo]:
    files = scan_sessions(search_paths)
    q = query.lower()
    matches: list[SessionInfo] = []
    
    for f in files:
        stype = detect_session_type(f)
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if q in line.lower():
                        text = _extract_text(line)
                        idx = text.lower().find(q)
                        start = max(0, idx - 60)
                        end = min(len(text), idx + 100)
                        snippet = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
                        
                        sess_id = f.stem
                        if f.parent.name == "logs" and f.parent.parent.name == ".system_generated":
                            sess_id = f.parent.parent.parent.name
                        
                        matches.append(SessionInfo(
                            session_id=sess_id,
                            session_type=stype,
                            path=f,
                            timestamp="",
                            snippet=snippet,
                        ))
                        if len(matches) >= limit:
                            return matches
                        break
        except Exception:
            continue

    return matches
