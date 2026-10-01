"""brain — always-on inbox agent. CLI surface."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import agent, config, knowledge, notify, queue, service, sync as gitsync, watchdog
from .channels import all_channels, get

app = typer.Typer(
    add_completion=True,
    no_args_is_help=True,
    help="brain — wakes headless Claude when a channel changes, drafts replies, "
         "queues them for approval. Never sends on its own.",
)
console = Console()


@app.callback()
def _bootstrap() -> None:
    config.ensure_dirs()
    config.load_env()


@app.command()
def tick(
    force: bool = typer.Option(False, "--force", "-f", help="Invoke Claude even if nothing changed."),
    dry: bool = typer.Option(False, "--dry", "-n", help="Build the snapshot, but never invoke Claude."),
) -> None:
    """One poll cycle. This is what launchd runs."""
    watchdog.beat()   # a tick that correctly does nothing is still a healthy tick
    if watchdog.watchdog_stale():
        # Nothing watches the watchdog, so the tick does. Mutual, no third daemon.
        notify.send("brain", "the watchdog has stopped running — `brain install`")
        agent.log("watchdog appears dead")
    doc = agent.snapshot()
    if dry:
        console.print(doc)
        console.print(f"\n[dim]fingerprint {agent.fingerprint(doc)[:12]} · not invoking Claude[/dim]")
        return
    if not agent.changed(doc) and not force:
        agent.log("no change, skipping claude")
        console.print("[dim]no change, skipping claude[/dim]")
        watchdog.mark_ok()   # deciding there is nothing to do IS a working tick
        return

    agent.log("change detected, invoking claude")
    out = agent.invoke()
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    agent.log(f"claude: {tail}")
    if tail and not tail.startswith(("claude CLI not found", "claude timed out")):
        watchdog.mark_ok()
    console.print(tail or "[dim](no output)[/dim]")

    if tail and tail != "0 drafts, 0 proposals":
        notify.send("brain", f"{tail} · {queue.count()} queued · run `brain queue`")


@app.command()
def reply(
    dry: bool = typer.Option(False, "--dry", "-n", help="Preview autodraft decisions without writing."),
    limit: int = typer.Option(15, "--limit", help="Max messages per personal inbox."),
) -> None:
    """Mail autodraft via imail at 08:00, 14:00, 19:00. What launchd runs as com.mlubich.brain-reply."""
    import subprocess

    cmd = ["imail", "autodraft", "--limit", str(limit)]
    if dry:
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    except FileNotFoundError:
        console.print("[red]imail not on PATH[/red]")
        raise typer.Exit(1)
    except subprocess.TimeoutExpired:
        agent.log("reply: imail autodraft timed out")
        console.print("[red]imail autodraft timed out[/red]")
        raise typer.Exit(1)
    out = (proc.stdout + proc.stderr).strip()
    agent.log(f"reply: {out.splitlines()[-1] if out else 'empty'}")
    console.print(out or "[dim]no pending messages[/dim]")
    if proc.returncode != 0:
        raise typer.Exit(proc.returncode)


@app.command("queue")
def queue_cmd() -> None:
    """Show pending proposals awaiting approval."""
    items = queue.load()
    if not items:
        console.print("[dim]queue empty[/dim]")
        return
    for i, p in enumerate(items, 1):
        console.print(f"[bold]{i}[/bold] [cyan]{p.ch}[/cyan] -> {p.to}")
        if p.re:
            console.print(f"   [dim]re: {p.re}[/dim]")
        console.print(f"   [green]>>[/green] {p.msg}\n")


@app.command()
def approve(
    index: int = typer.Argument(..., help="Queue number from `brain queue`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Send proposal N for real, then drop it from the queue."""
    try:
        item = queue.peek(index)
    except IndexError as exc:
        raise typer.BadParameter(str(exc)) from None

    console.print(f"[cyan]{item.ch}[/cyan] -> {item.to}\n  {item.msg}\n")
    if not yes and not typer.confirm("send this?"):
        raise typer.Abort()

    channel = get(item.ch)
    if not channel.sendable:
        raise typer.BadParameter(
            f"{item.ch} is not sendable — approve it in its own app "
            "(email drafts live in Mail.app)"
        )
    result = channel.send(item.to, item.msg)
    queue.pop(index)
    agent.log(f"approved {item.ch} -> {item.to}")
    console.print(f"[green]sent[/green] -> {item.to}" + (f"\n{result}" if result else ""))


@app.command()
def drop(index: int = typer.Argument(..., help="Queue number to discard.")) -> None:
    """Discard proposal N without sending."""
    try:
        item = queue.pop(index)
    except IndexError as exc:
        raise typer.BadParameter(str(exc)) from None
    console.print(f"[yellow]dropped[/yellow] {item.ch} -> {item.to}")


@app.command()
def digest() -> None:
    """Generate the end-of-day briefing now."""
    text = agent.digest()
    console.print(text)
    notify.send("daily briefing", "\n".join(text.splitlines()[:3]))


@app.command()
def status() -> None:
    """Where everything stands."""
    live = service.loaded()
    table = Table(show_header=False, box=None)
    table.add_row("last tick", _last_log() or "never")
    table.add_row("queued", str(queue.count()))
    table.add_row("channels", ", ".join(all_channels()))
    table.add_row("launchd", f"{len(live)}/{len(service.ALL)} loaded"
                  + (f" ({', '.join(l.split('.')[-1] for l in live)})" if live else ""))
    table.add_row("heartbeat", watchdog._fmt(watchdog.heartbeat_age()) + " ago")
    ks = knowledge.stats()
    table.add_row("knowledge", f"{ks['notes']} notes, indexed {ks['indexed']}")
    gs = gitsync.status()
    table.add_row("sync", gs.get("repo", "?") if gs.get("repo") == "not initialised"
                  else f"{gs['commits']} commits, ahead {gs['ahead']} behind {gs['behind']}, dirty {gs['dirty']}")
    table.add_row("notify", notify.configured())
    table.add_row("config", str(config.DIR))
    console.print(table)


@app.command()
def doctor() -> None:
    """Check every channel and report what is broken."""
    table = Table("channel", "sendable", "status")
    bad = 0
    for name, ch in all_channels().items():
        ok, why = ch.available()
        bad += not ok
        table.add_row(name, "yes" if ch.sendable else _delivery(ch),
                      f"[green]{why}[/green]" if ok else f"[red]{why}[/red]")
    console.print(table)
    if not config.CONFIG_ENV.exists():
        console.print(f"[yellow]no config.env — run `brain init`[/yellow]")
    raise typer.Exit(1 if bad else 0)


def _delivery(ch) -> str:
    """How an approved item for this channel actually reaches its recipient."""
    if ch.sendable:
        return "brain approve"
    return "Mail.app drafts" if ch.name == "mail" else "read-only"


@app.command()
def channels() -> None:
    """List discovered channels. Add one by dropping a module in brain/channels/."""
    table = Table("name", "label", "sends via")
    for name, ch in all_channels().items():
        table.add_row(name, ch.label,
                      _delivery(ch))
    console.print(table)


def _build_calendar_helper() -> str:
    """Compile the EventKit helper. 0.2s per tick built, ~6s interpreted."""
    import subprocess
    from .channels.calendar import HELPER_BIN, HELPER_SRC

    if not HELPER_SRC.exists():
        return "[yellow]calendar helper source missing — skipping[/yellow]"
    if not shutil.which("swiftc"):
        return "[yellow]swiftc not found — calendar channel will interpret the source[/yellow]"
    HELPER_BIN.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["swiftc", "-O", "-o", str(HELPER_BIN), str(HELPER_SRC)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"[yellow]calendar helper did not build: {proc.stderr.strip()[:120]}[/yellow]"
    return f"[dim]built calendar helper -> {HELPER_BIN}[/dim]"


@app.command()
def install(
    poll: int = typer.Option(config.POLL_SECONDS, help="Seconds between ticks."),
    hour: int = typer.Option(config.DIGEST_HOUR, help="Hour (0-23) for the daily briefing."),
    knowledge_remote: str = typer.Option("", "--knowledge-remote",
                                         help="Git URL for the shared knowledge repo."),
) -> None:
    """Set up everything and start it: config, knowledge store, launchd agents."""
    init(quiet=True)
    if not gitsync.is_repo():
        for line in gitsync.init(knowledge_remote):
            console.print(f"[dim]{line}[/dim]")
    elif knowledge_remote:
        for line in gitsync.init(knowledge_remote):
            console.print(f"[dim]{line}[/dim]")
    knowledge.reindex()
    console.print(_build_calendar_helper())
    for line in service.install(poll=poll, hour=hour):
        console.print(line)
    console.print(f"\n[green]brain is live[/green] — tick every {poll}s, "
                  f"reply 08:00/14:00/19:00, briefing at {hour:02d}:00, watchdog and sync running")
    console.print("[dim]next: `brain health`[/dim]")


@app.command()
def uninstall() -> None:
    """Unload and remove the launchd agents. Leaves config and queue alone."""
    for line in service.uninstall():
        console.print(line)


@app.command()
def init(quiet: bool = typer.Option(False, "--quiet", "-q")) -> None:
    """Create ~/.config/brain and the config template."""
    config.ensure_dirs()
    if not config.CONFIG_ENV.exists():
        config.CONFIG_ENV.write_text(config.CONFIG_TEMPLATE)
        if not quiet:
            console.print(f"[green]wrote[/green] {config.CONFIG_ENV}")
    elif not quiet:
        console.print(f"[dim]{config.CONFIG_ENV} already exists[/dim]")


@app.command("log")
def log_cmd(n: int = typer.Argument(40, help="Lines to show.")) -> None:
    """Tail the activity log."""
    if not config.LOG.exists():
        console.print("[dim]no log yet[/dim]")
        return
    console.print("\n".join(config.LOG.read_text().splitlines()[-n:]))


def _last_log() -> str:
    if not config.LOG.exists():
        return ""
    lines = [x for x in config.LOG.read_text().splitlines() if x.strip()]
    return lines[-1] if lines else ""


if __name__ == "__main__":
    app()


# ---------------------------------------------------------------- knowledge

@app.command()
def learn(
    text: str = typer.Argument(..., help="The fact to remember."),
    title: str = typer.Option("", "--title", "-t", help="Note title. Defaults to the first words."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    append: bool = typer.Option(False, "--append", "-a", help="Append to an existing note of the same title."),
    force: bool = typer.Option(False, "--force", help="Write even if a similar note already exists."),
    push: bool = typer.Option(False, "--push", "-p", help="Sync to the remote straight after."),
) -> None:
    """Record something worth keeping.

    Refuses when an existing note already says this — every real duplicate is a
    reword, so the check is fuzzy, not a hash. Pass --force to write anyway.
    """
    path, action = knowledge.learn(
        text, title=title,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        append=append, force=force,
    )
    colour = {"created": "green", "appended": "cyan", "updated": "cyan",
              "duplicate": "yellow", "similar": "yellow"}[action]
    console.print(f"[{colour}]{action}[/{colour}] {path}")
    if action == "similar":
        console.print(f"[dim]already covered there. Edit it with "
                      f"`brain learn ... --title \"{knowledge.parse(path).title}\" --append`, "
                      f"or re-run with --force.[/dim]")
        return
    if push:
        for line in gitsync.sync(f"learn: {title or text[:50]}"):
            console.print(f"  {line}")


@app.command()
def recall(
    query: str = typer.Argument(..., help="Full-text search over everything learned."),
    limit: int = typer.Option(10, "--limit", "-n"),
    sessions: bool = typer.Option(False, "--sessions", "-s", help="Also search multi-agent session logs (AGY, Codex, Claude, etc)."),
    json_out: bool = typer.Option(False, "--json", help="Output results as JSON for agents."),
) -> None:
    """Search the knowledge index."""
    import json as _json

    rows = knowledge.recall(query, limit)
    sess_matches = []
    if sessions:
        from . import sessions as _sess
        sess_matches = _sess.search_sessions(query, limit=limit)

    if json_out:
        if sessions:
            results = {
                "notes": [
                    {
                        "title": title,
                        "snippet": snippet,
                        "path": str(path),
                        "slug": Path(path).stem,
                    }
                    for title, snippet, path in rows
                ],
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "session_type": s.session_type,
                        "snippet": s.snippet,
                        "path": str(s.path),
                    }
                    for s in sess_matches
                ],
            }
            print(_json.dumps(results, indent=2))
        else:
            results = [
                {
                    "title": title,
                    "snippet": snippet,
                    "path": str(path),
                    "slug": Path(path).stem,
                }
                for title, snippet, path in rows
            ]
            print(_json.dumps(results, indent=2))
        return

    if not rows and not sess_matches:
        console.print(f"[dim]nothing for {query!r} ({knowledge.stats()['notes']} notes indexed)[/dim]")
        return

    for title, snippet, path in rows:
        console.print(f"[bold]{title}[/bold]\n  {snippet}\n  [dim]{path}[/dim]\n")

    if sess_matches:
        console.print(f"\n[bold cyan]── Sessions ({len(sess_matches)}) ──[/bold cyan]")
        for s in sess_matches:
            console.print(f"[{s.session_type.upper()}] [bold]{s.session_id}[/bold]\n  {s.snippet}\n  [dim]{s.path}[/dim]\n")


@app.command(name="sessions")
def search_sessions_cmd(
    query: str = typer.Argument(..., help="Search query across all agent session transcripts."),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Search multi-agent session transcripts (Antigravity, Codex, Claude, etc.)."""
    import json as _json
    from . import sessions as _sess

    matches = _sess.search_sessions(query, limit=limit)
    if json_out:
        print(_json.dumps([
            {
                "session_id": s.session_id,
                "session_type": s.session_type,
                "snippet": s.snippet,
                "path": str(s.path),
            }
            for s in matches
        ], indent=2))
        return

    if not matches:
        console.print(f"[dim]no session transcripts matched {query!r}[/dim]")
        return

    for s in matches:
        console.print(f"[{s.session_type.upper()}] [bold]{s.session_id}[/bold]\n  {s.snippet}\n  [dim]{s.path}[/dim]\n")


@app.command()
def show(
    slug_or_title: str = typer.Argument(..., help="Slug or title of the note to inspect."),
    json_out: bool = typer.Option(False, "--json", help="Output note details as JSON for agents."),
) -> None:
    """Display a note's full content and metadata."""
    import json as _json

    note = knowledge.get_note(slug_or_title)
    if not note:
        console.print(f"[red]note not found:[/red] {slug_or_title}")
        raise typer.Exit(1)

    if json_out:
        print(_json.dumps(note.to_dict(), indent=2))
        return

    console.print(f"[bold cyan]{note.title}[/bold cyan]")
    if note.tags:
        console.print(f"[dim]tags: {', '.join(note.tags)}[/dim]")
    if note.updated:
        console.print(f"[dim]updated: {note.updated}[/dim]")
    console.print(f"[dim]path: {note.path}[/dim]\n")
    console.print(note.body)


@app.command("list")
def list_cmd(
    tag: str = typer.Option("", "--tag", "-t", help="Filter by tag."),
    limit: int = typer.Option(100, "--limit", "-n", help="Max notes to list."),
    json_out: bool = typer.Option(False, "--json", help="Output notes as JSON."),
) -> None:
    """List notes in the knowledge base."""
    import json as _json

    notes = knowledge.all_notes()
    if tag:
        t_clean = tag.strip().lower()
        notes = [n for n in notes if t_clean in [x.lower() for x in n.tags]]
    notes = notes[:limit]

    if json_out:
        print(_json.dumps([n.to_dict() for n in notes], indent=2))
        return

    if not notes:
        console.print("[dim]no notes found[/dim]")
        return

    table = Table("slug", "title", "tags", "updated")
    for n in notes:
        table.add_row(n.slug, n.title, ", ".join(n.tags), n.updated or "-")
    console.print(table)


@app.command()
def reindex() -> None:
    """Rebuild the search index from the markdown. Safe — notes are untouched."""
    count = knowledge.reindex()
    semantic = knowledge.stats()["semantic"]
    console.print(f"indexed [green]{count}[/green] notes · semantic {semantic}")


@app.command("sync")
def sync_cmd(
    message: str = typer.Option("", "--message", "-m", help="Commit message."),
) -> None:
    """Pull, commit, push the knowledge repo. Never force-pushes, never discards."""
    for line in gitsync.sync(message):
        style = "red" if ("CONFLICT" in line or "failed" in line) else "dim"
        console.print(f"[{style}]{line}[/{style}]")


@app.command("knowledge")
def knowledge_cmd(
    init_remote: str = typer.Option("", "--init", help="Create the repo, optionally with this git remote URL."),
) -> None:
    """Show or initialise the knowledge store."""
    if init_remote or not gitsync.is_repo():
        for line in gitsync.init(init_remote if init_remote != "-" else ""):
            console.print(line)
        knowledge.reindex()
    table = Table(show_header=False, box=None)
    for key, value in {**knowledge.stats(), **gitsync.status()}.items():
        table.add_row(key, str(value))
    console.print(table)


# ---------------------------------------------------------------- resilience

@app.command("watchdog")
def watchdog_cmd(
    check_only: bool = typer.Option(False, "--check", "-c", help="Report only; change nothing."),
    reset: bool = typer.Option(False, "--reset", help="Clear the circuit breaker."),
) -> None:
    """Detect a stalled agent and recover it. Non-destructive at every step."""
    if reset:
        watchdog.reset_breaker()
        console.print("[green]breaker reset[/green]")
        return
    if check_only:
        for key, value in watchdog.check().items():
            console.print(f"{key}: {value}")
        return
    for line in watchdog.run():
        style = "green" if line.startswith("healthy") else "yellow"
        console.print(f"[{style}]{line}[/{style}]")


@app.command()
def health() -> None:
    """Whole-system view: jobs, heartbeat, channels, knowledge, breaker."""
    state = watchdog.check()
    table = Table("check", "state", "detail")

    def row(name, ok, detail):
        table.add_row(name, "[green]ok[/green]" if ok else "[red]FAIL[/red]", detail)

    live = service.loaded()
    row("launchd jobs", not state["jobs_missing"],
        f"{len(live)}/{len(service.ALL)} loaded" +
        (f" · missing {', '.join(state['jobs_missing'])}" if state["jobs_missing"] else ""))
    row("heartbeat", not state["stale"],
        f"{watchdog._fmt(state['heartbeat_age'])} ago (stale after {state['stale_after']}s)")
    row("hung child", not state["hung_child"],
        str(state["hung_child"]) if state["hung_child"] else "none")
    row("breaker", not state["breaker_open"],
        f"{state['attempts']} attempts in window" + (" — OPEN" if state["breaker_open"] else ""))
    # Liveness and usefulness are different questions; a job can run forever
    # while achieving nothing, and the heartbeat alone would call that healthy.
    productive = state["productive_age"]
    row("last productive run", productive is not None and productive < state["stale_after"] * 4,
        watchdog._fmt(productive) + " ago" if productive is not None else "never")
    row("watchdog alive", not watchdog.watchdog_stale(),
        watchdog._fmt(state["watchdog_age"]) + " ago" if state["watchdog_age"] else "never")
    for name, ch in all_channels().items():
        ok, why = ch.available()
        row(f"channel {name}", ok, why)
    ks = knowledge.stats()
    row("knowledge", True, f"{ks['notes']} notes · indexed {ks['indexed']}")
    gs = gitsync.status()
    row("knowledge sync", gs.get("repo") != "not initialised",
        f"ahead {gs.get('ahead','?')} behind {gs.get('behind','?')} dirty {gs.get('dirty','?')}"
        if gs.get("repo") != "not initialised" else "not initialised")
    console.print(table)


@app.command()
def tags() -> None:
    """Tags already in use. Reuse one before inventing a new one."""
    counts = knowledge.all_tags()
    if not counts:
        console.print("[dim]no tags yet[/dim]")
        return
    table = Table("tag", "notes")
    for tag, count in counts.items():
        table.add_row(tag, str(count))
    console.print(table)
