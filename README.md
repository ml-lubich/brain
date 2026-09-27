# brain

Always-on inbox agent for macOS. A timer notices your channels changed, wakes a
headless Claude, and it drafts replies. You approve. Headless Claude cannot send.
Hourly `brain reply` runs `imail autodraft` on the three personal walls.

```
launchd (every 10 min)
  └─ brain tick
       ├─ snapshot every channel   -> ~/.config/brain/context.md
       ├─ fingerprint vs last run  -> unchanged? exit. zero tokens spent.
       └─ changed? claude -p (send tools removed)
            ├─ email  -> real unsent Mail.app draft
            ├─ imsg   -> proposal in the approval queue
            ├─ wa     -> proposal in the approval queue
            └─ projects -> updates your TODO in place
```

## Why it works this way

**Claude Code hooks cannot fire on incoming mail.** Hooks are session-lifecycle
only — there is no `OnEmailArrives`. So the heartbeat lives in launchd and Claude
is the worker it wakes.

**Approval for email is Mail.app's own draft folder.** Drafts sit unsent, sync to
your phone, you hit send. There is no dashboard here and that is deliberate.

**The agent physically cannot send.** `imail send`, `imsg send`, and `wa send` are
in `--disallowedTools` and absent from `--allowedTools`. A test asserts this.

## Install

```bash
uv tool install mac-brain     # or: uv tool install -e ~/dev/brain
brain init                    # writes ~/.config/brain/config.env
brain doctor                  # check every channel
brain install                 # write + load the launchd agents
```

`brain install --poll 300 --hour 8` to tick every 5 minutes and brief at 08:00.

## Commands

| Command | Does |
|---|---|
| `brain tick` | One poll cycle. What launchd runs. `--dry` builds the snapshot without calling Claude, `--force` calls it anyway. |
| `brain reply` | Hourly mail autodraft via `imail autodraft`. `--dry` previews. Personal accounts only. |
| `brain queue` | Pending proposals, numbered. |
| `brain approve N` | Send queued iMessage/WhatsApp. Email approval is Mail.app drafts; autodraft auto-send is only for known low-stakes follow-ups. |
| `brain drop N` | Discard proposal N. |
| `brain digest` | End-of-day briefing now. |
| `brain status` | Last tick, queue depth, launchd state, notifications. |
| `brain doctor` | Per-channel health. Exits non-zero if anything is broken. |
| `brain channels` | What is discovered and how each one gets approved. |
| `brain install` / `uninstall` | Manage the launchd agents. |
| `brain log [n]` | Tail the activity log. |

## Recall

`brain recall` is hybrid search.

- **Lexical.** SQLite FTS5 with the Porter stemmer (`ready`/`readiness`, `apply`/`applying`). Proper-name aliases expand too (`auto-apply` / `auto-lazyapply` / `job-autofill`, `workday` / `my-experience`), because those are renames, not paraphrases. Every distinctive word has to match.
- **Semantic.** Cosine similarity over one vector per note. The model is `mlx-community/bge-small-en-v1.5-4bit` via `mlx-embeddings` (`uv tool install -e '.[embed]'`). `BRAIN_EMBED=0` turns it off. Agents ask with words that are not in the note; that is what this channel is for. There is no hand-maintained thesaurus of those words. Hits have to clear a floor and sit near the best match, so a vaguely similar note does not fill the list.
- **Fusion.** Reciprocal rank fusion of the two lists. A flat OR of the original words runs only when both channels miss, so a note that merely shares one word does not outrank the paraphrase.

`brain reindex` rebuilds both indexes from the markdown. `brain learn` updates them. A model failure is recorded in `brain knowledge` under `semantic` and lexical search keeps working.

## Adding a channel

Drop one module in `brain/channels/`. Discovery is automatic — no registry to
edit, no import to add.

```python
from .base import Channel, run

class Slack(Channel):
    name = "slack"
    label = "Slack (recent DMs)"
    binary = "slack"

    def snapshot(self) -> str:
        return run(["slack", "unreads", "--json"])

    def send(self, to: str, message: str) -> str:
        return run(["slack", "send", to, message])
```

Set `sendable = False` when approval happens in the channel's own app rather than
through `brain approve` (that is what `mail` does).

## Remote control

Full control from a phone: Tailscale + `ssh you@host` + `cmux attach` from Blink
or Termius. iOS dictation is your voice input. No app to build.

Light control: put a Telegram bot token in `~/.config/brain/config.env` and
notifications reach your phone.

## Config

| Path | Is |
|---|---|
| `~/.config/brain/config.env` | Telegram token + chat id. Secrets. |
| `~/.config/brain/queue.jsonl` | Pending proposals, one JSON object per line. |
| `~/.config/brain/context.md` | Latest snapshot the agent reads. |
| `~/.config/brain/state` | Last fingerprint. The token-saving gate. |
| `~/.config/brain/brain.log` | Activity log. |

Env overrides: `BRAIN_HOME`, `BRAIN_POLL_SECONDS`, `BRAIN_DIGEST_HOUR`,
`BRAIN_FROM`, `AGENT_TODO`, `WA_REPO`.

## Tests

```bash
uv run --with pytest --with typer --with rich python -m pytest tests/ -q
```

## Skills

Reusable agent playbooks live in [`skills/`](skills/README.md) — the same launchd-wakes-a-
headless-Claude pattern this repo runs on, generalised for other long unattended jobs.

## License

MIT
