# Changelog

All notable changes to Korolev.

## v0.2.0 (2026-11-25)

### Added
- **Owner approves every Korolev publish, privacy-checked, maintainer-only** (L-20261125-9)
  - Why: Self-publishing must not push without the owner seeing the changes, must never leak personal info, and must work only for the maintainer
  - What changed: release.py propose, then approve, then publish, bound to the exact approved commit and notes; privacy_scan.py on every outgoing commit, tag and line; maintainer-only gate (upstream origin, maintainer login, local opt-in)

### Changed
- **Long worker chats waste tokens** (L-20261125-7)
  - Why: Dev chats re-read ~160k tokens of their own history on every call after a few packages
  - What changed: SKILL.md: a new worker session for every package, enforced by run_codex.py --ephemeral (done); a warning in run records when a run uses more than 1.5M input tokens (configurable; normal runs measured 0.4M–1M)

### Fixed
- **Busy checks must not pattern-match command lines** (L-20261125-4)
  - Why: pgrep -f matched its own shell, so a free chat was reported busy twice and the task was not sent
  - What changed: Add scripts/procs.py with lock-file and pid-start-time checks; SKILL.md forbids pgrep -f for busy detection
- **A green test run can hide skipped integration tests** (L-20261125-5)
  - Why: Integration tests skipped silently without one environment variable; the suite looked green but never tested the integration
  - What changed: Add scripts/testcount.py: parse pytest/node results, fail on zero collected, compare skips with the base and a per-product allowlist; verify step must use it
- **Hosted CI can be green without running tests** (L-20261125-6)
  - Why: The CI job failed at sandbox start, and a later green run had to be checked by hand to prove tests executed
  - What changed: Add scripts/ci_wait.py: find the run by exact commit SHA with retries, gh run watch --exit-status, then confirm from the job log that tests were collected and ran

### Commits
- aa343f9 Raise the large-context warning default to 1.5M input tokens; fix rule numbering
- 49b14ad Task 04-context-warning: warn about expensive worker context (lesson L-2
- a855710 Task 03-procs: reliable "is it running" checks without pattern matching
- 1454e38 Task 02-ci-wait: wait for hosted CI by exact commit and prove tests ran
- 250e5f8 Task 01-testcount: prove that tests actually ran (lesson L-20261125-5)
- 42b2a2b Owner-approved, privacy-checked, maintainer-only self-publishing
- cd02fe1 README: call the Telegram channel the Korolev bot


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







