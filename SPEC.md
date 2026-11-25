# Korolev: specification v0.1 (draft for review)

Korolev is a skill plus small scripts. It lets one coordinator agent (Claude Code or Codex) run software development carried out by worker agents (Codex CLI, Kilo CLI, Claude), across one or more linked git repositories. It keeps the coordinator's own token spend low, checks every claim mechanically, keeps git as the single source of truth, and reaches the human owner on Telegram only for real decisions.

This spec is grounded in a real session running two linked production repos (2026-11-25). Each rule below exists because its absence cost time or money that day.

## 1. Lessons that drive the design

| Observed problem | Korolev rule |
|---|---|
| Dev chats re-read about 160k tokens of context on every model call after a few packages | A fresh worker session per package, seeded only with TASK.md plus a one-page STATE |
| Coordinator polling and long coordinator threads | No polling: runs start in the background and the coordinator wakes on exit. State lives in files, and the coordinator restarts per milestone |
| All work lived only in `/tmp` clones, nothing pushed | Push to the integration branch right after verification. Back up clones. Never treat temp paths as durable |
| Patch chains, unpushed branches and bundles as the "source" | Accepted means a commit reachable from the remote, with the SHA verified |
| A green gate that never really ran (hosted CI blocked by the sandbox; integration tests skipped without an environment variable) | Verify gates actually executed: count collected, passed and skipped tests, and fail on unexpected skips or zero-test runs |
| Tests named `*_regression.py` were never collected | Check that new test files are collected by the runner |
| Tests in the clone wrote into the original checkout | Fingerprint original checkouts before and after each run |
| Two repos that call each other were released separately | Pairs: linked repos are promoted, tagged and deployed only as a verified pair |
| A self-matching `pgrep` falsely reported a busy worker | Use real process and lock checks, never pattern matching on command lines |
| Secrets found and handled by hand | Scan every outgoing diff; credentials only by file path; nothing secret in Korolev, logs or prompts |
| Owner decisions lost in chat | `DECISIONS.md` (dated, newest first) is binding until folded into the product spec |

## 2. Repository layout (one private repo, `geenes/korolev`)

```
skill/SKILL.md            the coordinator procedure; symlinked into ~/.claude/skills/korolev and ~/.codex/skills/korolev
scripts/dispatch.py       launch one worker run for one package
scripts/verify.py         mechanical acceptance checks, writes VERIFY.json
scripts/promote.py        push to integration branch, fast-forward main, tag pairs
scripts/notify.py         Telegram send and inbox (owner answers)
scripts/stand.py          heavy-test stand: lock, rolling run counter, scratch quota watchdog, disk floor
scripts/backup.py         git bundles of worker clones to a durable directory
templates/                TASK.md, handoff.schema.json, STATE.md, DECISIONS.md, PAIRS.md, project.toml
products/<name>/          per-product state (no secrets): project.toml, STATE.md, DECISIONS.md, PAIRS.md, INBOX.md, packages/<NN-slug>/
tests/                    unit and integration tests with fake workers, git repos and a fake Telegram server
.github/workflows/ci.yml  runs tests on push
```

Python 3.12, standard library only. Exception: optional Telethon for creating Telegram groups, used only via an external venv path given in config. Every script supports `--help`, has deterministic exit codes, and prints JSON when given `--json`.

## 3. Product config: `products/<name>/project.toml`

- `[[repo]]`: `name`, `remote` (an https URL), `main_branch`, `integration_branch`, `clone` (the worker clone path), `original` (an optional checkout that must stay untouched; fingerprint its HEAD, index, refs and status), `allowed_paths` (globs, the default for tasks), `forbidden_patterns`. The secret-scan regexes are built in and extendable.
- `[repo.gates]`: `fast` (a list of commands that verify.py reruns in a throwaway worktree), `full` (the command for the full suite, or `ci = "github"` to use the hosted workflow conclusion), `expected_tests` (an optional path to a required-ID list), `max_skips` (an optional integer).
- `[[pair]]`: `repos = ["worker", "cabinet"]`, `gate` (a command template that receives both commit paths, e.g. it points the cabinet tests at the worker export), `tag_prefix`.
- `[workers]`: one role per key. Each role has `harness` (`codex`, `kilo` or `claude`), `model`, `effort` and `timeout_minutes`. Defaults:
  - `builder` = codex `gpt-6.2-sol`, effort high;
  - `reviewer_a` = codex `gpt-6.2-astra`, effort xhigh;
  - `reviewer_b` = codex `gpt-5.6-sol`, effort xhigh.
- `[telegram]`: `credentials` (path to an env file with `KOROLEV_BOT_TOKEN` and `KOROLEV_CHAT_ID`, mode 0600), `enabled`.
- `[host]`: `disk_floor_percent` (default 15), `stand_dir`, `stand_max_starts_24h`, `stand_run_quota_bytes`, `backup_dir`.

## 4. Package lifecycle

A package lives in `products/<p>/packages/<NN-slug>/`:

1. **TASK.md**, written by the coordinator from the template. It holds:
   - the goal;
   - the repo, base commit and work branch;
   - allowed paths;
   - acceptance checks;
   - stop rules (what not to do);
   - references to specs, commits and decisions by path or URL, never copies;
   - suggested skills.
2. **`dispatch.py <product> <package>`.** Preflight:
   - the product lock (flock);
   - the clone is clean and at the base commit;
   - the disk floor;
   - no live worker for that repo (checked by pid file plus `/proc` check, not by `pgrep -f`);
   - original checkout fingerprints recorded.

   It then launches the worker with a timeout:
   - **codex:** `codex exec -C <clone> -m <model> -c model_reasoning_effort=<effort> -s workspace-write --json --output-schema handoff.schema.json -o HANDOFF.json "<prompt>"`;
   - **kilo:** `kilo run --auto` in a new session, with a model or variant if supported;
   - **claude:** `claude -p` with equivalent options.

   The prompt is short: read TASK.md and STATE.md, obey the repo's AGENTS.md, and return the handoff schema. Outputs:
   - `RUN.json`: harness, model, start and end, exit code, timeout flag, and token usage parsed from the event stream when available;
   - `events.jsonl`;
   - `HANDOFF.json`.
3. **`verify.py <product> <package>`** writes `VERIFY.json` with `PASS`, `FAIL` or `NEEDS_HUMAN`, the reasons, and a summary of ten lines or fewer. Checks:
   - HANDOFF.json validates against the schema;
   - the claimed commit exists, descends from the base and matches the claimed tree;
   - the clone is clean;
   - changed paths are a subset of the allowed paths;
   - no forbidden patterns or secrets in the diff;
   - original checkout fingerprints are unchanged;
   - fast gates are rerun in a throwaway `git worktree` of the claimed commit, with exit codes recorded and compared with the handoff's claims;
   - new test files are actually collected;
   - the test count is at least `expected_tests`;
   - skips are at most `max_skips`;
   - nothing outside the clone was modified under configured watch paths.
4. **The coordinator reads only VERIFY.json and the handoff summary.**
   - On PASS, it runs `promote.py push` (integration branch, remote SHA verified) and `backup.py`, updates STATE.md, and writes the next TASK.
   - On FAIL, it writes a fix TASK or escalates.
   - For money, auth or security packages, it runs a review package: a reviewer role, read-only sandbox, returning a findings schema (severity, file:line, scenario).
5. **Full gates.** A full-suite run goes through `stand.py run -- <cmd>` locally, or through the hosted CI conclusion (`gh run watch` in the background, never polled by a model).
6. **`promote.py main <repo>`** fast-forwards main only if all of these hold:
   - a full-gate PASS is recorded for that exact commit;
   - for repos in a pair, the latest PAIRS.md entry covers this commit, or the change set does not touch the other side's contract paths (configurable);
   - main is an ancestor of the commit, so the move is a fast-forward.

   `promote.py tag <pair>` creates the same annotated tag in each repo of a verified pair.
7. **Deploy is out of scope for scripts in v0.1.** The coordinator follows each product's runbook. Before every deploy it sends the owner a Telegram notice with the SHA pair, window and rollback.

## 5. Owner contact: `notify.py`

- `send <product> "<text>"` or `send <product> -` (stdin), via the Bot API, with credentials read from the configured file. Never print the token or the URL.
- `ask <product> <questions.md>` sends one batched message: numbered questions, each with options and a recommended default. It appends the questions to DECISIONS.md under `## Pending`.
- `inbox <product>` reads bot updates (`getUpdates` with a stored offset), keeps only messages in the configured chat that reply to the bot's questions, and appends them to INBOX.md with a timestamp. The coordinator turns answers into DECISIONS.md entries.
- No customer personal data in messages. Messages are truncated at 4,000 characters.
- `setup-group` (optional) creates a private group with the owner's Telethon session and adds the bot. It requires an explicit `--yes`.

## 6. Spec cycle

When a big slice is done and remaining work is uncovered or undiscussed:
1. The coordinator drafts the next spec, referencing state, decisions and evidence.
2. It runs two independent review packages, `reviewer_a` and `reviewer_b`, read-only.
3. It merges their findings into a revision.
4. It sends open owner decisions via `notify.py ask`, and continues all unblocked work.
5. It splits the spec into packages for the builder.

## 7. SKILL.md (the coordinator procedure) must state

- **Session start:** read `products/<p>/STATE.md`, DECISIONS.md, PAIRS.md and INBOX.md, then the latest VERIFY.json files. Never read worker transcripts.
- **Cost rules:**
  - no polling; background runs only, wake on exit;
  - one shell command per verification;
  - read summaries rather than diffs, except for money, auth and security code, where a reviewer reads the diffs;
  - restart the coordinator per milestone.
- **Authority:**
  - what the owner delegated (e.g. push, promote, deploy) is recorded in DECISIONS.md;
  - real-money operations stay with the owner unless DECISIONS.md says otherwise;
  - everything else outward-facing needs a DECISIONS.md entry.
- **Pair rule** and main-branch rule, as above.
- **Handoff rules** (after Matt Pocock's `handoff` skill):
  - reference, don't copy;
  - redact secrets;
  - include a suggested-skills section;
  - downgrade unverified claims;
  - never leave state in temp.

## 8. Tests (required for v0.1)

- **dispatch:** a fake harness binary that writes a handoff and a commit; timeout handling; busy detection without self-matching; refusal on a dirty clone or the disk floor.
- **verify:** each check has a failing fixture, including:
  - a disallowed path;
  - a secret in the diff;
  - a changed original checkout;
  - an uncollected new test file;
  - a skip count over the limit;
  - a claimed exit code that differs from the rerun.
- **promote:**
  - refuses a non-fast-forward;
  - refuses main without a recorded full gate;
  - refuses a paired repo without a pair entry;
  - verifies remote SHAs against a local bare remote.
- **notify:** against a local fake Bot API server:
  - send;
  - ask, which updates the pending section;
  - inbox, which filters by chat and reply, stores the offset, and never echoes the token.
- **stand:** port of the first product's test-stand behavior and tests.

## 9. Migration of the first product

Create `products/<name>/` from the first product's existing control folder (STATE, DECISIONS, PAIRS, notify settings) and write its `project.toml`: both repos, clones, main and integration branches, gates, and the pair gate.

## 10. Out of scope for v0.1

Automated deploys, multi-host orchestration, a web UI, parallel workers on the same repo, and automatic model escalation.
