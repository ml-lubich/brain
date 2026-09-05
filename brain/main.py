"""brain — always-on inbox agent. CLI surface."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import agent, config, notify, queue, service
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
    table.add_row("launchd", f"{len(live)}/2 loaded" + (f" ({', '.join(live)})" if live else ""))
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


@app.command()
def channels() -> None:
    """List discovered channels. Add one by dropping a module in brain/channels/."""
    table = Table("name", "label", "sends via")
    for name, ch in all_channels().items():
        table.add_row(name, ch.label,
                      "brain approve" if ch.sendable else "Mail.app drafts")
    console.print(table)


@app.command()
def install(
    poll: int = typer.Option(config.POLL_SECONDS, help="Seconds between ticks."),
    hour: int = typer.Option(config.DIGEST_HOUR, help="Hour (0-23) for the daily briefing."),
) -> None:
    """Write and load the launchd agents. Idempotent."""
    init(quiet=True)
    for line in service.install(poll=poll, hour=hour):
        console.print(line)
    console.print(f"\n[green]brain is live[/green] — every {poll}s, briefing at {hour:02d}:00")


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
