"""Knowledge store, sync safety, and watchdog safety.

The tests that matter most here are the ones asserting what CANNOT happen:
notes are never clobbered, sync never force-pushes, and the watchdog can never
signal an interactive claude session.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    monkeypatch.setenv("BRAIN_KNOWLEDGE", str(tmp_path / "knowledge"))
    for mod in [m for m in list(sys.modules) if m.startswith("brain")]:
        del sys.modules[mod]
    from brain import config
    config.ensure_dirs()
    return config


# ------------------------------------------------------------------ knowledge

def test_learn_then_recall_finds_it(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn("Vercel autodeploy is broken, ship with vercel --prod --yes",
                    title="Vercel deploys", tags=["vercel"])
    hits = knowledge.recall("vercel")
    assert hits and "Vercel deploys" == hits[0][0]


def test_identical_content_is_a_duplicate_not_a_new_note(tmp_path, monkeypatch):
    """An agent ticking every 10 minutes would otherwise re-learn forever."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    text = "The booking link is mishalubich.com"
    _, first = knowledge.learn(text, title="Booking link")
    _, second = knowledge.learn(text, title="Booking link")
    assert (first, second) == ("created", "duplicate")
    assert len(knowledge.all_notes()) == 1


def test_without_git_history_a_same_title_write_forks_instead_of_overwriting(
        tmp_path, monkeypatch):
    """Edit-in-place is only safe because git holds the previous version. With
    no repo there is no archive, so the old text must never be overwritten."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge, sync
    assert not sync.is_repo()             # no archive available
    p1, _ = knowledge.learn("first version of the fact", title="Topic")
    p2, action = knowledge.learn("a genuinely different claim about lume vms",
                                 title="Topic", force=True)
    assert action == "created"
    assert p1 != p2 and p1.exists() and p2.exists()
    assert "first version" in p1.read_text()


def test_append_extends_rather_than_replaces(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn("original line", title="Notes")
    path, action = knowledge.learn("added line", title="Notes", append=True)
    body = path.read_text()
    assert action == "appended"
    assert "original line" in body and "added line" in body


def test_reindex_rebuilds_from_markdown_only(tmp_path, monkeypatch):
    """The index is derived. Deleting it must lose nothing."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn("the ClickUp API rate limit is account-wide", title="One")
    knowledge.learn("Lume VMs expose a daemon on port 7777", title="Two")
    knowledge.db_path().unlink()
    assert knowledge.reindex() == 2
    assert knowledge.recall("clickup")
    assert knowledge.recall("lume")


def test_frontmatter_roundtrips(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    path, _ = knowledge.learn("body text here", title="My Title", tags=["a", "b"])
    note = knowledge.parse(path)
    assert note.title == "My Title" and note.tags == ["a", "b"]
    assert note.body == "body text here"


# ----------------------------------------------------------------- sync safety

def _git_call_args(module):
    """Every literal argument passed to git(...) anywhere in the module.

    Parsed from the AST rather than grepped from the text, so a comment saying
    "never --force" cannot pass or fail the test on its own.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "git":
            args += [a.value for a in node.args if isinstance(a, ast.Constant)]
    return args


def test_sync_never_force_pushes(tmp_path, monkeypatch):
    """A sync tool that can overwrite the remote can lose another machine's notes."""
    _isolate(tmp_path, monkeypatch)
    from brain import sync
    args = _git_call_args(sync)
    assert args, "expected to find git() calls to inspect"
    assert not [a for a in args if "force" in a]
    assert "--hard" not in args
    assert "clean" not in args


def test_sync_has_no_delete_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import inspect
    from brain import sync, knowledge
    for module in (sync, knowledge):
        source = inspect.getsource(module)
        assert "rm -rf" not in source
        assert "shutil.rmtree" not in source


def test_init_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import sync
    sync.init()
    (sync.config.KNOWLEDGE / "notes" / "keep.md").write_text("do not lose me")
    sync.init()   # second run must not wipe anything
    assert (sync.config.KNOWLEDGE / "notes" / "keep.md").read_text() == "do not lose me"


# ------------------------------------------------------------- watchdog safety

def test_watchdog_ignores_a_pid_that_is_not_headless_claude(tmp_path, monkeypatch):
    """THE critical guard. This Mac runs many interactive `claude` sessions; the
    watchdog must only ever target the headless child a tick recorded."""
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog
    watchdog.record_child(os.getpid())          # a python process, not claude -p
    # backdate it well past the hung threshold
    watchdog._path(watchdog.PIDFILE).write_text(
        '{"pid": %d, "started": %d}' % (os.getpid(), int(time.time()) - 99999)
    )
    assert watchdog.hung_child() is None


def test_watchdog_ignores_a_child_that_is_not_old_yet(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog
    watchdog.record_child(os.getpid())
    assert watchdog.hung_child() is None


def test_heartbeat_staleness(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog, config
    assert watchdog.heartbeat_age() is None      # never run
    watchdog.beat()
    assert watchdog.heartbeat_age() < 5
    config.HEARTBEAT.write_text(str(int(time.time()) - 99999))
    assert watchdog.heartbeat_age() > watchdog.stale_after()


def test_breaker_opens_and_resets(tmp_path, monkeypatch):
    """Stops a permanently broken job from being restarted forever."""
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog
    assert watchdog.breaker_state() == (False, 0)
    for _ in range(watchdog.MAX_ATTEMPTS):
        watchdog.record_attempt()
    is_open, count = watchdog.breaker_state()
    assert is_open and count == watchdog.MAX_ATTEMPTS
    watchdog.reset_breaker()
    assert watchdog.breaker_state() == (False, 0)


def test_breaker_forgets_attempts_outside_the_window(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog
    import json
    old = time.time() - watchdog.BREAKER_WINDOW - 60
    watchdog._path(watchdog.BREAKER).write_text(
        json.dumps({"attempts": [old] * 10, "opened": None})
    )
    assert watchdog.breaker_state() == (False, 0)


def test_watchdog_check_never_mutates(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog, config
    watchdog.beat()
    before = config.HEARTBEAT.read_text()
    watchdog.check()
    assert config.HEARTBEAT.read_text() == before


def test_overnight_sleep_is_not_treated_as_a_stall(tmp_path, monkeypatch):
    """launchd suspends StartInterval jobs while the Mac sleeps, so every
    morning the heartbeat looks hours old. Recovering from that would burn a
    breaker attempt daily for a system that is working perfectly."""
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog, config
    watchdog.mark_run()
    config.HEARTBEAT.write_text(str(int(time.time()) - 8 * 3600))

    # watchdog also last ran 8h ago -> the machine was asleep, not stuck
    watchdog._path(watchdog.LASTRUN).write_text(str(int(time.time()) - 8 * 3600))
    assert watchdog.slept() is True

    # watchdog ran 30s ago while the tick did not -> a genuine stall
    watchdog._path(watchdog.LASTRUN).write_text(str(int(time.time()) - 30))
    assert watchdog.slept() is False


def test_sleep_path_records_no_breaker_attempt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import watchdog, config, service
    # Pretend every job is loaded, so the test does not depend on whether this
    # particular machine happens to have them installed.
    monkeypatch.setattr(service, "loaded", lambda: list(service.ALL))
    config.HEARTBEAT.write_text(str(int(time.time()) - 8 * 3600))
    watchdog._path(watchdog.LASTRUN).write_text(str(int(time.time()) - 8 * 3600))
    actions = watchdog.run()
    assert any("resumed after" in a for a in actions)
    assert watchdog.breaker_state() == (False, 0)   # nothing spent


# --------------------------------------------- dedup calibration (measured)

VERCEL_ORIGINAL = ("Vercel auto-deploy from GitHub pushes has been broken since "
                   "2026-07-25. Ship with 'vercel --prod --yes' and verify prod "
                   "with a marker string.")
VERCEL_REWORD = ("Vercel autodeploy from github pushes stopped working back in "
                 "July 2026; you have to ship using vercel --prod --yes and "
                 "check prod with a marker")
UNRELATED = ("ClickUp rate limits are account-wide and burst sensitive; back "
             "off globally rather than per-list.")


def test_a_reworded_duplicate_is_refused(tmp_path, monkeypatch):
    """The real duplicate is always a reword — byte-hashing never sees it."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn(VERCEL_ORIGINAL, title="Vercel autodeploy broken")
    path, action = knowledge.learn(VERCEL_REWORD, title="Vercel deploy notes")
    assert action == "similar"
    assert path.name == "vercel-autodeploy-broken.md"
    assert len(knowledge.all_notes()) == 1


def test_an_unrelated_note_is_still_accepted(tmp_path, monkeypatch):
    """The dedup must not become a wall that refuses genuinely new facts."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn(VERCEL_ORIGINAL, title="Vercel autodeploy broken")
    _, action = knowledge.learn(UNRELATED, title="ClickUp rate limits")
    assert action == "created"
    assert len(knowledge.all_notes()) == 2


def test_force_overrides_the_similarity_refusal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn(VERCEL_ORIGINAL, title="Vercel autodeploy broken")
    _, action = knowledge.learn(VERCEL_REWORD, title="Vercel deploy notes", force=True)
    assert action == "created"


def test_thresholds_separate_the_measured_cases(tmp_path, monkeypatch):
    """Guards the calibration itself: the reword must score above the cut and
    unrelated notes below it, with margin on both sides."""
    _isolate(tmp_path, monkeypatch)
    import difflib
    from brain import knowledge
    ratio = difflib.SequenceMatcher(
        None, knowledge._normalise(VERCEL_ORIGINAL), knowledge._normalise(VERCEL_REWORD)
    ).ratio()
    unrelated = difflib.SequenceMatcher(
        None, knowledge._normalise(VERCEL_ORIGINAL), knowledge._normalise(UNRELATED)
    ).ratio()
    assert ratio > knowledge.SIMILAR_RATIO > unrelated
    assert knowledge._overlap(VERCEL_ORIGINAL, VERCEL_REWORD) >= knowledge.SIMILAR_OVERLAP
    assert knowledge._overlap(VERCEL_ORIGINAL, UNRELATED) < knowledge.SIMILAR_OVERLAP


def test_operator_punctuation_does_not_break_search(tmp_path, monkeypatch):
    """A term like `--prod` is FTS5's NOT operator; unquoted it errors the whole
    query to zero rows, which is what makes an agent think nothing is known."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge
    knowledge.learn(VERCEL_ORIGINAL, title="Vercel autodeploy broken")
    assert "--prod" not in knowledge.terms(VERCEL_ORIGINAL)
    assert knowledge.recall("ship with vercel --prod --yes")


def test_same_title_edits_in_place_rather_than_forking(tmp_path, monkeypatch):
    """Two contradictory notes with no way to tell which is current is the
    failure this store exists to prevent. Git history is the archive."""
    _isolate(tmp_path, monkeypatch)
    from brain import knowledge, sync
    sync.init()                       # history exists, so overwriting is safe
    p1, _ = knowledge.learn("the original claim", title="Topic")
    p2, action = knowledge.learn("a completely different claim about lume vms",
                                 title="Topic", force=True)
    assert action == "updated"
    assert p1 == p2
    assert len(knowledge.all_notes()) == 1
    assert "completely different" in p2.read_text()
