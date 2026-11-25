# Korolev 🚀

**One chief designer for your AI coding crew.** Claude orchestrates, Codex Astra and Sol 5.6 argue about the spec, Codex Sol 6.2 does the heavy lifting, and a Telegram butler pings you only when a human really has to decide.

> Tired of AI coding agents that drift, burn through your limits, report "all green" on tests that never ran, and leave a week of work sitting in `/tmp`? Same here. Korolev is the approach we switched to.

## The idea in one picture

```
                 you (the owner)
                  ▲  decisions only, via Telegram (Butler bot)
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
- **Butler is the polite one.** When something truly needs you, such as a price, a limit or a product rule, you get one batched Telegram message with options and a recommended default. Everything else keeps moving.

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

## What's inside (v0.1, in progress)

- `skill/SKILL.md`: the coordinator procedure. It works as a Claude Code skill and as a Codex skill.
- `dispatch.py`: launches one worker run (Codex, Kilo or Claude) with a timeout, a structured handoff and token accounting.
- `verify.py`: mechanical acceptance. The commit is real, only allowed paths changed, no secrets, gates rerun independently, tests actually executed.
- `promote.py`: integration branch first, fast-forward main only on a verified full gate, matching tags for paired repos.
- `notify.py`: Telegram send, batched questions, and your replies collected into an inbox.
- `stand.py`: a polite heavy-test stand for shared servers, with a lock, a daily run budget, a scratch quota and a disk floor.

Standard library Python, no framework, nothing to host.

## Status

🧪 **Spec stage.** [`SPEC.md`](SPEC.md) is being reviewed by Astra xhigh and Sol 5.6 xhigh, then Sol 6.2 builds it. The first real user is a two-repo product (an SEO data worker plus a customer cabinet). Expect rough edges and fast changes.

## Why "Korolev"?

Sergei Korolev was the chief designer of the early Soviet space program. He never flew; his job was to make sure everything that did fly had been checked, argued over and tested on the ground first. That's the job description.

## Contributing

War stories welcome. If your agents ever lied about green tests, ate your token budget or lost your work, open an issue and tell us how. Ideas, harness adapters (other CLIs, other chat bots) and test fixtures are all very welcome.

## License

MIT. See [LICENSE](LICENSE).
