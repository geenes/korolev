# Korolev 🚀

**One chief designer for your AI coding crew.** Claude orchestrates, Codex Astra and Sol 5.6 argue about the spec, Codex Sol 6.2 does the heavy lifting, and a Telegram bot pings you only when a human really has to decide.

> Tired of AI coding agents that drift, burn through your limits, report "all green" on tests that never ran, and leave a week of work sitting in `/tmp`? Same here. Korolev is the approach we switched to.

## The idea in one picture

```
                 you (the owner)
                  ▲  decisions only, via Telegram (Korolev bot)
                  │
        ┌─────────┴──────────┐
        │  Claude: chief     │  plans packages, verifies, pushes, promotes, keeps state
        │  designer          │  wakes up only when a run finishes, never polls
        └───┬───────────┬────┘
   specs &  │           │  one package at a time, fresh session each
   second   ▼           ▼
   opinions Astra xhigh   Sol 6.2: the builder (high / xhigh)
            Sol 5.6 xhigh writes code, runs checks, commits, hands off a JSON report
```

- **Claude is the orchestrator.** It writes the task for each work package, launches a worker, and goes to sleep. When the run ends, it reads a one-screen verification report, not the whole transcript. Then it pushes, promotes, or sends a fix.
- **Astra and Sol 5.6 are the consultants.** Every new spec gets two independent reviews at extra-high reasoning. They disagree, Claude merges, and the spec gets better before any code exists.
- **Sol 6.2 is the builder.** Each package runs in a brand-new session that gets a short task and a one-page state file instead of a 200k-token history, so it's sharp and cheap.
- **The Korolev bot is the polite one.** When something truly needs you, such as a price, a limit or a product rule, you get one batched Telegram message with options and a recommended default. Everything else keeps moving.

## Why: lessons paid for in real money

Korolev came out of one long day running two linked production projects with AI agents. Every rule here exists because its absence hurt:

| What went wrong | What Korolev does |
|---|---|
| Agents re-read ~160k tokens of their own chat on every call | Fresh session per package, state in small files |
| The coordinator kept "checking in" | No polling: background runs, wake on exit |
| A week of work lived only in `/tmp` clones | Verified work is pushed right away, with clone backups |
| CI was "green" but had never run a single test | Gates must prove tests ran: collected, passed and skipped counts |
| Integration tests silently skipped for want of one env var | Skip budgets; new test files must be collected |
| Tests in a scratch clone wrote into the real checkout | The original checkout is fingerprinted before and after every run |
| Two repos that call each other were released separately | Pairs: linked repos are promoted, tagged and deployed together |
| Owner decisions were lost somewhere in a chat | `DECISIONS.md`, dated and binding |

## Quick start

```bash
git clone https://github.com/geenes/korolev && cd korolev
./install.sh                     # once per machine: links the skill for Claude Code and Codex, runs the preflight check
# optional, a Telegram owner channel with your own dedicated bot from @BotFather:
echo "<bot token>" | python3 skill/scripts/setup_telegram.py   # then send /start to your bot
```

Then, in any Claude Code session:

> korolev https://github.com/you/your-repo

Korolev clones the repo into its own workspace (your checkout is never touched) and inventories it. Codex Sol then analyzes the code and specs. Claude adopts and corrects the existing plan, or writes one. Astra and Sol 5.6 review it in parallel, and the product questions go to your phone. Building starts when you say go.

One install handles any number of projects. Each one gets its own `~/korolev/products/<name>/` (STATE, DECISIONS, PLAN, questions, opinions) and `~/korolev/work/<name>/repo`.

## What's inside (v0.1)

| Script | Job |
|---|---|
| `doctor.py` | preflight: Codex, gh, models, disk, Telegram, skill links |
| `onboard.py init <repo>` | clone (gh for private repos), state folder, `INVENTORY.md` |
| `run_codex.py --role …` | one fresh ephemeral Codex session per job; read-only or write; timeout; usage log |
| `opinions.py` | Astra xhigh and Sol 5.6 xhigh in parallel, for second and third opinions |
| `notify.py` / `inbox.py` | owner questions out, answers in, owner-only, routed per project, single locked reader |
| `setup_telegram.py` | two-minute bot connection wizard |

Standard library Python, no framework, nothing to host. The full verification and promotion machinery (`verify.py`, `promote.py`, pairs) is specified in [`SPEC.md`](SPEC.md) and comes next.

## Why "Korolev"?

Sergei Korolev was the chief designer of the early Soviet space program. He never flew; his job was to make sure everything that did fly had been checked, argued over and tested on the ground first. That's the job description.

## Contributing

War stories welcome. If your agents ever lied about green tests, ate your token budget or lost your work, open an issue and tell us how. Ideas, harness adapters (other CLIs, other chat bots) and test fixtures are all very welcome.

## License

MIT. See [LICENSE](LICENSE).
