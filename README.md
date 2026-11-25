# Korolev: a Claude Code skill for running your AI coding team

**A Claude Code skill that makes Claude the chief designer of your AI coding team.** Claude plans, checks and decides. Codex reviewers argue about the plan, Codex builds, tests and CI are verified for real, and a Telegram bot asks you only the decisions that are truly yours.

[![Release](https://img.shields.io/github/v/release/geenes/korolev)](https://github.com/geenes/korolev/releases) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Agent Skills](https://img.shields.io/badge/Agent_Skills-Claude_Code_%7C_Codex-blueviolet)](skill/SKILL.md) [![tests](https://github.com/geenes/korolev/actions/workflows/ci.yml/badge.svg)](https://github.com/geenes/korolev/actions)

> Tired of AI coding agents that drift, burn through your limits, report "all green" on tests that never ran, and leave a week of work sitting in `/tmp`? Same here. Korolev is the approach we switched to.

## Who is this for?

- **Founders and product owners** who build with AI agents and want progress without babysitting a terminal. You answer a few plain questions on your phone; the rest moves on its own.
- **Developers running Claude Code and Codex together** who want one coordinator, independent second opinions, and proof instead of claims.
- **Anyone tired of paying for tokens twice:** long agent chats re-read their own history on every step. Korolev keeps every job small and fresh.

## What it does

| | |
|---|---|
| **Onboards any repo from a link** | Clones it into its own workspace (your checkout is never touched), inventories the code, specs and agent setup, and has Codex write a precise analysis. |
| **Builds or fixes the plan** | Adopts a good existing plan and corrects it, or writes a new one, split into small, checkable packages. |
| **Second and third opinions** | Two different Codex models (Astra and Sol 5.6, extra-high reasoning) review the plan against the real code, independently. |
| **Asks you only real decisions** | Plain-language questions on Telegram, each with options, a recommendation and how to answer. Your reply wakes the coordinator. |
| **Hands the hard work to Codex** | One fresh Codex session per package, in a sandbox that can't see your other projects or secrets. |
| **Verifies for real** | Proves tests actually ran (no silent skips), waits for CI on the exact commit, and checks every change before accepting it. |
| **Learns from its mistakes** | Each mistake becomes a concrete improvement proposal. You approve it, Korolev builds it into itself, and publishes a new version, again only after you approve. |
| **Keeps going when you run out of usage** | Optional: a small watchdog waits for your usage limit to reset, then continues the work and tells you on Telegram. Pushing and publishing still wait for you. |
| **Cheap by design** | No polling, small contexts, a clean Codex setup for workers, and token use measured per job. |

## How it works

```
                 you (the owner)
                  ▲  decisions only, via Telegram
                  │
        ┌─────────┴──────────┐
        │  Claude: chief     │  plans packages, verifies, pushes, keeps state in files
        │  designer          │  wakes up only when a job finishes, never polls
        └───┬───────────┬────┘
   reviews  │           │  one package at a time, fresh session each
            ▼           ▼
   Astra xhigh       Codex Sol: the builder
   Sol 5.6 xhigh     writes code, runs checks, hands off a short report
```

## Install (once per machine)

Requirements: [Claude Code](https://claude.com/claude-code), the [Codex CLI](https://github.com/openai/codex), git, Python 3.10+. Optional: the GitHub CLI (`gh`) for private repos, `bwrap` (bubblewrap) for the worker sandbox, and a Telegram bot.

```bash
git clone https://github.com/geenes/korolev && cd korolev
./install.sh                    # links the skill for Claude Code and Codex, runs the preflight check
# optional: your own Telegram bot from @BotFather
echo "<bot token>" | python3 skill/scripts/setup_telegram.py   # then send /start to your bot
```

One install covers any number of projects.

## Use it

Start Claude Code in `~/korolev` and type:

> korolev https://github.com/you/your-repo

Korolev onboards the repo, writes or corrects the plan, gets two independent reviews, sends you the product questions, and stops for your go-ahead before building. Each project lives in `~/korolev/products/<name>/` (state, decisions, plan, reviews) with its own clone in `~/korolev/work/<name>/repo`.

Settings live in `~/.config/korolev/config.json`, for example `{"language": "en"}` (default English, also `ru`), `{"worker_sandbox": true}` and `{"isolate_codex_home": true}` (both on by default). Unattended resume is off unless you install with `./install.sh --with-resume`.

## Why: lessons paid for in real money

Korolev came out of one long day running two linked production projects with AI agents. Every rule exists because its absence hurt:

| What went wrong | What Korolev does |
|---|---|
| Agents re-read about 160k tokens of their own chat on every call | A fresh session per package, with state in small files |
| A week of work lived only in `/tmp` clones | Verified work is committed and published only through an approved release |
| CI was "green" but had never run a single test | Gates must prove tests ran: collected, passed and skipped counts |
| Integration tests silently skipped for want of one env var | Skip limits, and new test files must be collected |
| Tests in a scratch clone wrote into the real checkout | Workers run in a sandbox that sees only their project |
| Owner decisions got lost in a chat | `DECISIONS.md`, dated and binding |

## What's inside

`skill/SKILL.md` holds the coordinator procedure. `skill/scripts/` holds small standard-library Python tools: `onboard`, `run_codex`, `opinions`, `notify`, `inbox`, `lesson`, `release`, `privacy_scan`, `testcount`, `ci_wait`, `procs`, `sandbox`, `handoff`, `heartbeat`, `resume_watch` and `doctor`. There are no frameworks and nothing to host. See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Why "Korolev"?

Sergei Korolev was the chief designer of the early Soviet space program. He never flew; his job was to make sure everything that did fly had been checked, argued over and tested on the ground first. That's the job description.

## Contributing

War stories welcome. If your agents ever lied about green tests, ate your token budget or lost your work, open an issue and tell us how. Ideas, harness adapters (other CLIs and chat apps) and test fixtures are all welcome.

## License

MIT. See [LICENSE](LICENSE).
