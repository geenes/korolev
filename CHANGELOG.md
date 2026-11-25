# Changelog

All notable changes to Korolev.

## v0.1.0 (2026-11-25)

First public-ready version of Korolev: a chief designer for your AI coding crew.

### Added
- **Onboarding from a repo link or path** (`onboard.py init`): clones into Korolev's own workspace (gh for private repos; your checkout is never touched), creates the project's state folder and writes `INVENTORY.md`. The inventory covers languages, size, agent instructions (including nested ones), Claude and Codex tooling (skills, subagents, commands, plugins, MCP), docs and specs, detected build and test commands, CI and recent commits.
- **Codex roles** (`run_codex.py`): one fresh ephemeral Codex session per job (analyst, builder, reviewers), read-only or write sandbox, a timeout, and a token usage log in `USAGE.jsonl`.
- **Second and third opinions** (`opinions.py`): Astra xhigh and Sol 5.6 xhigh review in parallel.
- **Owner channel on Telegram** (`notify.py`, `inbox.py`, `setup_telegram.py`): questions out, answers in, one bot for all projects with the project name on every message. It accepts the owner only, routes each reply to the right project, and runs as a single locked reader.
- **Self-improvement loop** (`lesson.py`): lessons from mistakes become concrete proposals that the owner approves on Telegram, built through Korolev's own loop. Safety-rule changes always need explicit approval.
- **Self-publishing releases** (`release.py`): version bump, detailed changelog, tag, a GitHub Release only after CI passes on the exact commit, and a Telegram notice.
- **`doctor.py` preflight**, a one-step `install.sh` (Claude Code and Codex), offline tests and CI.

### Added
- **Use the project's own agent tooling** (L-20261125-1)
  - Why: Workers ignored the repo's skills, subagents, plugins and nested AGENTS.md/CLAUDE.md
  - What changed: Inventory lists Claude/Codex skills, agents, commands, plugins, MCP; worker header tells Codex to follow and use them

### Changed
- **Codex cannot commit in workspace-write** (L-20261125-2)
  - Why: Builder wrote the file but .git is read-only in Codex's sandbox, so nothing was committed
  - What changed: run_codex.py commits the builder's working-tree changes itself, under the task title

### Fixed
- **Never read updates on a shared bot** (L-20261125-3, safety rule)
  - Why: getUpdates on a bot used by other services consumed their updates and changed its sticky allowed_updates
  - What changed: Only a dedicated bot; one locked inbox reader; golden rule 7
- **Owner replies must get a visible answer** (L-20261125-8)
  - Why: The owner's approval reached the inbox, but the bot's receipt failed silently and the coordinator sent no response, so the owner saw no reaction
  - What changed: inbox.py checks Telegram's ok flag, retries, records ack; SKILL.md: after acting on an owner message, reply with what is being done







