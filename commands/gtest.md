---
description: Sweep this repo's tests — what they miss, which kinds are absent, which assert nothing, which are flaky
---

Use the `coverage-gaps` skill to sweep this repository's tests and print the
diagram in the terminal.

Arguments passed after the command, if any: $ARGUMENTS

Recognised arguments:

- (none) — scan the test kinds and the tests that assert nothing, then offer
  to measure coverage
- `--diff [ref]` — only report gaps on lines this branch changed
- `--flaky [n]` — run the suite n times (default 3), compare outcomes, and
  list the slowest tests
- `--fix` — after reporting, write tests for the top three gaps
- `--all` — scan, coverage and flaky in one pass

Follow the skill exactly. Its confirmation gate before running a test suite is
not optional: show the command and wait for an answer.
