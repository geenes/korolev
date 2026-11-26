# Changelog

All notable changes to Korolev.

## v0.3.2 (2026-11-26)

v0.3.2 is the first published release of the v0.3 line. The v0.3.0 and v0.3.1 tags exist but were never released: each failed CI on GitHub's test machine for a test-setup reason, and the release step stopped as designed. This version contains everything below.

### Added
- **English by default, with a language setting** (L-20261125-15)
  - Why: Korolev's own messages were hard-coded in Russian; most users expect English and some want another language
- **Questions written so anyone can answer them** (L-20261125-16)
  - Why: Questions used internal codes, file names and technical terms that most people would not understand
- **Korolev picks up where it left off when your usage limit resets** (L-20261125-19)
  - Why: When your Claude usage limit is reached, work stops mid-way and nothing restarts it until you come back

### Changed
- **Workers run with their own clean Codex setup** (L-20261125-10)
  - Why: Every worker inherited the global Codex setup (unrelated skills, plugins and shared instructions), adding about 3,700 tokens to every model call and exposing other projects' tools
- **Shorter coordinator memory with a written handoff** (L-20261125-12)
  - Why: A single long coordinator chat grew very large and re-reads its whole history every turn, the same waste as long worker chats
- **Tracking what tokens actually cost** (L-20261125-13)
  - Why: Run totals of 0.4M-1M input tokens are mostly cached prompt, which is much cheaper; the totals hide what actually costs money and what bloats

### Fixed
- **Workers can't see other projects or secrets** (L-20261125-11, safety rule)
  - Why: Codex's read-only sandbox still lets a worker read the whole disk: other projects, Korolev's Telegram credentials and SSH keys
- **Releases no longer stall on duplicate CI runs** (L-20261125-14)
  - Why: Pushing main and the version tag together started CI twice for one commit, and the release stopped instead of guessing which run to trust
- **Release notes describe features in plain words** (L-20261125-17)
  - Why: Release notes used each lesson's problem title, so a fix appeared as a feature named after the problem
- **Codex jobs can no longer hang waiting for input** (L-20261125-20)
  - Why: two research jobs started the wrong way waited for input forever and produced nothing
- **Test checks now fail when every test was skipped** (L-20261125-22)
  - Why: an independent review showed a run where all tests were skipped still passed the check
- **Workers run commands normally inside the sandbox** (L-20261125-23, safety rule)
  - Why: Codex's own sandbox cannot start inside Korolev's, so workers could not run commands; Korolev's sandbox now does the job alone, and the coordinator tests it end to end
- **Resumed sessions can read credential files** (L-20261125-24)
  - Why: The unattended resume session restricts shell commands and writes, but its Read, Glob and Grep tools can still open files such as the Telegram credentials
  - What changed: Add deny rules for credential paths (~/.config/korolev/**, ~/.ssh/**, *.env) to the resume session settings, with a test
  - Warning: no change title. Set one with `lesson.py retitle L-20261125-24 --change "…"`.
- **Tests pass the same way on any machine, and CI runs the real sandbox tests** (L-20261125-26)
  - Why: 16 tests drove the launcher with a fake Codex while the worker sandbox (on by default) needed bubblewrap; this machine has it, GitHub's runner does not, so v0.3.0 failed CI and was not released
- **Questions never reuse labels between items and answer options** (L-20261125-27)
  - Why: Question 5 named two fixes 5a and 5b and also asked the owner to answer 5a or 5b, so the answer would have been ambiguous
- **Korolev continues the same chat when your usage comes back, even if it forgot to say it was busy** (L-20261126-1)
  - Why: The coordinator hit the usage limit mid-work, but its heartbeat still said idle (it forgot to switch back to working), and the owner's Telegram reply arrived with nobody to act on it; the watcher checked 144 times overnight and correctly di…
- **Tests no longer depend on the project's git history** (L-20261126-3)
  - Why: The release-notes test copied Korolev itself and expected commits since the last tag; CI checks out only the latest commit, so v0.3.1 failed CI on GitHub while passing locally

### Commits
- dc102de Tests: release-notes test builds its own repo; CI fetches full history
- 87b58f2 Task 17-resume-same-chat: continue the same chat when usage comes back (
- e54c674 Task 16-safety-small: block credential reads in resumed sessions; every


## v0.3.1 (2026-11-26)

v0.3.1 is the first published release of the v0.3 line. The v0.3.0 tag exists but was never released: its CI failed on GitHub's test machine, so the release step stopped as designed. This version contains everything listed under v0.3.0, plus:

### Fixed
- **Tests pass the same way on any machine, and CI runs the real sandbox tests** (L-20261125-26)
  - Why: the tests needed the sandbox tool, which GitHub's test machine lacked, so v0.3.0 failed CI there while passing here

### Everything from v0.3.0
### Added
- **English by default, with a language setting** (L-20261125-15)
  - Why: Korolev's own messages were hard-coded in Russian; most users expect English and some want another language
- **Questions written so anyone can answer them** (L-20261125-16)
  - Why: Questions used internal codes, file names and technical terms that most people would not understand
- **Korolev picks up where it left off when your usage limit resets** (L-20261125-19)
  - Why: When your Claude usage limit is reached, work stops mid-way and nothing restarts it until you come back

### Changed
- **Workers run with their own clean Codex setup** (L-20261125-10)
  - Why: Every worker inherited the global Codex setup (unrelated skills, plugins and shared instructions), adding about 3,700 tokens to every model call and exposing other projects' tools
- **Shorter coordinator memory with a written handoff** (L-20261125-12)
  - Why: A single long coordinator chat grew very large and re-reads its whole history every turn, the same waste as long worker chats
- **Tracking what tokens actually cost** (L-20261125-13)
  - Why: Run totals of 0.4M-1M input tokens are mostly cached prompt, which is much cheaper; the totals hide what actually costs money and what bloats

### Fixed
- **Workers can't see other projects or secrets** (L-20261125-11, safety rule)
  - Why: Codex's read-only sandbox still lets a worker read the whole disk: other projects, Korolev's Telegram credentials and SSH keys
- **Releases no longer stall on duplicate CI runs** (L-20261125-14)
  - Why: Pushing main and the version tag together started CI twice for one commit, and the release stopped instead of guessing which run to trust
- **Release notes describe features in plain words** (L-20261125-17)
  - Why: Release notes used each lesson's problem title, so a fix appeared as a feature named after the problem
- **Test checks now fail when every test was skipped** (L-20261125-22)
  - Why: an independent review showed a run where all tests were skipped still passed the check
- **Workers run commands normally inside the sandbox** (L-20261125-23, safety rule)
  - Why: Codex's own sandbox cannot start inside Korolev's, so workers could not run commands; Korolev's sandbox now does the job alone, and the coordinator tests it end to end


## v0.3.0 (2026-11-25)

### Added
- **English by default, with a language setting** (L-20261125-15)
  - Why: Korolev's own messages were hard-coded in Russian; most users expect English and some want another language
- **Questions written so anyone can answer them** (L-20261125-16)
  - Why: Questions used internal codes, file names and technical terms that most people would not understand
- **Korolev picks up where it left off when your usage limit resets** (L-20261125-19)
  - Why: When your Claude usage limit is reached, work stops mid-way and nothing restarts it until you come back

### Changed
- **Workers run with their own clean Codex setup** (L-20261125-10)
  - Why: Every worker inherited the global Codex setup (unrelated skills, plugins and shared instructions), adding about 3,700 tokens to every model call and exposing other projects' tools
- **Shorter coordinator memory with a written handoff** (L-20261125-12)
  - Why: A single long coordinator chat grew very large and re-reads its whole history every turn, the same waste as long worker chats
- **Tracking what tokens actually cost** (L-20261125-13)
  - Why: Run totals of 0.4M-1M input tokens are mostly cached prompt, which is much cheaper; the totals hide what actually costs money and what bloats

### Fixed
- **Workers can't see other projects or secrets** (L-20261125-11, safety rule)
  - Why: Codex's read-only sandbox still lets a worker read the whole disk: other projects, Korolev's Telegram credentials and SSH keys
- **Releases no longer stall on duplicate CI runs** (L-20261125-14)
  - Why: Pushing main and the version tag together started CI twice for one commit, and the release stopped instead of guessing which run to trust
- **Release notes describe features in plain words** (L-20261125-17)
  - Why: Release notes used each lesson's problem title, so a fix appeared as a feature named after the problem
- **Test checks now fail when every test was skipped** (L-20261125-22)
  - Why: an independent review showed a run where all tests were skipped still passed the check
- **Workers run commands normally inside the sandbox** (L-20261125-23, safety rule)
  - Why: Codex's own sandbox cannot start inside Korolev's, so workers could not run commands; Korolev's sandbox now does the job alone, and the coordinator tests it end to end

### Commits
- 6b43240 README: present Korolev as a Claude Code skill
- f830ab7 Task 14-testcount-all-skipped: a run where nothing executed must fail (L
- 44c1db6 Task 11b-sandbox-nesting: Codex must work inside Korolev's sandbox (L-20
- 8fabde3 Task 13-auto-resume: resume automatically when the usage limit resets (L
- 8bd1d3f Task 11-worker-sandbox: workers cannot read other projects or secrets (l
- 852f3c3 Tests: pass the now-required --change to existing lesson fixtures
- 7964c9b Task 12-release-titles: release notes describe features, not problems (L
- 1af62be Task 10-coordinator-handoff: keep the coordinator's own context small (l
- ac2eed9 Task 09-billable-tokens: measure what tokens actually cost (lesson L-202
- c9c08e0 Task 08-codex-home: workers run with an isolated Codex home (lesson L-20
- 1e82f1f Task 07-plain-questions: owner questions in plain language (lesson L-202
- 8c927c0 Task 06-language: English by default, language configurable (lesson L-20
- 1235e99 Task 05-ci-branch: release CI check must handle branch and tag runs (les


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
