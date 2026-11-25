# Changelog

All notable changes to Korolev.

## v0.2.0 (2026-11-25)

### Added
- **You approve every Korolev release, and it is checked for personal data first.** Nothing is published until you reply "a" to a Telegram message describing the changes. Every outgoing commit is scanned for personal emails, home-folder paths, secrets and private words. Only the maintainer can publish.
  - Why: Korolev publishes itself to a public repo, so you should see and approve each release, and nothing private may leak.

### Changed
- **Warning when a worker run gets too expensive.** Each Codex run records how much context it used and flags runs above 1.5M input tokens (configurable). Normal runs use 0.4M–1M.
  - Why: long worker conversations quietly waste tokens; every package already starts a fresh session, and now oversized ones stand out.

### Fixed
- **Reliable "is it already running?" checks.** Korolev uses lock files instead of searching the process list, and two builders can no longer write to the same project at once.
  - Why: a process search once matched itself and wrongly reported a free chat as busy.
- **Proof that tests really ran.** The new `testcount.py` fails a check when no tests ran, when fewer ran than expected, or when new tests were silently skipped.
  - Why: integration tests once skipped without anyone noticing, and the run still looked green.
- **Waiting for GitHub CI on the exact commit.** The new `ci_wait.py` finds the CI run for the exact commit, waits for it, and confirms from the log that tests actually ran.
  - Why: CI once showed green without running a single test.

## v0.1.0 (2026-11-25)

First public-ready version of Korolev: a chief designer for your AI coding crew.

### Added
- **Onboarding from a repo link or path** (`onboard.py init`): clones into Korolev's own workspace (gh for private repos; your checkout is never touched), creates the project's state folder and writes `INVENTORY.md`. The inventory covers languages, size, agent instructions (including nested ones), Claude and Codex tooling (skills, subagents, commands, plugins, MCP), docs and specs, detected build and test commands, CI and recent commits.
- **Codex roles** (`run_codex.py`): one fresh Codex session per job (analyst, builder, reviewers), read-only or write sandbox, a timeout, and a token usage log.
- **Second and third opinions** (`opinions.py`): Astra xhigh and Sol 5.6 xhigh review in parallel.
- **Owner channel on Telegram** (`notify.py`, `inbox.py`, `setup_telegram.py`): questions out, answers in, one bot for all projects with the project name on every message. It accepts the owner only, routes each reply to the right project, and runs as a single locked reader.
- **Self-improvement loop** (`lesson.py`): lessons from mistakes become concrete proposals that the owner approves on Telegram.
- **Self-publishing releases** (`release.py`), **`doctor.py` preflight**, a one-step `install.sh`, offline tests and CI.
- **Workers use each project's own agent setup**: its AGENTS.md and CLAUDE.md files, skills, subagents, plugins and MCP configs.

### Changed
- **Korolev commits the builder's work itself.** Codex's write sandbox can't commit, so the launcher commits the finished changes under the task's title.

### Fixed
- **Korolev only ever reads its own dedicated bot.** Reading another bot's messages can steal updates from other services that use it.
- **Your replies always get a visible answer.** The bot's receipt is checked and retried, and Korolev replies with what it is doing.
