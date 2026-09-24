"""The checks that fail if the logic breaks. No fixtures, no ceremony."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    for mod in [m for m in list(sys.modules) if m.startswith("brain")]:
        del sys.modules[mod]
    from brain import config
    config.ensure_dirs()
    return config


def test_fingerprint_ignores_the_timestamp_line(tmp_path, monkeypatch):
    """The whole token-saving gate rests on this: same inbox, different clock,
    same fingerprint. If this breaks, every tick invokes Claude."""
    _isolate(tmp_path, monkeypatch)
    from brain import agent
    a = "# Inbox snapshot 2026-09-04 10:00:00\n\n## Email\nhello"
    b = "# Inbox snapshot 2026-09-04 23:59:59\n\n## Email\nhello"
    c = "# Inbox snapshot 2026-09-04 10:00:00\n\n## Email\nsomething new"
    assert agent.fingerprint(a) == agent.fingerprint(b)
    assert agent.fingerprint(a) != agent.fingerprint(c)


def test_changed_is_true_once_then_false(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import agent
    doc = "# Inbox snapshot X\n\nbody"
    assert agent.changed(doc) is True
    assert agent.changed(doc) is False
    assert agent.changed("# Inbox snapshot X\n\ndifferent") is True


def test_queue_roundtrip_and_1_based_pop(tmp_path, monkeypatch):
    config = _isolate(tmp_path, monkeypatch)
    from brain import queue
    for i in range(3):
        queue.add(queue.Proposal(ch="imsg", to=f"+1555000000{i}", re="hi", msg=f"m{i}"))
    assert queue.count() == 3
    assert queue.pop(2).msg == "m1"           # 1-based, middle removal
    assert [p.msg for p in queue.load()] == ["m0", "m2"]
    try:
        queue.pop(9)
        raise AssertionError("expected IndexError")
    except IndexError:
        pass


def test_queue_survives_a_malformed_line(tmp_path, monkeypatch):
    config = _isolate(tmp_path, monkeypatch)
    from brain import queue
    config.QUEUE.write_text(
        '{"ch":"wa","to":"a","msg":"good"}\nNOT JSON\n{"ch":"imsg","to":"b","msg":"also good"}\n'
    )
    assert [p.msg for p in queue.load()] == ["good", "also good"]


def test_send_tools_are_forbidden(tmp_path, monkeypatch):
    """The only thing preventing unattended sends. Never let this regress."""
    _isolate(tmp_path, monkeypatch)
    from brain import agent
    for tool in ("Bash(imail send:*)", "Bash(imail autodraft:*)", "Bash(imsg send:*)", "Bash(wa send:*)"):
        assert tool in agent.FORBIDDEN
        assert tool not in agent.ALLOWED
    assert not any("send" in t for t in agent.ALLOWED)


def test_channels_are_discovered_without_registration(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import all_channels
    found = all_channels()
    assert {"mail", "imsg", "wa"} <= set(found)
    assert found["mail"].sendable is False   # email approval is Mail.app drafts
    assert found["imsg"].sendable is True


def test_broken_channel_does_not_kill_the_snapshot(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels.base import Channel

    class Exploding(Channel):
        name, label = "boom", "Exploding"
        def snapshot(self): raise RuntimeError("kaboom")

    section = Exploding().section()
    assert "snapshot failed" in section and "kaboom" in section


# --- github channel ---------------------------------------------------------
# Code review requests and red CI are the other thing on this Mac that waits on
# a human. Read-only: a channel that could merge or comment on Misha's behalf is
# a different risk class from drafting a reply.


def test_github_channel_is_discovered_and_cannot_send(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import all_channels

    found = all_channels()
    assert "gh" in found
    assert found["gh"].sendable is False


def test_github_snapshot_asks_for_review_requests_and_own_prs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import github as gh_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(gh_mod, "run", lambda cmd, timeout=30: (calls.append(cmd), "")[1])

    gh_mod.GitHub().snapshot()

    flat = [" ".join(c) for c in calls]
    assert any("--review-requested=@me" in f for f in flat), "must surface review requests"
    assert any("--author=@me" in f for f in flat), "must surface his own open PRs"
    assert all(c[0] == "gh" for c in calls)


def test_github_channel_refuses_to_send(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels.github import GitHub
    import pytest as _pytest

    with _pytest.raises(NotImplementedError):
        GitHub().send("someone", "hi")


def test_github_reports_unauthenticated_rather_than_failing_the_tick(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import github as gh_mod

    monkeypatch.setattr(gh_mod.shutil, "which", lambda _b: "/usr/bin/gh")
    monkeypatch.setattr(gh_mod, "run", lambda cmd, timeout=30: "You are not logged into any GitHub hosts")
    ok, why = gh_mod.GitHub().available()
    assert ok is False
    assert "gh auth login" in why


def test_read_only_channels_do_not_claim_to_deliver_mail_drafts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import all_channels
    from brain.main import _delivery

    found = all_channels()
    assert _delivery(found["gh"]) == "read-only"
    assert _delivery(found["cal"]) == "read-only"
    assert _delivery(found["mail"]) == "Mail.app drafts"
    assert _delivery(found["imsg"]) == "brain approve"


def test_cli_still_exposes_its_commands(tmp_path, monkeypatch):
    # A helper accidentally inserted between @app.command() and its function
    # silently unregistered `channels` while every other test stayed green.
    _isolate(tmp_path, monkeypatch)
    from brain.main import app

    registered = {c.callback.__name__ for c in app.registered_commands}
    assert {"channels", "doctor", "tick", "approve", "reply"} <= registered


def test_mail_snapshot_covers_all_personal_accounts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import mail as mail_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(mail_mod, "run", lambda cmd, timeout=30: (calls.append(cmd), "[]")[1])

    out = mail_mod.Mail().snapshot()
    accounts = [c[c.index("--account") + 1] for c in calls if "--account" in c]
    assert accounts == [
        "michaelle.lubich@gmail.com",
        "metropol007@gmail.com",
        "misha@lupfr.com",
    ]
    assert "michaelle.lubich@gmail.com" in out
    assert "metropol007@gmail.com" in out
    assert "misha@lupfr.com" in out


def test_headless_claude_cannot_run_imail_autodraft(tmp_path, monkeypatch):
    """autodraft can send low-stakes follow-ups. Headless Claude must not."""
    _isolate(tmp_path, monkeypatch)
    from brain import agent
    assert "Bash(imail autodraft:*)" in agent.FORBIDDEN
    assert not any("autodraft" in t for t in agent.ALLOWED)


def test_reply_job_runs_morning_lunch_evening(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import service
    assert service.REPLY == "com.mlubich.brain-reply"
    assert service.REPLY in service.ALL
    written: list[tuple[str, str, str]] = []
    monkeypatch.setattr(service, "_write", lambda label, command, schedule: written.append((label, command, schedule)) or tmp_path / f"{label}.plist")
    monkeypatch.setattr(service, "_launchctl", lambda *a: (0, ""))
    service.install()
    labels = [w[0] for w in written]
    assert service.REPLY in labels
    reply = next(w for w in written if w[0] == service.REPLY)
    assert reply[1] == "reply"
    assert "StartCalendarInterval" in reply[2] and "3600" not in reply[2]
    for h, m in service.REPLY_TIMES:
        assert f"<key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer>" in reply[2]
    assert service.REPLY_TIMES == [(8, 0), (14, 0), (19, 0)]


# --- calendar channel -------------------------------------------------------
# AppleScript cannot do this: enumerating events across the 11 calendars took
# 68 seconds, and four named calendars still took 20. EventKit compiled with
# swiftc answers in 0.2s. The channel therefore shells out to a small binary,
# and falls back to interpreting the source when it has not been built yet.


def test_calendar_channel_is_discovered_and_is_read_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import all_channels

    found = all_channels()
    assert "cal" in found
    assert found["cal"].sendable is False


def test_calendar_prefers_the_compiled_helper(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import calendar as cal_mod

    binary = tmp_path / "brain-calendar"
    binary.write_text("#!/bin/sh\necho hi\n")
    binary.chmod(0o755)
    monkeypatch.setattr(cal_mod, "HELPER_BIN", binary)

    seen: list[list[str]] = []
    monkeypatch.setattr(cal_mod, "run", lambda cmd, timeout=30: (seen.append(cmd), "09:00–10:00  Standup")[1])

    out = cal_mod.Calendar().snapshot()
    assert seen == [[str(binary)]]
    assert "Standup" in out


def test_calendar_falls_back_to_interpreting_the_source(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import calendar as cal_mod

    monkeypatch.setattr(cal_mod, "HELPER_BIN", tmp_path / "does-not-exist")
    seen: list[list[str]] = []
    monkeypatch.setattr(cal_mod, "run", lambda cmd, timeout=30: (seen.append(cmd), "")[1])

    cal_mod.Calendar().snapshot()
    assert seen[0][0] == "swift"
    assert seen[0][1].endswith("calendar-today.swift")


def test_calendar_reports_denied_access_rather_than_pretending_the_day_is_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels import calendar as cal_mod

    monkeypatch.setattr(cal_mod, "run", lambda cmd, timeout=30: "(calendar access not granted)")
    ok, why = cal_mod.Calendar().available()
    assert ok is False
    assert "Calendar" in why


def test_calendar_channel_refuses_to_send(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain.channels.calendar import Calendar
    import pytest as _pytest

    with _pytest.raises(NotImplementedError):
        Calendar().send("someone", "hi")
