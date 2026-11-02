---
name: coverage-gaps
description: >
  Scan a repository's tests and draw one diagram: which code the tests miss,
  which kinds of test are absent, which tests assert nothing, and which tests
  disagree with themselves. Use for /gtest, /testsweep, /sweep, and whenever the user
  writes or changes a test, asks "is this tested", "what should I test next",
  "test coverage", "coverage gaps", "which tests are flaky", "do we have e2e
  tests", "are these tests any good", or finishes implementing something and
  needs to know what is still untested.
---

# Sweep the tests

Five scripts live beside this file. They parse and draw; none of them runs a
test suite and none of them writes code.

| Script | Answers |
|---|---|
| `stacks.py` | which runners exist, tests per kind, tests that assert nothing |
| `rank.py` | which uncovered code matters most |
| `pysource.py` | Python: which function owns a line, does a test assert, does it run |
| `jssource.py` | JavaScript and TypeScript: the same questions, plus `it.only` |
| `flaky.py` | which tests disagree with themselves, and which are slowest |
| `mutate.py` | small wrongnesses your tests ought to notice |
| `diagram.py` | all of it as one picture in the terminal |

## Scan straight away

The scan reads files and runs nothing. Do not ask permission for it and do not
announce it as a step — just do it, first thing:

```bash
mkdir -p .gtest
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/stacks.py" --json \
  > .gtest/stacks.json
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/diagram.py" \
  --stacks .gtest/stacks.json
```

That diagram already answers most of it: tests per kind, the kinds sitting at
zero, and every test that executes code without checking anything. Show it.

Two findings here matter more than any percentage, so say them in words as
well as in the diagram:

- **a kind at zero** — a repo at 90% line coverage with no end-to-end test is
  not well tested, and only this scan can see that
- **tests that assert nothing** — they raise the coverage number and catch
  nothing. Name them and say what each one should assert.
- **a focused test** — one `it.only` leaves every other test in its file idle
  while CI reports green. Say this first; it is the loudest finding there is.
- **tests that never run** — a skip with no reason is one nobody comes back to.
- **a faked backend that cannot fail** — msw with `onUnhandledRequest` not set
  to `'error'` passes a call to a route that no longer exists. Without
  `resetHandlers()` in `afterEach`, one test's override leaks into the rest.

If no runner was found, stop there. There is nothing to measure.

## Running the suite needs an answer

**Never run a test suite without showing the command and waiting.** A suite can
seed a database, send requests, spend money or take an hour. This is a
requirement of this skill, not a courtesy.

Show the `command` from the scan and ask. If the answer is no, the scan above
was still a real answer — leave it there.

If the runner's coverage package is missing, print its `install` string and let
the user install it. Never install anything yourself. Where the scan reports a
`setup` note (playwright and cypress need a reporter or instrumentation before
they emit any coverage at all), show that note too, or the run produces an
empty report and the numbers lie.

## Coverage

Run the confirmed command, then:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/rank.py" \
  --coverage <report> --json > .gtest/ranked.json
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/diagram.py" \
  --stacks .gtest/stacks.json --coverage .gtest/ranked.json
```

`<report>` is the `report` path from the scan: `coverage.json` for pytest,
`coverage/coverage-final.json` for vitest, jest, playwright and cypress,
`coverage.out` for Go.

Add `--diff <base>` to restrict the report to lines this branch changed. That
is the right default when reviewing a change rather than auditing a repo.

For each top gap, give **one concrete test to write**: name it after the
function the diagram shows and the behaviour, and say what it should assert.
`test_expire_returns_true_past_deadline` is useful; `test_expire` is not.

Say once that coverage measures execution, not assertion quality.

## Flakiness, when asked

Several runs of the suite, so it needs its own answer. Show the cost first:
three runs of a four-minute suite is twelve minutes.

```bash
for i in 1 2 3; do <runs_command with out=.gtest/run$i.xml>; done
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/flaky.py" \
  .gtest/run*.xml --json > .gtest/flaky.json
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/diagram.py" \
  --stacks .gtest/stacks.json --coverage .gtest/ranked.json \
  --flaky .gtest/flaky.json
```

The diagram separates four things, because each needs a different fix:

- **flaky** — same code, different answers. The quarantine block gives the
  deselect arguments; say plainly that quarantine is temporary.
- **always failing** — a real bug or a stale test. Never quarantine these.
- **not in every run** — the collection changed; usually a conditional skip or
  an import error.
- **slowest** — the worst run of each test. Often the cheapest CI win there is.

## Mutation, only when asked

Coverage says a line ran. It cannot say the line was checked. A mutant answers
that: break the code on purpose, run the suite, and see whether anything
fails. A mutant that survives is a line the tests execute and do not verify.

**Each mutant costs one full test run**, so the default is two. Say the cost
before asking: two mutants on a four-minute suite is eight minutes.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/mutate.py" \
  --file <the worst-ranked file> --limit 2
```

That proposes and changes nothing. Then, for each mutant, one at a time:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/mutate.py" \
  --file <file> --apply <n>
<the confirmed test command>
python3 "${CLAUDE_PLUGIN_ROOT}/skills/coverage-gaps/mutate.py" \
  --file <file> --restore
```

**Restore after every single one, before applying the next.** `--apply` is the
only write anywhere in this plugin. It refuses unless the file is committed and
unmodified, so restore can always be exact, and it puts the file back itself if
the write touched more than the one line.

If the suite **fails**, the mutant was killed and that line is genuinely
tested. If it **passes**, say so plainly: that line runs in the tests and
nothing checks it, and name the assertion that is missing.

Pick the files to mutate from the ranked gaps, not at random. Raise `--limit`
only if the user asks for it.

## `--fix`, only on the explicit flag

Write tests for the **top three** gaps, no more, and for any test that asserts
nothing, add the assertion it is missing rather than a new test beside it.

Read a neighbouring test file first and mirror its imports, fixtures and
assertion style. Run what you write. It must pass. Then say which gaps are
still open.

Never weaken a test to make it pass, never add an assertion-free test (that is
the thing this plugin reports), and never change the code under test to fit a
test.

## What this skill will not do

- run a suite before the user answers
- write to any file except through `mutate.py --apply`, which is always
  followed by `--restore`
- install packages, or wire up a reporter on its own
- generate code from any of the scripts
- treat a coverage percentage as a measure of test quality
- quarantine a test that fails every single run
