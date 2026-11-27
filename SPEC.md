# Korolev: specification, revision 2 (draft for review)

Revision 2 replaces revision 1 (v0.1, 2026-11-25). Spec revisions are numbered separately from releases: **everything here ships as 0.3.x patch releases** (0.3.3, 0.3.4, …), one or more per stage, each approved by the owner for its exact commit. No 0.4 or 0.5 (owner decision, 2026-11-26).

Korolev is a Claude Code skill plus standard-library Python scripts. A Claude coordinator plans, decides, verifies and talks to the owner on Telegram. Worker tools (Codex today) do the reading-heavy and writing-heavy work: analysis, reviews and building. State lives in files under `$KOROLEV_HOME/products/<name>/`.

## 0. What changed since revision 1

Revision 1 described a design (`dispatch.py`, `verify.py`, `promote.py`, `project.toml`, pairs, `stand.py`, `backup.py`) that was mostly not built in that form. What shipped in 0.1.0–0.3.2 is: `run_codex.py` (launcher with stdin prompt, timeout, bubblewrap sandbox, isolated Codex home, usage log, auto-commit), `opinions.py`, `testcount.py`, `ci_wait.py`, `procs.py`, `inbox.py`/`notify.py` (owner-ID-filtered Telegram), `heartbeat.py`/`handoff.py`/`resume_watch.py`, `lesson.py`, `release.py` with `privacy_scan.py`, `doctor.py`, `onboard.py`. This revision specifies the next work **on top of what exists**. Revision 1 items not listed here (pairs, stand, backup, Kilo, deploy scripts) stay deferred; §8 lists them.

Revision 1 review findings already addressed by shipped code: owner authentication by numeric Telegram user ID; exact-SHA hosted CI checks; launcher-made commits; zero-test and all-skipped runs fail; worker write confinement and credential hiding. Findings still open are folded into stage 2 (§5).

## 1. Incidents that drive this revision

| Incident (2026-11-25/26) | Rule in this revision |
|---|---|
| The resume watchdog read a 20-minute-old `working` heartbeat as a usage stop and started a second copy of a chat that was still open (waiting for a sign-in code) | Resume only after a **confirmed** usage-limit stop and only when no process holds the session (§3.1). Watchdog stays disabled until this ships |
| The resumed copy ran with a narrow allowlist and no skills, so reviewers and spec work were blocked | Resumed chats run with the owner's normal setup; only destructive or outward actions are denied (§3.2) |
| The owner had no direct way to open the chat working on a project from the phone | Every project chat starts with Remote Control and its link goes to Telegram (§3.3) |
| CI failed twice after local tests passed (no bubblewrap on GitHub; a test depended on git history) | `release.py propose` first runs the suite in a fresh shallow clone without bubblewrap (§3.9) |
| One project's state was scattered across several folders | Korolev works in the owner's folder; one home per project; `doctor` flags strays (§3.4) |
| A bare `codex exec` hung for 50 minutes although `run_codex.py` exists | A guard hook blocks bare worker CLI calls (§3.6) |
| Plans were built from code while product facts were unknown | Discovery first: product brief, use cases, verification traced to use cases (§3.7) |
| A new product idea had no flow | New-idea mode (§3.8) |
| Work existed only on local branches, in a `/tmp` clone and in another tool's uncommitted worktree; Korolev's clones read older rules than the owner's folder | In-place mode, Task 0 grasp and baseline (§3.4, §3.5) |
| The owner answered "do" to a release; the parser knew only a/yes/ok | Plain approval words, clarifying reply otherwise (§3.10) |
| Reviewers had no way to ask; doubts ended up as findings or were lost | Reviewer questions answered by the coordinator, the owner only when it is in doubt (§3.11) |

## 2. Stages

| Stage | Contents | Ships as |
|---|---|---|
| 1 | In-place mode and Task 0, reliable resume, full-setup resumed chats, chat links, guard hook, discovery and verification, new-idea mode, patch versioning, CI-parity preflight, approval words | 0.3.3 (in-place mode, Task 0, resume, links), 0.3.4 (guard, versioning, approvals, discovery, new-idea, reviewer questions, setup part 1, project start interview, question registry) |
| 2 | Tools per job (adapters incl. Gemini via Antigravity), provider alerts, `verify.py` with reviews bound to a revision, partial handoff states, per-attempt cost, decision-bound Telegram answers, both-install test | 0.3.5+ |
| 3 | Self-running learning (recurrence, evidence, enforcement, replay gate, dedup and decay, usefulness, rollback), plugin install | 0.3.x after stage 2 |

Every package follows the normal build loop (TASK → builder → coordinator verification → tests). One lesson per package. Safety packages (marked **[safety]**) need the owner's explicit "a" before they are applied.

## 3. Stage 1

### 3.1 Resume only after a confirmed usage stop [safety] (L-20261126-6)

`resume_watch.py` may start or continue a coordinator chat for product P only if **all** hold:

1. P's heartbeat status is `working` or `waiting_limit`. New status `waiting_owner` (waiting for an answer, a sign-in code, an approval) **never** triggers a resume, whatever its age. `heartbeat.py set --status waiting_owner` is what the skill uses before every wait on the owner.
2. The recorded session's transcript ends in a usage or rate-limit error. Read only the transcript tail (last 64 KiB), parse JSONL, take the last non-meta entry; it must be an API error entry that matches the existing `LIMIT` pattern. A stale heartbeat alone is never evidence.
3. The limit has reset: the parsed reset time has passed, or, without one, a `claude -p` probe succeeds.
4. **No process holds the session**, decided by `procs.py session-holders --session ID --cwd DIR`: any live process whose command line contains the session ID, any `claude` process whose cwd equals the recorded cwd, or any process with the transcript file open. Implemented by reading `/proc` directly (no `pgrep`, rule 9), with process start time recorded to survive PID reuse. Any holder → no resume, log the reason.
5. The coordinator lock (`heartbeat.py run` lock) is free.

The owner-message trigger (owner wrote, Korolev did not answer) also requires 4 and 5; when a holder exists, the watcher sends nothing (the open chat will answer).

Tests: a **replay of the 2026-11-26 incident** (stale `working` heartbeat, transcript ending in an assistant turn awaiting a code, live `claude` in the same cwd) must produce no resume; a transcript ending in a limit error with no holder must resume; `waiting_owner` of any age must not resume; PID-reuse fixture.

The timer stays disabled until this ships and the owner confirms re-enabling it (question 1).

### 3.2 Resumed chats run with the owner's normal setup (L-20261126-5)

A resumed chat is the same chat, continued with the owner's normal Claude Code configuration: user settings, skills, plugins and MCP servers. No `--tools` narrowing and no `--setting-sources` restriction.

- It runs interactively in tmux with Remote Control (§3.3), so the owner can watch and steer it from the phone, instead of headless `claude -p`.
- Permission mode: `auto` where available, else `acceptEdits`. A **deny list** passed with `--settings` blocks: `git push`, `gh release`, `gh repo create|delete`, `gh pr merge`, `release.py publish`, deploy scripts named in the product's runbook, `rm -rf` outside task worktrees, the folder-changing git commands listed in §3.4, and reads of credential paths (`~/.config/korolev/**`, `~/.ssh/**`, `**/*.env`, `**/telegram.env`). The deny list is one module (`resume_policy.py`) with a unit test per rule.
- When a denied action is needed, the chat asks the owner on Telegram with the chat link and waits (`waiting_owner`).

### 3.3 Every project chat has a link (L-20261126-4)

`korolev chat <project>` (new `scripts/chat.py`, plus a `korolev` launcher installed to `~/.local/bin`):

1. Refuses if tmux session `korolev-<project>` is alive; prints and re-sends the recorded link instead (one active chat per project).
2. Starts `claude --remote-control "Korolev · <project>" -n "Korolev · <project>"` in a new tmux session, in the attached folder (§3.4), under `heartbeat.py run`.
3. Handles first-run screens within a bounded budget (at most 10 screen reads, 60 s): folder trust is answered only for the registered directory; any unknown screen stops with a Telegram message to the owner and the pane text redacted.
4. Captures the `https://claude.ai/code/session_…` link from the pane, records it in `STATE.md` under `## Controller chat` and in the heartbeat, and sends it on Telegram.
5. Sends a start prompt ("korolev: resume product <project>" or a given task).

The resume watcher uses the same function (with `--resume <session>`), so every new or resumed chat sends its link. Fresh chat per milestone: `korolev chat <project> --new` ends the previous tmux session only after its handoff is written and its heartbeat is idle.

### 3.4 In-place mode: Korolev works in the owner's folder [safety] (owner decision 2026-11-26)

The owner has one folder per project: the repo on the server, where the app runs and where they edit and build. Korolev works **with that folder** instead of a build clone. Build clones (`~/korolev/work/<name>/repo`) are dropped: they drifted from the owner's folders and from GitHub (2026-11-26 investigation). Reviewed by Astra, Sol 5.6 and Gemini 4 Hydrogen (`OPINION-*-inplace.md`, answers in `REVIEW-ANSWERS-inplace.md`).

**Three ways to start, one project-named folder each** (`~/projects/<name>` by default, `config.json` `projects_root`; never a generic Korolev path):
- **Existing folder:** `korolev attach <path>` registers it as it is.
- **GitHub repo with no folder on this server:** `korolev attach <github-url>` clones it once into `~/projects/<repo-name>` (owner confirms the path; refuses an existing non-empty destination). That clone is the folder from then on.
- **New project:** new-idea mode (§3.8) creates `~/projects/<name>` with `git init` and attaches it.

**Identity.** The registry (`$KOROLEV_HOME/projects.json`) records the canonical folder (symlinks resolved), the Git common directory (`git rev-parse --git-common-dir`), the current branch and its upstream, the default branch and the remotes. One project per common Git directory: a second folder sharing that `.git` (a linked worktree) or the same remote is reported as a collision, not silently attached. Reattaching resumes. Existing product folders keep working: the registry maps them to the owner's folder; nothing is moved or renamed.

**Where things live.**
- **In the repo, committed:** `.korolev/` holds only reviewed project knowledge: `PRODUCT-BRIEF.md`, `USE-CASES.md`, `MAP.md`, `PLAN.md`, `DECISIONS.md`, `BASELINE.md`. It is committed like any other change, after the secret scan, as part of an approved commit. `config.json` `commit_knowledge: false` keeps it out of the repo entirely.
- **Outside the repo** (`$KOROLEV_HOME/products/<name>/`, as today): state, heartbeat, usage, handoffs, raw reviews, journals, pending approvals and backups (mode 0700).
- **Task worktrees outside the repo:** `$KOROLEV_HOME/tasks/<name>/<NN-slug>`, created with `git worktree add` from the owner's repo. They share its `.git` (no second repo), and no watcher, test runner or container build in the owner's folder can see them.

**Workers never touch the live folder.**
- Every write role must run in a registered task worktree; `run_codex.py` refuses `--write` anywhere else.
- The sandbox binds that worktree and its outbox writable and the Git common directory read-only (needed for `git` to work), and hides the owner's folder, `.korolev/` in the worktree (read-only) and credential-like paths (`*.env`, keys, `*.pem`, data stores), which read-only analysts cannot read either.
- The launcher stages exact paths, not `git add -A`; every staged blob passes the secret scan (§3.5 step 4) before commit. Changes under `.korolev/` by a worker fail verification.

**Updating the owner's folder (live update).**
- Only by fast-forward, only when the folder is on the recorded branch at the recorded HEAD, its index is clean, and no path the update touches is modified, staged or untracked. All checks are repeated under the project lock immediately before `git merge --ff-only`.
- Never: `reset --hard`, `clean`, `checkout -- .`, `stash`, branch switches, rebase or force-push on the owner's folder or branches. `release.py`'s hard-reset rollback refuses to run in a registered owner folder.
- If anything changed, or a process runs from the folder (`/proc` cwd and command lines), the outcome is **published, live update pending**. The owner picks the moment, and Korolev then rechecks and fast-forwards. It restarts or deploys only under the project's runbook with owner approval.
- Before every live update Korolev writes a backup and checks that it restores (§3.5 step 6).
- The update itself needs the owner's approval of the exact SHA (default). `config.json` `merge_policy: auto` allows it without asking only when the folder is clean, unchanged and nothing runs from it.

**One controller per repository.**
- A per-project lock (`fcntl.flock` via `procs.py`) keyed by the Git common directory: a second Korolev chat on the same repo, through any folder or worktree, refuses to run.
- On attach and at each chat start Korolev lists other controllers touching the repo: other tools' worktrees (`.kilo/worktrees`, `.codex/worktrees`, …), agent processes with cwd in the repo, and supervision locks named in AGENTS.md. Task 0 asks the owner to retire or keep each.
- A Korolev section for AGENTS.md (who controls, where data lives, branch rules) is proposed as an ordinary diff inside the baseline and committed only with its approval; existing AGENTS.md text is preserved.

`doctor.py` reports registry collisions and Korolev state outside registered paths as WARN; it never deletes.

### 3.5 Task 0 on attach: grasp the project, then baseline the code [safety]

Task 0 is the first task for every attached project. Each step is journaled in `$KOROLEV_HOME/products/<name>/task0.json` (phase, inputs, candidate SHAs, approvals, pushes, CI, live update). After a crash Korolev inspects the actual refs and resumes from the journal; it never repeats a push blindly.

1. **Existing Korolev knowledge first.** Read `.korolev/` in the folder, `korolev/*` branches on the remote and the legacy product folder, and continue from them. Facts already recorded are re-checked against the code, not re-derived.
2. **Grasp the project.** The analyst role (read-only, secrets masked) produces, and the coordinator checks:
   - `MAP.md`: structure, stack, entry points, data stores, external services, and the build, test, run and deploy commands. Build and test commands are probed only in a task worktree without production credentials, with their exit codes. Deploy and production commands are recorded as **unverified** and never run.
   - running processes and services that use this folder;
   - specs and docs ranked by authority (the live AGENTS.md and the owner's latest decisions first), conflicts listed;
   - discovery (§3.7) drafts `PRODUCT-BRIEF.md` and `USE-CASES.md`, tagged known/assumed/unknown.
3. **Reconcile the code** (`baseline.py scan`, read-only). It records a pinned snapshot: HEAD, branch, index tree, remote OIDs and per-path hashes and modes. Coverage:
   - the folder: staged, unstaged and untracked files, separately;
   - every linked worktree (including other tools'), with its staged and unstaged changes;
   - stashes, including their index and untracked parts;
   - local branches not on the remote, and same-name branches that diverged;
   - remote branches not merged into the default branch;
   - other clones of the same remote found by a bounded, read-only search (home folder, `/var`, `/tmp`, depth 3) plus owner-nominated paths, which are listed with their heads and dirty counts;
   - submodules (pointer and dirty state, never recursed into), LFS objects, symlinks, large and binary files: reported, not interpreted.
   
   Evidence is exact: ancestry, path, mode and blob identity, patch identity. Added-line overlap is shown only as a hint. Each item is classified `in remote`, `superseded` (only with exact evidence), `commit`, `archive`, `never commit` (credentials, data, runtime: listed by name only) or `owner decides` (anything ambiguous). Output: a machine manifest (`baseline.json`, outside the repo) and a redacted `BASELINE.md` grouped by function, with a recommendation and its coverage limits per group.
   
   **Target branch:** the folder's current branch and its upstream. Merging that branch into the default branch is an owner decision listed in `BASELINE.md`. A detached HEAD, an unborn repository, a missing remote or several remotes need the owner to choose a target before anything is prepared.
4. **Approval 1: the plan.** One Telegram batch: the project in plain words, the top unknowns (at most 5), the baseline groups with recommendations, the other controllers found, and where archives would go. The owner approves or changes it. This authorizes preparation only: no push, no live update.
5. **Prepare candidates locally.** In a task worktree on `korolev/00-baseline`, from the pinned snapshot:
   - copy approved paths from a private snapshot and stage them with exact pathspecs (including deletions), one commit per `commit` group, plus the knowledge files and the AGENTS.md diff if approved;
   - check that the candidate diff equals the approved manifest;
   - run the secret scan over the exact staged trees and every newly reachable blob and commit message (not only the endpoint diff);
   - run the project's tests where Task 0 found them;
   - build `archive` groups as local refs `korolev/archive/<group>`.
6. **Approval 2: exact SHAs.** One message names the candidate SHAs, the destination refs, the remote's visibility, whether CI exists, and the live-update step (with a backup and restart note if something runs from the folder). Before sending, Korolev revalidates the pinned snapshot; any drift means rescan and a new approval 1. After approval it pushes `korolev/00-baseline`, waits for CI, opens a pull request where the branch is protected (absent CI is reported as unverified, never as passed), and merges into the target per the approval.
   - Archive refs are pushed only to a private remote (checked with `gh repo view --json visibility`) and only if approved; otherwise they stay as a git bundle in the product folder.
7. **Live update** per §3.4. Before it, a backup: HEAD and ref, index tree, staged and unstaged patches, untracked files (regular files and symlinks only, size-capped, excluding ignored build folders), and hashes. Korolev restores it into a scratch folder and compares before the update. If anything changed in the folder since approval 2, the outcome is "published, live update pending".
8. **After Task 0:** the remote, the folder and `.korolev/` agree; one controller holds the lock; the plan builds from there.

Tests (fixture repos):
- every classification, including deletion-only fixes and staged-vs-unstaged differences;
- a dirty file touched by the live update (must leave it pending);
- owner edits between scan and approval 2 (must rescan);
- a crash after push (must resume from the journal without re-pushing);
- a secret added then removed across two commits (must block the push);
- a worker trying to write the live folder or `.korolev/` (must be refused);
- a second chat through a linked worktree (must refuse);
- detached HEAD and missing remote (must stop for the owner);
- a public remote with archive groups (must keep them local);
- a replay of the 2026-11-26 findings: three features of one project only on local branches, a second project's work only in a `/tmp` clone, uncommitted staged work in a Kilo worktree. All must appear in `BASELINE.md`.

### 3.6 Guard hook: no bare worker CLI calls

A Claude Code `PreToolUse` hook (`hooks/guard.py`) denies Bash commands that start a worker CLI directly (`codex exec`, `codex e`, `agy`, `agy-desktop`, `gemini`) unless invoked through Korolev's launchers, and tells the model which launcher to use. `install.sh` adds the hook to `~/.claude/settings.json` (merging, with a backup, idempotent); stage 3 ships it inside the plugin instead. `doctor.py` reports whether the hook is installed. Tests: command variants (`env X=1 codex exec`, `bash -c "codex exec"`, absolute paths), and allowed launcher calls.

### 3.7 Discovery first, verification against use cases (L-20261126-9)

Phase 1 of the skill starts with discovery for new onboards and new ideas; existing products get it at their next spec cycle (question 3).

1. **`PRODUCT-BRIEF.md`** (template): users, jobs to be done, alternatives, business model, constraints, success metrics. Every line is tagged `known (source)`, `assumed` or `unknown`.
2. The owner is asked only the `unknown` and risky `assumed` items, in rounds of at most 5 plain questions.
3. **`USE-CASES.md`** (template): a story map of the main journeys; per use case `UC-n`: rules, concrete examples, open questions, and acceptance criteria in Given/When/Then.
4. `PLAN.md` packages and `TASK.md` cite the `UC-n` they serve and how they will be verified.
5. Verification follows the use cases:
   - **end to end** on the running system through a per-project verify skill (`products/<p>/verify/SKILL.md`, written during discovery: how to start the system, seed data, drive the journeys);
   - **differential tests** against the old system for rewrites and consolidations;
   - **checks out of reach**: acceptance checks for a package live in `.korolev/`, which verification forbids workers to change (§3.4), and the coordinator reruns them;
   - every accepted commit gets a `Verified-by:` trailer (`unit`, `e2e`, `differential`, `review`, `manual`, `none`);
   - new gates start in **observe** mode (reported, not blocking) and switch to **enforce** after 3 consecutive green runs or by owner decision.

Enforced by a new `trace.py check --product P`: fails if a planned package cites no use case, a use case in scope has no Given/When/Then, or an accepted package lacks a `Verified-by` trailer. Tests with fixture products.

### 3.8 New-idea mode (L-20261126-10)

`onboard.py idea --name N` creates a product with no repository. Flow:

1. The owner describes the idea in plain words (chat or Telegram).
2. Korolev drafts `PRODUCT-BRIEF.md`, then asks question rounds (idea, users, problem, business model, constraints), at most 5 questions per round, at most 3 rounds before writing the remaining unknowns down as assumptions.
3. It writes `ARCHITECTURE-BRIEF.md` and `MARKETING-BRIEF.md` (positioning, audience, channels), tagged known/assumed/unknown.
4. The configured reviewers critique the three briefs (`templates/prompts/brief-review.md`).
5. Then use cases, spec, plan; after the owner approves the plan, a local repository scaffold in `~/projects/<name>` (`git init`, README, AGENTS.md, `.korolev/`, test skeleton, CI file), attached per §3.4. Creating the GitHub repository (same name) is outward-facing: see question 4.

### 3.9 Patch versioning and CI-parity preflight

- `release.py` defaults to a **patch** bump. `config.json` key `release_line` (default `"0.3"`) makes it refuse any version outside that line; `--level minor|major` additionally requires `--owner-decision <DECISIONS.md date>`. Tests.
- `release.py propose` first clones the candidate commit with `--depth 1` into a scratch folder and runs the suite there with bubblewrap hidden (`tests/run_without_bwrap.py`), then with it. Any failure stops the proposal before the owner is asked.

### 3.10 Plain approval words (L-20261126-2)

Approval replies accept `a`, `yes`, `ok`, `do`, `go`, `go ahead`, `publish`, `ship`, `sure`, `approved`, `👍`, `✅` and Russian equivalents (`да`, `давай`, `делай`, `публикуй`); rejections accept `b`, `no`, `stop`, `reject`, `нет`, `стоп`. Anything else, or a reply that matches both, gets a one-line clarifying question; nothing is assumed. The parser lives in one place (`inbox.py`) and is used by release approval and question routing. Tests per word, mixed replies, and replies to an unrelated message.

### 3.11 Reviewer questions go through the coordinator (owner decision 2026-11-26)

Reviewers may have questions as well as findings. Every review prompt (`templates/prompts/*review*.md`, `opinions.py`) ends with a required `## Questions` section: each question numbered, with why it matters and the reviewer's default assumption. `none` if there are none.

1. The coordinator answers each question from the spec, code, `DECISIONS.md`, `PRODUCT-BRIEF.md` and earlier answers, citing the source.
2. A question the coordinator cannot answer with confidence (missing product fact, business or money choice, conflicting sources, a risk the owner should own) goes to the owner on Telegram in plain words, batched with other open questions (at most 5 per message), with the reviewer's default as the proposed answer.
3. Answers are recorded in `DECISIONS.md` (source: coordinator or owner) and in a `REVIEW-ANSWERS-<tag>.md` next to the opinions.
4. If an answer changes what was reviewed, the changed part goes back to the reviewer who asked, bound to the new revision (§4.3). Otherwise the answer is noted and work continues.
5. Unanswered owner questions do not block unrelated work; the package that depends on them waits (`waiting_owner` for that package only).

`opinions.py` extracts the `## Questions` sections into `QUESTIONS-<tag>.json` (reviewer, number, text, default, status). Tests: a review with questions, one with `none`, one missing the section (warned, treated as none), and an owner answer recorded against the right question.

### 3.12 First-time setup: connect, then choose the team (owner decision 2026-11-26)

`korolev setup` runs in the chat. It also starts automatically when no completed setup is recorded (`setup.json`, `"completed": true`). A language-only `config.json` from older versions does not count as a completed setup. Setup can be re-run at any time: a re-run starts from the current values and changes only what the owner changes. Progress is saved after each step, so an interrupted setup resumes where it stopped. The configuration is written atomically, with a schema version.

**Secrets never go through chat or Telegram.** Bot tokens and API keys are entered only through a hidden prompt on the server (`korolev secret <name>`, run by the owner in a terminal on the server; `getpass`, never argv). They are stored in `~/.config/korolev/secrets/<name>` (0600, directory 0700). `config.json` holds only a reference to the secret. `doctor.py`, handoffs and summaries show only whether a secret is present, never its value. Each worker sandbox receives only the secrets its own adapter needs. Rotating a secret means re-running `korolev secret <name>`, followed by the provider check. If an owner pastes a secret into chat anyway, Korolev does not repeat it, tells the owner to rotate it, and does not use it.

1. **Language** for messages and documents (default English).
2. **Owner channel.** Telegram (recommended) or chat only.
   - **Choosing Telegram runs the bot setup here.** Korolev explains the BotFather steps (`/newbot`, a name ending in `bot`). The owner stores the token with `korolev secret telegram`.
   - **Pairing.** Korolev shows a one-time code that expires in 10 minutes. Only `/start <code>`, sent in a private chat, records the owner and chat IDs. Groups are refused.
   - **The inbox reader is paused while pairing.** It runs as a persistent user service (systemd `korolev-inbox.service`, one locked reader), so the owner can answer at any time and chats read the stored messages.
   - A test message that the owner confirms ends this step.
   - **A chat-only owner** gets every question and approval in the chat, with the same IDs (§3.14).
3. **Connect providers** (before any model choice).
   - Korolev detects installed and logged-in tools: `claude`, `codex`, `agy-desktop`, and others once their adapters exist. For each, it checks that the tool is on the PATH that services will use.
   - Each connection is named and records provider, transport (CLI or API, chosen per provider by the owner, §4.1) and a secret reference where needed.
   - Each connection is checked once through its real adapter inside Korolev's sandbox, not with the owner's unrestricted CLI.
   - Only transports with a shipped adapter are offered. Others appear as "coming later".
   - A provider that needs a login gets plain instructions, and setup continues without it.
   - GitHub (`gh` login, plus default visibility for new repos: private) is checked separately; it is not a model provider.
4. **Team.** A summary card shows the recommended, verified choice for each role. "change team" opens the details.
   - **coordinator:** the model and effort used for the Claude chats Korolev starts, plus an optional advisor model (Claude Code's advisor setting). The chat the owner is already in keeps its own model.
   - **builder:** one builder, defaulting to Codex `gpt-6.2-sol` at high effort (xhigh for hard packages). Claude can be the builder once its adapter supports sandboxed write runs (§4.1). Korolev says that this spends the owner's Claude limits.
   - **reviewers:** three by default, from the connected providers.
     - The same vendor as the coordinator is allowed (e.g. Fable reviewing an Opus coordinator).
     - Korolev recommends at least one reviewer from another vendor when one is connected, and says so when none is.
     - Assigning the builder's exact model as a reviewer needs an explicit confirmation.
   - **Model lists:** the curated `models.json` in the skill shows one or two recommended models per role first, then "other models" from each connection's model list. A role is enabled only when its adapter supports it and the chosen model and effort pass the check.
   - If no builder qualifies, setup is saved, and Korolev explains how to connect one.
5. **Where projects live:** `projects_root` (default `~/projects`). Each new project is either attached from an existing folder or created there (§3.4). Ask, or use the default for each project.
6. **Autonomy.** Defaults:
   - commit and push `korolev/<task>` branches;
   - ask before merging into the target branch, a live update or a deploy;
   - never money operations.
   
   "defaults" fills in values only. Setup ends with a summary that highlights every outward action (pushes, merges, deploys, repo visibility), and nothing is saved until the owner answers `save`. Granting merge or deploy without asking needs the owner's own explicit words, recorded in `DECISIONS.md` with the source. A per-project delegation (§3.13) never loosens a stricter gate, such as Task 0's exact-SHA approval or release approval.
7. **Advanced (defaults unless opened):**
   - auto-resume after usage limits;
   - Telegram quiet hours;
   - daily caps on builder runs and xhigh use;
   - the commit identity, which must be a noreply or owner-approved email;
   - sandbox on.
   
   The number of questions per message is fixed at 5 and is not a setting.

It ends with `doctor.py`, which also checks the chosen team, and a summary card. **Changes later:**
- re-run `korolev setup`;
- or send Telegram text such as "korolev: builder = sonnet 5.5". This applies globally unless it is prefixed with a project ("shop-api: builder = …"). Korolev echoes the resolved scope, connection, model and effort and saves only after confirmation.

Stored in `config.json` (connections, roles per §4.1, `coordinator`, `autonomy`, `projects_root`) and `setup.json`. **Ships in two parts:**
- **0.3.4:** language, owner channel with pairing, secrets, projects root and autonomy, with today's Codex and Antigravity roles;
- **0.3.5:** the provider connection registry and the full team card, with the adapters of §4.1.

**Tests**, with fake CLIs and a fake `models.json`:
- providers that are connected, need a login, or are missing;
- a re-run that keeps existing values;
- an interrupted setup that resumes;
- pairing with a wrong, expired or group `/start`;
- a secret never appearing in logs, config or handoffs;
- "defaults" without `save` saving nothing.

### 3.13 Project start interview: everything that needs the owner (owner decision 2026-11-26)

**This is the single interview flow.** §3.7 discovery uses it. For existing projects it runs in Task 0 after the grasp step (§3.5 step 2) and before plan approval (step 4). For new ideas it replaces §3.8 steps 1–3: the owner's first plain-words description counts as the first broad answer. Answers given in new-idea mode carry over when the project is later attached, so Task 0 asks only about gaps.

1. **Open questions first, one per message.** Korolev starts with 1 to 3 broad questions, sent one at a time, and lets the owner answer in their own words, at any length. For example:
   - "Describe the project: who is it for and what should it do for them?"
   - "How do you see it being found and sold?"
   - "What does success look like in 6 months?"
   
   For an existing project, Korolev first summarizes what it understood from the code and docs, and asks the owner to correct or add.
2. **Then targeted follow-ups,** only for what is still unknown or risky after the broad answers, the code and the docs. Areas that don't apply are skipped, e.g. marketing for an internal tool. The coverage is not limited to the product, and every area is tied to the product's use cases and goals:
   - **product:** users, jobs, use cases, scope, priorities;
   - **marketing:** audience, positioning, channels, and goals per channel;
   - **monetization:** model, pricing, payment flows. Money operations stay with the owner;
   - **operations:** where it runs, deploy and restart (procedure and approver), who else works on the repo (tools to retire or keep), budgets, where runtime secrets live (by name only);
   - **risk:** payments, sign-in, personal data, legal. These always get the deep review and owner approval. Korolev records facts and flags matters that need professional (e.g. legal) advice instead of deciding them;
   - **project overrides:** builder and effort, number of reviewers, language of specs and docs. Deploy delegation follows §3.12 step 6.
   
   Each follow-up says why it matters, which use case or goal it affects, and a suggested default. There are at most 5 per message and 3 rounds.
3. **After the rounds,** remaining ordinary unknowns are written down as `assumed`, and the owner can correct them at any time by replying "correct: <text>". Risky areas (payments, sign-in, personal data, legal, deploy) stay `unknown` and block only the packages that depend on them. A suggested default or silence never becomes an owner decision.
4. **Where answers go:**
   - Raw replies stay private in the product folder.
   - Reviewed summaries go to `PRODUCT-BRIEF.md`, `USE-CASES.md`, `ARCHITECTURE-BRIEF.md`, `MARKETING-BRIEF.md` (marketing and monetization), `RUNBOOK.md` (operations) and `DECISIONS.md`. Each line is tagged known, assumed or unknown (§3.7).
   - When an owner answer contradicts existing docs, the owner's latest answer wins, and the conflict is noted.
   - Later chats read these files instead of asking again, and later spec cycles ask only about new gaps.
   - The interview never asks for real customer records or secrets.

### 3.14 Owner questions on Telegram: IDs, long messages, replies (owner decision 2026-11-26)

Setup questions, interview questions, reviewer questions (§3.11), lessons and approvals all use one question registry: `questions.json` in the bot state directory, locked and atomic. Only the way they are presented and how each is authorized differ.

- **Unique IDs.** IDs are unique across all projects: `<project>-<batch>-<n>`, for example `shop-api-B7-3`, shown as `B7-3` with the project name in the header. Setup uses the project `setup`. Each entry records the project, batch, version, status, the allowed answers and every message ID it was sent in.
- **One broad question per message.** Batches of short questions are allowed, at most 5 per message. Choices are lettered (`B7-3 a/b/c`).
- **Length.**
  - The full rendered message is capped at about 3800 characters, including the project header and part number.
  - A batch is split only between questions, with parts numbered `1/3`.
  - A question that doesn't fit on its own gets its background in a separate message labeled with the same ID, and its answerable prompt is shortened to fit.
  - Ordinary non-question messages keep a paragraph-aware splitter.
  - Sends are journaled per part. A retry sends only the parts that weren't sent, and an answer is recorded only once.
- **How answers count:**
  - **Reply to any part of a batch:** answers the questions in that part.
  - **A message naming full IDs** (`B7-3 a, B7-5: <text>`) counts without replying, but only for open questions.
  - **A reply with no IDs** to a part holding several questions counts only with one answer per line, in order, and Korolev confirms its reading before recording. Anything unclear gets one short clarifying question.
  - **Broad open answers** may span several messages. Korolev keeps a saved draft for that one active ID, acknowledges each addition, and finalizes on "done" (in the owner's language), or when the owner names another ID. The draft survives restarts.
  - **Approvals** still need the exact decision ID and, where required, the commit SHA (§4.5). A free-form message never approves a push, merge, deploy or release.
  - **Closed or superseded questions** are never overwritten without confirmation.
  - **Edited messages:** an edit before recording replaces the draft; after recording, it is treated as a correction to confirm.
  - **Voice notes** (owner request 2026-11-26) are downloaded and transcribed on the server: a local Whisper model through `ffmpeg`, or an API transcriber only if the owner configured one. Korolev replies with what it heard.
    - A broad open answer takes the transcript right away, and the owner can correct it.
    - A choice answer is recorded after the owner confirms the transcript.
    - An approval is never accepted by voice alone.
    - Until this ships, every non-text message gets "I can only read text for now"; it is never skipped silently.
  - **Files and screenshots** get a polite "please answer in text" until supported.
- **Confirmation.** After each answer, Korolev confirms what it recorded per ID. Still-open IDs are re-listed at most once a day, or whenever the owner asks "open?".
- **Privacy.** The bot state directory is 0700 and its files are 0600. Pending questions and suggested defaults never enter `DECISIONS.md`; only confirmed answers do.
- **Tests:**
  - two projects with simultaneous batches;
  - a 3-part batch answered by replying to part 2 only;
  - one message answering questions from parts 1 and 3 by ID;
  - an unmatched reply, which gets a clarifying question;
  - a broad answer spread over three messages with a restart in between;
  - duplicate updates;
  - a partial-send retry;
  - a stale answer to a closed question;
  - a reply to a confirmation message;
  - Unicode, emoji and header length at the limit.

### 3.15 Owner channel service: talk to Korolev freely, by text or voice (owner decision 2026-11-26)

The owner can write or speak to the Korolev bot about any project, in their own words, as a reply or not. Korolev receives every message reliably, understands it, routes it to the right project, and confirms what it understood. This section supersedes the listener parts of §3.12 and §3.14 and lesson L-18 (voice). The question registry and IDs of §3.14 stay.

**1. Receiver** (`korolev-inbox.service`, a systemd user service, always on, no AI):
- the only `getUpdates` reader of the Korolev bot (an exclusive lock), owner-ID and chat filtered;
- receives messages, edited messages, voice notes, forwarded messages and button presses (`callback_query`);
- stores every update durably in `inbox.sqlite`, with the raw text, a voice file reference, reply-to, forward origin and an edit chain. The offset advances only after an fsync. Duplicates are dropped by update ID;
- downloads voice files to a 0700 folder;
- acknowledges at once ("received ✓");
- has a health heartbeat. If it is down for more than 10 minutes, a watchdog sends one notice through the VPS alerts chat. That is send-only: the alerts bot is never read;
- the coordinator chats and the interpreter read from `inbox.sqlite`, never from Telegram.

**2. Buttons first.**
- Every question with choices is sent with inline buttons. Callback data carries the question ID and the choice, so it can't be misread.
- When the project is unclear, Korolev sends project buttons.
- Approvals (push, merge, deploy, release, live update) are buttons bound to the exact decision ID and SHA. They are the only way to approve from Telegram besides typing the exact ID plus SHA (§4.5).

**3. Deterministic routing before any AI:**
- a button press → its question;
- a reply → the question or project of the replied message;
- exact IDs (`B7-3 a`) → the question registry;
- a `project: …` prefix (only known project names; `B7-3:` is never read as a project) → that project;
- `status` / `status <project>` → answered from STATE, heartbeat and the registry, with no AI.

**4. Interpreter** (only for what rules can't settle: free text, voice transcripts, multi-topic messages):
- Role `interpreter` in config (§4.1). Default: a cheap model through an existing CLI login (e.g. Codex with a small model at medium effort), chosen at setup. No API key is required.
- **Input:** the message, the active projects (name plus one-line description), the open questions per project (ID, text, allowed answers), and the last 5 exchanges. Never whole project files.
- **Output:** strict JSON only (schema-validated): `{items: [{project, type: answer|task|correction|status|approval_intent|note, question_id?, value?, text, confidence}], clarify?}`. Unknown projects or IDs, or a value outside the allowed answers, are rejected and become a clarification.
- **One message may yield items for several projects.** Each is routed separately.
- **Confirmation:** Korolev replies with what it understood, per item, with ✓ / ✗ / ✏️ buttons. Free-form items are recorded only after ✓. Items from replies, buttons or exact IDs need no confirmation.
- **`approval_intent`** never approves anything. It triggers the matching approval message with its SHA button (item 2).
- **`task`** becomes a proposed package in that project's queue (`TASKS.md`). The project's coordinator plans it, or asks about it.
- **Forwarded or pasted third-party text** is data: it is attached to an item, never treated as an instruction.
- **Fallback:** if the interpreter is unavailable (limit, login, timeout), Korolev sends project buttons and stores the message for later. Nothing is lost.
- **Cost:** the interpreter usage goes in `USAGE.jsonl` (role `interpreter`), with a daily cap.

**5. Voice:**
- Voice notes are transcribed on the server with a local Whisper model through `ffmpeg` (an optional dependency, installed with the owner's OK). An API transcriber is used only if one is configured.
- The transcript is shown to the owner ("I heard: …"), then interpreted like text, with the same confirmation rules.
- Audio files are deleted after the items are confirmed, or after 7 days.

**6. Delivery to projects:**
- Each confirmed item lands in the project's queue (`inbox` rows with a project and status).
- If the project's coordinator chat is running, it picks the item up within a minute.
- If not, the item waits. With `autostart` on, the receiver starts the project chat (the §3.3 chat link is sent to Telegram). Otherwise the item goes into the daily digest.
- Items unanswered for 24 hours are re-listed once in the digest.

**7. Privacy:** `inbox.sqlite` and the voice folder are 0600/0700. Raw messages are kept for audit (the owner can purge them); interpretations can be re-run with a better model.

**Tests**, with a fake Telegram API, a fake interpreter and a fake transcriber:
- receiver crash and restart, with no loss or duplication;
- a button press recorded exactly;
- a reply and an exact ID that bypass the interpreter;
- `B7-3: text` that is not read as a project;
- one voice note giving items for two projects, each confirmed separately;
- an interpreter returning an unknown ID, which becomes a clarification;
- `approval_intent` that does not approve, but sends the SHA button;
- a forwarded message that is never executed;
- an interpreter that is down, so the fallback buttons are used;
- `status` with no AI call;
- the watchdog notice when the receiver is down.

**Ships in 0.3.4:**
- part 1: the receiver plus deterministic routing plus buttons;
- part 2: the interpreter and voice.

**Interpreter provider order (owner question 2026-11-26):**
1. The configured `interpreter` role.
2. Else the cheapest small model of an already-connected provider: Codex (Luna-class, `--output-schema`), or Claude (`claude -p`, Haiku-class, JSON output validated by Korolev against the schema).
3. Else rules plus buttons only.

**Claude-only mode** (folded into §3.12 and §4.1): a supported configuration when only Claude is connected.
- Coordinator: Claude.
- Builder: Claude (Sonnet-class) through the `claude` adapter, sandboxed.
- Reviewers: 1–2 Claude models different from the coordinator's.
- Interpreter: Haiku-class.
- Voice: local Whisper.

Setup says plainly that every role then spends the owner's Claude limits. It suggests one free extra connection for an independent opinion: Gemini via Antigravity with a Google account. It runs more frugally:
- one reviewer for routine packages;
- three only for spec, risky and safety work.

**Review results (binding, override the text above where they differ):** see `REVIEW-ANSWERS-channel.md` in the product folder. Main points: transactional receiver, owner/private filter on every update, opaque single-use callback tokens, approvals only by token or exact ID+SHA, forwards inert until the owner replies to them, a tool-less interpreter with per-project context and secret quarantine, edits invalidate pending buttons, 30-day retention. First version: text, buttons and deterministic routing, one project per free-form message.

## 4. Stage 2

### 4.1 Tools per job (L-20261125-25; owner answers 2026-11-25)

Claude stays the coordinator and final author of specs and plans. Every other role picks a tool in `config.json`:

```json
{"roles": {
  "builder":   {"tool": "codex", "model": "gpt-6.2-sol",  "effort": "high"},
  "astra":     {"tool": "codex", "model": "gpt-6.2-astra", "effort": "xhigh"},
  "sol56":     {"tool": "codex", "model": "gpt-5.6-sol",  "effort": "xhigh"},
  "hydrogen":  {"tool": "antigravity", "model": "gemini-4-hydrogen-xhigh"}
 },
 "reviewers": ["astra", "sol56", "hydrogen"]}
```

- **Adapter contract** (`scripts/adapters/<tool>.py`): prompt in, final text out to a file, usage parsed into `USAGE.jsonl`, exit codes `0` ok, `75` limit (with `retry_after` when known), `76` needs owner action (login), other = failure; a wall-clock timeout and a no-output timeout enforced by the launcher; runs inside Korolev's sandbox.
- **Adapters:** `codex` (existing), `antigravity` (`agy-desktop -p … --model … --output-format json`; binds the desktop session bus into the sandbox; needs an active desktop login, else exit 76), `claude` (`claude -p --output-format json`; reviewer, and builder once sandboxed write runs pass the contract test). API adapters (OpenAI, Gemini) are optional and used only when the owner configures a key; per provider the owner chooses API or CLI. No Kilo.
- **Defaults:** three reviewers when connected (Astra xhigh, Sol 5.6 xhigh, Gemini 4 Hydrogen xhigh); routine reviews use `gemini-4-hydrogen-high`. Reviewers from the coordinator's vendor are allowed (owner decision 2026-11-26; e.g. Fable reviewing an Opus coordinator). Korolev recommends at least one reviewer from another vendor when one is connected (§3.12). `opinions.py` runs all configured reviewers in parallel and proceeds with the ones that answered, noting the missing ones.
- `onboard.py` and `doctor.py` list providers, test each with a one-line call, and ask the owner which to connect.
- Contract test per adapter with a fake binary (success, limit, login needed, hang, garbage output).

### 4.2 Provider alerts (owner answer 1A, 2026-11-25)

Adapters classify a failed call as `limit`, `login_needed`, `not_responding` (timeout or no output) or `crashed`. The launcher sends one Telegram alert per provider per incident (cooldown 6 h for the same class), and one "back to normal" message when the next call to that provider succeeds. State in `$KOROLEV_HOME/providers.json`. No model polling: alerts come from launcher exits.

### 4.3 `verify.py` and reviews bound to a revision

- `verify.py --product P --package NN-slug` runs the Phase 2 step 3 checks mechanically (new commit on the base, paths within allowed globs, clean tree, fast checks rerun with exit codes compared, secret scan over every new blob and commit message, new test files collected, `testcount.py`, `trace.py`) and writes `VERIFY.json` (`PASS` | `FAIL` | `NEEDS_HUMAN`, reasons, ≤10-line summary). The coordinator reads only that file and the handoff summary.
- Every opinion file records the SHA-256 of each reviewed file or the reviewed commit, the reviewer's verdict and coverage. Merging rejects stale opinions (reviewed digest ≠ current). Fix/review rounds are capped at 2; then unresolved findings go to the owner.
- Sensitive packages (money, auth, security; `sensitive_paths` in `product.json`) need a review `PASS` bound to the exact commit before any push.

### 4.4 Partial handoffs and per-attempt cost

- `HANDOFF.md` starts with a status line: `done`, `partial`, `blocked` or `failed`, plus blockers and the last command tried. Only `verify.py` issues PASS. A worker that claims done and exits non-zero is recorded as failed.
- `USAGE.jsonl` records product, package, attempt number, role, tool, model, input, cached input, output and reported cost (unknown stays unknown, never 0). `usage.py report --product P` sums per package and attempt.

### 4.5 Decision-bound Telegram answers and install test

- Each question batch gets a decision ID; `notify.py` stores the sent `message_id`. An answer counts only as a reply to that message; release approvals also carry the commit SHA. Tests: two pending batches, a late reply, a reply to an old message, a timeout (decision stays pending, other work continues).
- Test that Claude and Codex each discover exactly one `korolev` skill in an isolated home and resolve the same scripts.

### 4.6 Parallel lanes: several builders and reviewers at once [safety] (owner request 2026-11-26; research: research/parallel-sol56.md)

**Option, off by default, per project** (`config.json` `parallel`, overridable per project). With it off, Korolev runs exactly as today: one builder at a time. Parallel work is a bounded **package scheduler** run by the coordinator, not a free-form agent swarm. Builders never talk to each other or spawn further agents.

**Planning.**
- `PLAN.md` packages become a persisted graph (`plan.json`). Each package records `depends_on`, `claims` (normalized allowed paths), `resources` (named shared things such as `migrations`, `auth`, `money`, `lockfile`, `ci`, `release`, `schema:<name>`, `api:<name>`), `exclusive` and `hard` (best-of-N candidate).
- Only packages with no unfinished dependencies are `ready`.
- `exclusive` is set automatically when resources include migrations, auth or permissions, money, releases, dependency lockfiles, CI config or shared generated files. The owner can add more per project.
- Dependency-dense refactors are planned as a sequence, not split into parallel parts.

**Admission** (`lanes.py`, the only way a builder starts in parallel mode). A ready package is admitted only when all of these hold:
1. its path claims don't intersect any active claim. Parent and child paths count as intersecting, and symlinks and `..` are resolved first;
2. none of its resources is held by an active lane, and an exclusive package runs alone;
3. lane caps allow it: per project (default 2 builders), host-wide (default 4), per provider (default 2, with one slot reserved for the coordinator's reviews);
4. the host has headroom: load, free RAM, free disk and PIDs are above thresholds, checked at admission and every minute while lanes run;
5. **backpressure:** the review queue and the merge queue are below their WIP caps (default 2 each), because finishing integration comes before starting new work;
6. the daily budget (runs and xhigh minutes per provider) allows it.

Claims and resource holds are stored atomically in `$KOROLEV_HOME/lanes/<project>.json` under the repository lock, then released only at a terminal state or by heartbeat expiry (a stale lease with a dead process).

**Each lane:**
- runs in its own task worktree and branch (§3.4, package 19), its own outbox, and its own isolated Codex home;
- is identified by a stable lane ID used in every name: branch `korolev/<NN-slug>`, test ports from a per-lane range, test database and schema names, cache, temp and container names. These are given to the worker as environment variables (`KOROLEV_LANE`, `KOROLEV_PORT_BASE`, `KOROLEV_TEST_DB`);
- runs in a killable process group with CPU and memory limits (`systemd-run --user --scope` when available, else `prlimit`). All of it is torn down on any terminal state;
- has the launcher verify the worktree identity (path, common dir, branch, base commit) at start and again before commit;
- **enforces claims at commit:** any changed path outside the lane's claims fails verification (exit 4), as `.korolev/` and credential paths already do. This check also applies in serial mode.

**State machine per package**, journaled with idempotency keys for spawn, claim, commit, merge and Telegram actions: `queued → claimed → running → committed → reviewing → integrating → done`, plus `paused` (usage limit, owner question, host pressure), `failed` and `cancelled`. Git commits are the recovery checkpoints. After a crash Korolev reads the journal and refs and continues; it never re-spawns a finished step or imports unknown uncommitted work into another lane.

**Reviews:** read-only reviewers fan out against the lane's immutable commit, up to the reviewer cap. Risky packages get reviewers split by aspect (security, correctness, tests).

**Integration: one serial merge queue per project** (bors-style "not rocket science"):
1. Take the oldest reviewed lane commit.
2. Rebase it, or build a merge, onto the latest accepted tip of the **integration branch** `korolev/integration`. The owner's branch stays untouched until the existing approval step (§3.5 step 6, release approval).
3. Rerun the package's tests and the whole-project suite.
4. Fast-forward the integration branch to the tested commit.

A conflict or a red test ejects the lane back to `failed` with the reason. Korolev never auto-resolves a conflict; it re-plans that package as a repair package. Several candidate "good parts" are never merged together automatically.

**Retries are bounded and typed:**
- a transient tool error gets at most 2 retries with backoff;
- a failing test goes back as a repair package (max 1, then the owner is told);
- a usage limit pauses the lane without using up a retry, until the provider recovers;
- the builder model is never silently swapped for a different one.

**Best-of-N** (opt-in per package with `hard: true`, and only when the package has an executable verifier):
- `N=2` by default, and different models are allowed;
- reviewers and the verifier pick one commit and the rest are discarded;
- its budget is shown before starting.

**The coordinator stays one Claude chat.** It reads lane HANDOFF and VERIFY summaries only, never transcripts.

**Telegram:** blocked lanes, owner questions and approvals are sent immediately. Progress goes out as one combined summary at most every 15 minutes while lanes run.

**Tests**, with fake builders:
- overlapping claims are refused, and so are parent/child paths and symlink escapes;
- two exclusive packages never run together;
- a resource conflict waits;
- caps and backpressure are respected;
- the host-pressure threshold stops admission;
- a lane writing outside its claim fails at commit;
- the per-lane port and database environment is unique;
- the merge queue ejects a lane whose test fails after rebase, and the next lane proceeds;
- a crash mid-integration resumes without a double merge;
- a usage-limit pause doesn't consume a retry;
- best-of-N picks one candidate and discards the other;
- teardown leaves no orphan processes.

Ships as 0.3.x after Task 0 (stage 2).

**Review results (binding, override the text above where they differ):** see `REVIEW-ANSWERS-parallel.md`. Main points: a host-wide SQLite ledger, cgroups v2 required (no prlimit fallback), a lane lease required for every write launch, attempt IDs, orphan quarantine, merge-candidate integration with CAS update-ref, lexical claims, separate builder and reviewer quotas. Best-of-N and aspect reviewers are cut from the first version. Admission reads host pressure from resource-timeline (`/var/log/resource-monitor`) where available.

## 5. Stage 3

### 5.1 Self-running learning (research: self-learning report, Adopt 1–9)

- `lesson.py observe` records signatures from run failures, timeouts, empty outputs, `testcount`/`verify` failures, accepted reviewer findings and owner corrections (called by scripts, not remembered by the model). Three occurrences with distinct run IDs create one pending lesson.
- Lessons gain evidence fields: pattern key, occurrence count, first/last seen, up to 5 evidence pointers, target file, enforcement (`test|script|template|prose`; prose needs an owner-visible reason), base and candidate commits.
- Every applied lesson adds a regression check that fails on the recorded defect and passes on the fix. `resolve … applied` is refused without it.
- Replay gate: `lesson.py validate` compares baseline and candidate on the recorded reproducer (1 run for deterministic checks, 3 for agent tasks with identical model, effort and budget).
- Dedup by SHA-256 of the normalized signature; rejected signatures suppress repeats.
- `lesson.py prune`: pending lessons not seen again expire after 30 days; approved but unbuilt ones return to pending after 90 days; at most 25 pending; safety and applied lessons never expire.
- `propose --limit 10` sends only title, count, target, enforcement and up to 3 evidence pointers; `lesson.py show ID` gives full detail.
- Usefulness counters (`helped`, `harmful`, `last_fired`) updated only from check results, never self-reported.
- Rollback: `resolve ID rolled_back --commit SHA`; a regression creates a normal one-commit revert package.

### 5.2 One-command install (L-20261125-18)

The repo becomes a Claude Code plugin and marketplace (`.claude-plugin/plugin.json`, `marketplace.json`): `/plugin marketplace add geenes/korolev`, then `/plugin install korolev`. The plugin carries the skill and the guard hook. `install.sh` stays for Codex, the `korolev` launcher and the systemd resume units. The install test covers both paths.

## 6. Tests and gates (all stages)

- `python3 -m unittest discover -s tests` passes locally, in a shallow clone, and without bubblewrap.
- Each package adds failing-first tests for its rule; `testcount.py check --base` shows no drop in collected tests.
- Safety packages: §3.1, §3.2, §3.4, §3.5, §3.6. Each gets an Astra or Sol 5.6 code review bound to its commit before it is accepted.

## 7. Skill text changes

SKILL.md gains: `korolev attach` and Task 0 as the start for every existing project; the `waiting_owner` heartbeat rule; `korolev chat` as the way to start or hand over a chat; discovery steps at the start of Phase 1 and the new-idea entry; `verify.py` replacing the hand-run checks (stage 2); the tool-role table replacing the Codex-only model table (stage 2); the learning loop's `observe`/`validate` steps (stage 3). Phase 3 step 3 already says releases go through `release.py`.

## 8. Out of scope for this revision

Linked-repo pairs and paired tags (each repo of a pair is attached on its own until then), `stand.py`, clone bundles, Kilo, automated deploys, multi-host orchestration, web UI, parallel workers on one repo, automatic model escalation, a Gemini API key (owner decision: Antigravity only).
