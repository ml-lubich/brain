"""Git sync for the knowledge repo. Non-destructive by construction.

There is no code path here that deletes a note, discards a local change, or
force-pushes. When histories genuinely diverge the rebase is aborted, the local
tree is left exactly as it was, and the caller is told to look. A sync tool that
can lose work is worse than no sync tool.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config

GITIGNORE = """\
# The search index is derived from notes/ and rebuilt by `brain reindex`.
*.db
*.db-shm
*.db-wal
.DS_Store
"""

README = """\
# brain-knowledge

Notes written by the `brain` agent and by Claude Code sessions on this machine.

- `notes/*.md` is the source of truth. Mergeable text, one topic per file.
- The SQLite FTS5 index lives outside this repo and is rebuilt with `brain reindex`.

Write with `brain learn`, read with `brain recall`, move with `brain sync`.
Nothing in this repo is ever deleted automatically.
"""


def git(*args: str, cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd or config.KNOWLEDGE),
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_repo() -> bool:
    return (config.KNOWLEDGE / ".git").is_dir()


def has_remote() -> bool:
    code, out = git("remote")
    return code == 0 and bool(out.strip())


def init(remote: str = "") -> list[str]:
    """Create the knowledge repo locally. Idempotent, never destructive."""
    steps = []
    (config.KNOWLEDGE / "notes").mkdir(parents=True, exist_ok=True)

    gitignore = config.KNOWLEDGE / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE)
    readme = config.KNOWLEDGE / "README.md"
    if not readme.exists():
        readme.write_text(README)

    if not is_repo():
        git("init", "-q")
        git("checkout", "-q", "-B", "main")
        steps.append(f"initialised {config.KNOWLEDGE}")
    else:
        steps.append(f"already a repo: {config.KNOWLEDGE}")

    if remote:
        code, _ = git("remote", "get-url", "origin")
        git("remote", "set-url" if code == 0 else "add", "origin", remote)
        steps.append(f"origin -> {remote}")

    return steps


def _identity() -> list[str]:
    return ["-c", "user.name=brain", "-c", "user.email=brain@localhost"]


def dirty() -> bool:
    code, out = git("status", "--porcelain")
    return code == 0 and bool(out.strip())


def commit(message: str = "") -> str:
    if not dirty():
        return "nothing to commit"
    git("add", "-A")
    count = len([l for l in git("diff", "--cached", "--name-only")[1].splitlines() if l])
    message = message or f"knowledge: {count} file(s) updated"
    code, out = git(*_identity(), "commit", "-q", "-m", message)
    return f"committed {count} file(s)" if code == 0 else f"commit failed: {out}"


def pull() -> str:
    """Rebase local commits on top of the remote.

    On conflict: abort and leave everything untouched. We would rather stop and
    say so than resolve someone's notes by guessing.
    """
    if not has_remote():
        return "no remote configured"
    code, out = git("fetch", "origin", timeout=180)
    if code != 0:
        return f"fetch failed: {out.splitlines()[-1] if out else 'unknown'}"

    code, _ = git("rev-parse", "--verify", "origin/main")
    if code != 0:
        return "remote has no main yet"

    code, out = git(*_identity(), "rebase", "origin/main")
    if code != 0:
        git("rebase", "--abort")  # restores the pre-rebase tree exactly
        return "CONFLICT — rebase aborted, nothing changed. Resolve by hand in " \
               f"{config.KNOWLEDGE}"
    return "pulled"


def push() -> str:
    if not has_remote():
        return "no remote configured"
    # Plain push. Never --force, never --force-with-lease: a rejected push means
    # someone else has work we have not seen, and the fix is to pull, not to win.
    code, out = git("push", "origin", "main", timeout=180)
    if code != 0:
        if "rejected" in out or "non-fast-forward" in out:
            return "push rejected — run `brain sync` again to pull first"
        return f"push failed: {out.splitlines()[-1] if out else 'unknown'}"
    return "pushed"


def sync(message: str = "") -> list[str]:
    """pull -> commit -> push, in the order that cannot lose work."""
    if not is_repo():
        return ["no knowledge repo — run `brain knowledge init`"]
    steps = [commit(message), pull()]
    if steps[-1].startswith("CONFLICT"):
        return steps  # stop; do not push over a conflict
    if dirty():  # the rebase may have surfaced new files to record
        steps.append(commit(message))
    steps.append(push())
    return steps


def status() -> dict[str, str]:
    if not is_repo():
        return {"repo": "not initialised"}
    code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        branch = "main"
    code, count = git("rev-list", "--count", "HEAD")
    if code != 0:
        count = "0"          # a fresh repo with no commits is not an error
    code, remote = git("remote", "get-url", "origin")
    if code != 0:
        remote = ""
    ahead = behind = "?"
    if has_remote():
        code, out = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
        if code == 0 and out:
            parts = out.split()
            if len(parts) == 2:
                behind, ahead = parts
    return {
        "repo": str(config.KNOWLEDGE),
        "branch": branch or "?",
        "commits": count or "0",
        "remote": remote or "none",
        "ahead": ahead,
        "behind": behind,
        "dirty": "yes" if dirty() else "no",
    }
