# skills

Reusable agent playbooks and the small tools they invoke. Each skill is a directory:

```
skills/<slug>/SKILL.md      # the playbook an agent loads (frontmatter: name, description)
skills/<slug>/tools/        # scripts the playbook invokes
```

## Publishing policy

This repo is **private** — the playbooks reference internal projects. Anything genuinely
reusable and free of internal detail can be split into its **own public repo** and pulled
back here as a git submodule:

```bash
git submodule add https://github.com/ml-lubich/<public-tool> skills/<slug>/vendor/<public-tool>
```

That shares individual tools without making the private material public. Never invert it —
do not make this repo public and carve out exceptions.

## Index

| Skill | What it solves |
|-------|----------------|
| [claude-code-overnight-continuity](claude-code-overnight-continuity/SKILL.md) | Keep a long unattended Claude Code job running across the 5-hour usage-limit reset, a crashed child, and a wedged-but-alive process |
