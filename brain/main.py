"""brain — always-on inbox agent. CLI surface."""

from __future__ import annotations

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
    doc = agent.snapshot()
    if dry:
        console.print(doc)
        console.print(f"\n[dim]fingerprint {agent.fingerprint(doc)[:12]} · not invoking Claude[/dim]")
        return
    if not agent.changed(doc) and not force:
        agent.log("no change, skipping claude")
        console.print("[dim]no change, skipping claude[/dim]")
        return

    agent.log("change detected, invoking claude")
    out = agent.invoke()
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    agent.log(f"claude: {tail}")
    console.print(tail or "[dim](no output)[/dim]")

    if tail and tail != "0 drafts, 0 proposals":
        notify.send("brain", f"{tail} · {queue.count()} queued · run `brain queue`")


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
        table.add_row(name, "yes" if ch.sendable else "draft-only",
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
    for line in service.install(poll=poll, hour=hour):
        console.print(line)
    console.print(f"\n[green]brain is live[/green] — tick every {poll}s, "
                  f"briefing at {hour:02d}:00, watchdog and sync running")
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
    push: bool = typer.Option(False, "--push", "-p", help="Sync to the remote straight after."),
) -> None:
    """Record something worth keeping. Deduplicates; never overwrites a note."""
    path, action = knowledge.learn(
        text, title=title,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        append=append,
    )
    colour = {"created": "green", "appended": "cyan", "duplicate": "yellow"}[action]
    console.print(f"[{colour}]{action}[/{colour}] {path}")
    if push:
        for line in gitsync.sync(f"learn: {title or text[:50]}"):
            console.print(f"  {line}")


@app.command()
def recall(
    query: str = typer.Argument(..., help="Full-text search over everything learned."),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Search the knowledge index."""
    rows = knowledge.recall(query, limit)
    if not rows:
        console.print(f"[dim]nothing for {query!r} ({knowledge.stats()['notes']} notes indexed)[/dim]")
        return
    for title, snippet, path in rows:
        console.print(f"[bold]{title}[/bold]\n  {snippet}\n  [dim]{path}[/dim]\n")


@app.command()
def reindex() -> None:
    """Rebuild the search index from the markdown. Safe — notes are untouched."""
    console.print(f"indexed [green]{knowledge.reindex()}[/green] notes")


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
