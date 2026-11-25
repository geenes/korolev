Analyze this repository as a senior engineer who must hand a precise picture to a coordinator who will NOT read the code. Read-only: do not modify files, do not run tests that write outside /tmp, do not install anything, and do not use the network.

Cover, with file paths as evidence:
1. Purpose and users: what the product does, in five lines.
2. Architecture: main components, entry points, data stores, external services and integrations, and how they connect.
3. Build, run and test: the exact commands, the runtime versions, and whether tests and CI look healthy (read the CI config and the test layout). Mark anything you could not verify as UNVERIFIED.
4. Specs, plans and docs: list each one with its path. Give a verdict for each: authoritative, partially stale or obsolete. Say whether there is a usable implementation plan already, and its quality.
5. Code quality and risks: hot spots, missing tests, security-sensitive areas (money, auth, secrets, personal data), and obvious bugs or dead code.
6. Gaps: what the specs promise that the code does not do, and what the code does that no spec covers.
7. Suggested next packages: 5 to 10 small, verifiable units of work, in order, each with acceptance checks.
8. Questions only the product owner can answer, each with options.

At most 200 lines. No preamble. Prefer tables and bullets.
