# Task NN-slug: <goal in one line>

- Product: <name> · Repo: the Korolev clone (working directory)
- Base commit: <sha> · Work branch: <branch>
- Allowed paths: <globs>; anything else is out of scope
- References (read, don't copy): <spec/plan paths, commits, DECISIONS entries>

## Do
1. …

## Acceptance checks (you run them; the coordinator reruns them)
- `<command>` → exit 0, with N tests collected and run (not skipped)

## Stop rules
- No push, tag, deploy, paid API, real data or edits outside allowed paths.
- If blocked or something is unclear, stop and report; do not guess product decisions.

## Report (your final message, at most 60 lines)
- Files changed (do not commit: the launcher commits your working-tree changes)
- Commands run, with exit codes and test counts (collected/passed/failed/skipped)
- What remains, risks, and anything you could not verify (say so plainly)
