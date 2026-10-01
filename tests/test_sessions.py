import pytest
from pathlib import Path
from brain.sessions import detect_session_type, SessionInfo, scan_sessions, search_sessions

def test_detect_session_type(tmp_path):
    # AGY transcript
    agy_file = tmp_path / "transcript.jsonl"
    agy_file.write_text('{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"hello agy"}')
    assert detect_session_type(agy_file) == "antigravity"

    # Codex session
    codex_file = tmp_path / "rollout-2026-07-29.jsonl"
    codex_file.write_text('{"timestamp":"2026-07-29T21:26:31.373Z","type":"session_meta","payload":{"originator":"codex-tui"}}')
    assert detect_session_type(codex_file) == "codex"

    # Claude session
    claude_file = tmp_path / "session.json"
    claude_file.write_text('{"messages":[{"role":"user","content":"hello claude"}]}')
    assert detect_session_type(claude_file) == "claude"

def test_scan_and_search_sessions(tmp_path):
    sess_dir = tmp_path / "agy" / "sess-1" / ".system_generated" / "logs"
    sess_dir.mkdir(parents=True)
    transcript = sess_dir / "transcript.jsonl"
    transcript.write_text('{"step_index":0,"source":"USER_EXPLICIT","content":"deploying vercel with prod flag"}')

    results = search_sessions("vercel", search_paths=[tmp_path])
    assert len(results) >= 1
    assert results[0].session_type == "antigravity"
    assert "vercel" in results[0].snippet.lower()
