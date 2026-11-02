#!/usr/bin/env python3
"""Draw the sweep as one diagram in the terminal.

Three JSON files go in, one picture comes out: which kinds of test exist,
how covered each file is, the worst gaps with the functions that own them,
and which tests disagree with themselves. No HTML, no browser, nothing to
open -- it prints where you ran it.

    python3 diagram.py --stacks stacks.json --coverage ranked.json \
        --flaky flaky.json
    python3 diagram.py --self-check

Values arrive from a coverage report, which is a file on disk and can hold
anything, so every one of them is clipped to a single printable line.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BAR = 22
LABEL = 15
WIDTH = 96
MAX_GAPS = 8
MAX_FUNCS = 4

# Eighth-blocks let a 22-cell bar show a percentage without rounding to 5%.
FULL = "█"
EIGHTHS = (" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
TRACK = "·"

KIND_ORDER = ("unit", "integration", "smoke", "e2e", "performance", "contract")


def clean(value: object, limit: int = 120) -> str:
    """One printable line. Control characters in a file path stay out of the terminal."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pad(value: object, width: int) -> str:
    text = clean(value, width)
    return text.ljust(width) if len(text) <= width else text[: width - 1] + "…"


def bar(fraction: float, width: int = BAR, empty: str = TRACK) -> str:
    """A proportional bar. Never wider than width, never narrower than nothing."""
    fraction = min(max(fraction, 0.0), 1.0)
    eighths = round(fraction * width * 8)
    whole, rest = divmod(eighths, 8)
    drawn = FULL * whole + (EIGHTHS[rest] if rest else "")
    return (drawn + empty * width)[:width] if drawn else empty * width


def kinds_block(stacks: dict) -> list[str]:
    kinds = stacks.get("kinds") or {}
    if not kinds:
        return []
    peak = max((row.get("tests", 0) for row in kinds.values()), default=0)
    lines = ["TEST KINDS" + " " * (LABEL + BAR - 8) + "tests  files"]
    for kind in KIND_ORDER:
        row = kinds.get(kind)
        if row is None:
            continue
        tests, files = row.get("tests", 0), row.get("files", 0)
        drawn = bar(tests / peak if peak else 0.0)
        state = row.get("state", "covered" if tests else "gap")
        flag = {"gap": "   <- gap", "not applicable": "   n/a"}.get(state, "")
        lines.append(f" {pad(kind, LABEL)}{drawn} {tests:>5}  {files:>5}{flag}")

    missing = stacks.get("missing_kinds") or []
    skipped = stacks.get("not_applicable") or []
    if missing:
        lines.append("")
        for kind in missing:
            row = kinds.get(kind) or {}
            why = ", ".join(row.get("evidence") or ()) or "the repo layout"
            lines.append(f" No {clean(kind, 20)} tests, and this repo wants them:")
            lines.append(f"   {clean(row.get('because', ''), 70)}")
            lines.append(f"   evidence: {clean(why, 60)}")
    if skipped:
        lines += ["", f" Not expected here, so not counted against it: "
                      f"{clean(', '.join(skipped))}"]
    return lines


def files_block(coverage: dict) -> list[str]:
    gaps = coverage.get("gaps") or []
    if not gaps:
        return []
    lines = ["COVERED PER FILE"]
    for row in gaps[:MAX_GAPS]:
        statements = row.get("statements") or 0
        missed = len(row.get("missed_lines") or ())
        share = (statements - missed) / statements if statements else 0.0
        name = Path(clean(row.get("file", "?"))).name
        lines.append(f" {pad(name, LABEL)}{bar(share)} {share * 100:>5.1f}%")
    return lines


def gaps_block(coverage: dict) -> list[str]:
    gaps = coverage.get("gaps") or []
    if not gaps:
        return ["WORST GAPS", " No gaps worth reporting."]
    lines = ["WORST GAPS"]
    for row in gaps[:MAX_GAPS]:
        lines.append(
            f" [{row.get('score', 0):>2}] {clean(row.get('file', '?'))}"
            f":{clean(row.get('ranges', ''), 60)}"
        )
        reasons = row.get("reasons") or ["no risk signals"]
        lines.append(f"      {clean(' | '.join(str(r) for r in reasons))}")
        funcs = (row.get("functions") or [])[:MAX_FUNCS]
        for i, func in enumerate(funcs):
            stem = "└─" if i == len(funcs) - 1 else "├─"
            lines.append(
                f"      {stem} {clean(func.get('function', '?'), 40)}"
                f"  complexity {func.get('complexity', 0)},"
                f" {len(func.get('lines') or ())} lines"
            )
    if len(gaps) > MAX_GAPS:
        lines.append(f" ... {len(gaps) - MAX_GAPS} more files have gaps.")
    return lines


MSW_LABELS = {
    "msw_lenient": "unmatched requests pass",
    "msw_no_reset": "handlers leak between tests",
}


def mock_block(stacks: dict) -> list[str]:
    """Problems with a faked backend. A green suite against a stale fake is
    the quietest false confidence there is."""
    records = stacks.get("mock_setup") or []
    if not records:
        return []
    lines = [f"FAKED BACKEND  ({len(records)} problem"
             f"{'' if len(records) == 1 else 's'})"]
    for item in records[:MAX_GAPS]:
        label = MSW_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        lines.append(f" {pad(label, 28)} {clean(item.get('path', '?'), 46)}")
        lines.append(f"   {clean(item.get('why', ''), 72)}")
    return lines


def never_run_block(stacks: dict) -> list[str]:
    """Focused and skipped tests. A focused one is the dangerous half."""
    records = stacks.get("never_run") or []
    if not records:
        return []
    focused = [r for r in records if r.get("kind") == "focused"]
    skipped = [r for r in records if r.get("kind") == "skipped"]
    lines = [f"NEVER RUN  ({len(focused)} focused, {len(skipped)} skipped)"]
    if focused:
        lines.append(" focused -- everything else in their file is silently idle:")
        for item in focused[:MAX_GAPS]:
            lines.append(f"   {clean(item.get('path', '?'), 44)}"
                         f":{item.get('line', 0)}  {clean(item.get('test', '?'), 30)}")
    if skipped:
        lines.append(" skipped:")
        for item in skipped[:MAX_GAPS]:
            lines.append(f"   {pad(item.get('test', '?'), 38)} {clean(item.get('why', ''), 44)}")
        if len(skipped) > MAX_GAPS:
            lines.append(f"   ... {len(skipped) - MAX_GAPS} more.")
    return lines


def bare_block(stacks: dict) -> list[str]:
    """Tests that assert nothing. Covered code they execute, nothing they check."""
    bare = stacks.get("assertionless") or []
    if not bare:
        return []
    total = stacks.get("assertionless_count", sum(len(i.get("tests") or ()) for i in bare))
    nothing = stacks.get("checks_nothing_count", 0)
    plural = "test" if total == 1 else "tests"
    lines = [f"NO EXPLICIT ASSERTION  ({total} {plural}, "
             f"{nothing} that cannot fail at all)"]
    for item in bare[:MAX_GAPS]:
        lines.append(f" {clean(item.get('path', '?'), 60)}")
        for label, key in (("cannot fail", "checks_nothing"),
                           ("passes unless it raises", "relies_on_no_raise")):
            names = item.get(key) or ()
            if names:
                shown = ", ".join(clean(t, 34) for t in names[:5])
                lines.append(f"      {label}: {clean(shown)}")
    if len(bare) > MAX_GAPS:
        lines.append(f" ... {len(bare) - MAX_GAPS} more files.")
    return lines


SUSPECT_LABELS = {
    "always_true": "cannot fail",
    "broad_raises": "catches too much",
    "no_subject": "calls nothing",
    "duplicate": "same as another test",
    "unused_result": "drops the answer",
    "weak_assertion": "proves too little",
    "dead_fixture": "fixture nobody asks for",
}


def suspect_block(stacks: dict) -> list[str]:
    """Tests that are green for the wrong reason."""
    suspect = stacks.get("suspect") or []
    if not suspect:
        return []
    lines = [f"NOT REALLY TESTING  ({len(suspect)} green tests that prove nothing)"]
    for item in suspect[:MAX_GAPS]:
        label = SUSPECT_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        lines.append(f" {pad(label, 20)} {clean(item.get('test', '?'), 44)}")
        lines.append(f"   {' ' * 20}{clean(item.get('why', ''), 60)}")
    if len(suspect) > MAX_GAPS:
        lines.append(f" ... {len(suspect) - MAX_GAPS} more.")
    return lines


def slow_block(flaky: dict) -> list[str]:
    slow = flaky.get("slowest") or []
    if not slow:
        return []
    peak = max((item.get("seconds", 0) for item in slow), default=0) or 1
    lines = ["SLOWEST  (worst run of each)"]
    for item in slow:
        seconds = item.get("seconds", 0)
        # The id carries module and test name, so it needs most of the line;
        # the bar only has to show the relative shape.
        lines.append(
            f" {pad(item.get('test', '?'), 52)}"
            f"{bar(seconds / peak, 10)} {seconds:>8.3f}s"
        )
    return lines


def quarantine_block(flaky: dict) -> list[str]:
    args = flaky.get("quarantine") or []
    if not args:
        return []
    return ["QUARANTINE  (temporary -- a test nobody runs catches nothing)"] + [
        f" pytest {clean(arg)}" for arg in args[:MAX_GAPS]
    ]


def flaky_block(flaky: dict) -> list[str]:
    if not flaky:
        return []
    bad = flaky.get("flaky") or []
    broken = flaky.get("broken") or []
    partial = flaky.get("partial") or []
    if not (bad or broken or partial):
        return ["FLAKINESS",
                f" Every test agreed with itself across {flaky.get('runs', 0)} runs."]
    lines = ["FLAKINESS"]
    for item in bad:
        marks = " ".join(clean(o, 6) for o in item.get("outcomes") or ())
        lines.append(
            f" {pad(item.get('test', '?'), 34)} {marks}"
            f"   {item.get('passed', 0)}p/{item.get('failed', 0)}f"
        )
    for item in broken:
        lines.append(f" {pad(item.get('test', '?'), 34)} always failing"
                     f"   broken, not flaky")
    for item in partial:
        lines.append(f" {pad(item.get('test', '?'), 34)} not in every run"
                     f"   collection changed")
    return lines


def header(stacks: dict, coverage: dict, flaky: dict, title: str) -> list[str]:
    bits = []
    summary = coverage.get("summary") or {}
    if summary:
        bits.append(f"{summary.get('covered_pct', 0)}% covered")
        bits.append(f"{summary.get('missed', 0)}/{summary.get('statements', 0)} missed")
    if stacks:
        bits.append(f"{stacks.get('total_tests', 0)} tests")
        if stacks.get("mock_setup_count"):
            bits.append(f"{stacks['mock_setup_count']} mock issues")
        if stacks.get("focused_count"):
            bits.append(f"{stacks['focused_count']} focused")
        if stacks.get("never_run_count"):
            bits.append(f"{stacks['never_run_count']} never run")
        if stacks.get("suspect_count"):
            bits.append(f"{stacks['suspect_count']} prove nothing")
        # Only a test that cannot fail deserves the alarming word.
        nothing = stacks.get("checks_nothing_count", 0)
        if nothing:
            bits.append(f"{nothing} cannot fail")
        elif stacks.get("assertionless_count"):
            bits.append(f"{stacks['assertionless_count']} without an assert")
    if flaky:
        bits.append(f"{len(flaky.get('flaky') or ())} flaky")

    # Wrap rather than truncate: every one of these is a finding, and a
    # terminal is not always wide enough for all of them on one line.
    prefix = f"/{clean(title, 30)}"
    if not bits:
        return [prefix]
    indent = " " * (len(prefix) + 3)
    lines, current = [], f"{prefix}   {bits[0]}"
    for bit in bits[1:]:
        if len(current) + 5 + len(bit) > WIDTH:
            lines.append(current)
            current = indent + bit
        else:
            current += f"  |  {bit}"
    lines.append(current)
    return lines


def draw(stacks: dict, coverage: dict, flaky: dict, title: str = "gtest") -> str:
    blocks = [
        header(stacks, coverage, flaky, title),
        kinds_block(stacks),
        never_run_block(stacks),
        mock_block(stacks),
        suspect_block(stacks),
        bare_block(stacks),
        files_block(coverage),
        gaps_block(coverage),
        flaky_block(flaky),
        quarantine_block(flaky),
        slow_block(flaky),
    ]
    return "\n\n".join("\n".join(b) for b in blocks if b)


def load(path: str | None) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"no such file: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{file} is not a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stacks", help="stacks.py --json output")
    parser.add_argument("--coverage", help="rank.py --json output")
    parser.add_argument("--flaky", help="flaky.py --json output")
    parser.add_argument("--title", default="gtest", help="heading line")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if not (args.stacks or args.coverage or args.flaky):
        parser.error("pass at least one of --stacks, --coverage, --flaky")

    try:
        print(draw(load(args.stacks), load(args.coverage), load(args.flaky), args.title))
    except ValueError as exc:
        print(f"gtest: {exc}", file=sys.stderr)
        return 2
    return 0


# --- self-check ---

STACKS = {
    "total_tests": 94,
    "kinds": {
        "unit": {"tests": 80, "files": 5, "state": "covered"},
        "integration": {"tests": 14, "files": 1, "state": "covered"},
        "smoke": {"tests": 3, "files": 1, "state": "covered"},
        "e2e": {"tests": 0, "files": 0, "state": "gap",
                "because": "it ships something a user starts",
                "evidence": ["fastapi", "Dockerfile"]},
        "performance": {"tests": 0, "files": 0, "state": "not applicable",
                        "because": "it already carries a benchmarking tool",
                        "evidence": []},
        "contract": {"tests": 0, "files": 0, "state": "not applicable",
                     "because": "it publishes an API surface", "evidence": []},
    },
    "missing_kinds": ["e2e"],
    "not_applicable": ["performance", "contract"],
    "assertionless": [{"path": "tests/unit/test_math.py", "kind": "unit",
                       "tests": ["test_add", "test_sub"],
                       "checks_nothing": ["test_add"],
                       "relies_on_no_raise": ["test_sub"]}],
    "assertionless_count": 2,
    "checks_nothing_count": 1,
    "suspect": [
        {"path": "tests/unit/test_math.py", "test": "test_always_passes",
         "kind": "always_true", "why": "asserts a literal"},
        {"path": "tests/unit/test_math.py", "test": "test_catches_everything",
         "kind": "broad_raises", "why": "raises(Exception) with no match="},
    ],
    "suspect_count": 2,
    "never_run": [
        {"path": "tests/e2e/login.spec.ts", "test": "logs in", "kind": "focused",
         "line": 12, "why": "it.only silences the rest of the file"},
        {"path": "tests/unit/test_math.py", "test": "test_legacy", "kind": "skipped",
         "line": 40, "why": "pytest.mark.skip, with no reason given"},
    ],
    "never_run_count": 2,
    "focused_count": 1,
    "mock_setup": [
        {"path": "src/mocks/server.ts", "kind": "msw_lenient",
         "why": "onUnhandledRequest is bypass, not 'error'"},
    ],
    "mock_setup_count": 1,
}

COVERAGE = {
    "summary": {"covered_pct": 71.5, "missed": 57, "statements": 200, "files": 3},
    "gaps": [{
        "file": "src/auth/session.py", "score": 9, "ranges": "4-6,9",
        "reasons": ["risky path (auth)", "2 uncovered branches"],
        "missed_lines": [4, 5, 6, 9], "statements": 20, "branches": 2,
        "functions": [
            {"function": "expire", "complexity": 7, "lines": [4, 5, 6]},
            {"function": "refresh", "complexity": 3, "lines": [9]},
        ],
    }],
}

FLAKY = {
    "runs": 3,
    "flaky": [{"test": "t::login", "outcomes": ["pass", "fail", "pass"],
               "passed": 2, "failed": 1}],
    "broken": [{"test": "t::old", "outcomes": ["fail", "fail", "fail"]}],
    "partial": [],
    "slowest": [{"test": "t::slow_one", "seconds": 4.5},
                {"test": "t::login", "seconds": 0.9}],
    "quarantine": ["--deselect t/login.py::login"],
}


def self_check() -> int:
    page = draw(STACKS, COVERAGE, FLAKY)
    lines = page.splitlines()

    assert "</" not in page and "<html" not in page.lower(), "terminal output, not markup"
    assert "<style" not in page and "&lt;" not in page, "no markup, no escaping artefacts"
    assert lines[0].startswith("/gtest")
    assert "71.5% covered" in lines[0] and "94 tests" in lines[0]
    assert all(len(line) <= WIDTH for line in lines), max(lines, key=len)

    assert "TEST KINDS" in page and "COVERED PER FILE" in page
    assert "WORST GAPS" in page and "FLAKINESS" in page

    # the kinds with nothing are the headline, so they must be marked
    assert page.count("<- gap") == 1, page
    assert page.count("n/a") == 2, page
    assert "No e2e tests, and this repo wants them" in page
    assert "evidence: fastapi, Dockerfile" in page, "the reason is shown"
    assert "Not expected here" in page and "performance, contract" in page
    assert "No performance tests" not in page, "a kind with no evidence is not a gap"

    assert "[ 9] src/auth/session.py:4-6,9" in page
    assert "risky path (auth) | 2 uncovered branches" in page
    assert "├─ expire" in page, "a non-final function uses a tee"
    assert "└─ refresh" in page, "the last function closes the branch"
    assert "complexity 7, 3 lines" in page

    assert "t::login" in page and "pass fail pass" in page and "2p/1f" in page
    assert "always failing" in page

    # the SDET blocks: tests that check nothing, the slow ones, the quarantine list
    assert "FAKED BACKEND" in page and "1 problem)" in page
    assert "unmatched requests pass" in page and "src/mocks/server.ts" in page
    assert "1 mock issues" in page
    assert "FAKED BACKEND" not in draw({"kinds": STACKS["kinds"]}, {}, {})
    assert "NEVER RUN" in page and "1 focused, 1 skipped" in page
    assert "silently idle" in page and "logs in" in page
    assert "test_legacy" in page and "no reason given" in page
    assert "1 focused" in page and "2 never run" in page
    assert "NEVER RUN" not in draw({"kinds": STACKS["kinds"]}, {}, {})
    assert "NOT REALLY TESTING" in page and "2 green tests that prove nothing" in page
    assert "cannot fail" in page and "test_always_passes" in page
    assert "catches too much" in page and "no match=" in page
    assert "2 prove nothing" in page, page.splitlines()[:2]
    assert "NOT REALLY TESTING" not in draw({"kinds": STACKS["kinds"]}, {}, {})
    assert "NO EXPLICIT ASSERTION" in page and "1 that cannot fail at all" in page
    assert "2 tests, 1 that cannot" in page, "plural when there are two"
    single = draw({**STACKS, "assertionless_count": 1, "checks_nothing_count": 1,
                   "assertionless": [{"path": "a.py", "tests": ["t"],
                                      "checks_nothing": ["t"],
                                      "relies_on_no_raise": []}]}, {}, {})
    assert "1 test, 1 that cannot" in single, "singular when there is one"
    assert "tests/unit/test_math.py" in page
    assert "cannot fail: test_add" in page, "the worthless one is named as such"
    assert "passes unless it raises: test_sub" in page, "the weaker one is not"
    assert "1 cannot fail" in page
    softer = draw({**STACKS, "checks_nothing_count": 0}, {}, {})
    assert "2 without an assert" in softer
    assert "cannot fail" not in softer.splitlines()[0], "no alarm when none can fail"

    # a header with every chip wraps instead of running off the screen
    crowded = header(STACKS, COVERAGE, FLAKY, "a" * 30)
    assert len(crowded) >= 2, crowded
    assert all(len(line) <= WIDTH for line in crowded), max(crowded, key=len)
    assert header({}, {}, {}, "x") == ["/x"], "no findings, no chips"
    assert "SLOWEST" in page and "4.500s" in page and "t::slow_one" in page
    assert "QUARANTINE" in page and "pytest --deselect t/login.py::login" in page
    assert "a test nobody runs catches nothing" in page

    # each optional block disappears cleanly when its data is absent
    assert "NO EXPLICIT ASSERTION" not in draw({"kinds": STACKS["kinds"]}, {}, {})
    assert "SLOWEST" not in draw({}, {}, {"runs": 2, "flaky": [], "broken": []})
    assert "QUARANTINE" not in draw({}, {}, {"runs": 2, "flaky": [], "broken": []})
    # a slow bar is narrower than the kinds bars and still never overflows
    assert all(len(bar(f / 10, 14)) == 14 for f in range(11))

    # no line may be wider than a standard terminal
    assert all(len(line) <= 100 for line in lines), max(lines, key=len)

    # bars are proportional, clamped, and always exactly BAR cells wide
    assert bar(0.0) == TRACK * BAR
    assert bar(1.0) == FULL * BAR
    assert bar(2.0) == FULL * BAR, "over 100% cannot overflow the track"
    assert bar(-1.0) == TRACK * BAR, "a negative share cannot underflow"
    assert all(len(bar(f / 20)) == BAR for f in range(21))
    assert bar(0.5).count(FULL) == BAR // 2
    assert bar(0.01) != TRACK * BAR, "a small nonzero share must still show"

    # every block is optional and any one alone must still draw
    assert "TEST KINDS" in draw(STACKS, {}, {})
    assert "TEST KINDS" not in draw({}, COVERAGE, {})
    assert "No gaps worth reporting" in draw({}, {}, {})
    assert "agreed with itself across 2 runs" in draw(
        {}, {}, {"runs": 2, "flaky": [], "broken": [], "partial": []})
    assert "collection changed" in draw(
        {}, {}, {"runs": 2, "flaky": [], "broken": [],
                 "partial": [{"test": "t::x", "outcomes": ["pass", "absent"]}]})

    # a hostile file path is clipped and stripped, never echoed raw
    nasty = {
        "summary": {"covered_pct": 0, "missed": 1, "statements": 1},
        "gaps": [{
            "file": "a\x1b[31mred\x07.py" + "x" * 400, "score": 1, "ranges": "1",
            "reasons": ["\x1b[2J"], "missed_lines": [1], "statements": 1,
            "functions": [{"function": "f\x00g", "complexity": 1, "lines": [1]}],
        }],
    }
    drawn = draw({}, nasty, {})
    assert "\x1b" not in drawn and "\x07" not in drawn and "\x00" not in drawn, \
        "control characters must never reach the terminal"
    assert "…" in drawn, "an overlong path is truncated"
    assert all(len(line) <= 140 for line in drawn.splitlines())

    # zero statements must not divide by zero
    assert "  0.0%" in draw({}, {"gaps": [{
        "file": "a.py", "missed_lines": [1], "statements": 0,
        "ranges": "1", "reasons": [], "score": 0, "functions": []}]}, {})

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        assert load(None) == {}
        bad = Path(tmp) / "bad.json"
        bad.write_text("{nope")
        listy = Path(tmp) / "list.json"
        listy.write_text("[1,2]")
        for arg, why in (
            (str(Path(tmp) / "gone.json"), "missing"),
            (str(bad), "malformed"),
            (str(listy), "a JSON array"),
        ):
            try:
                load(arg)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{why} input must raise")

        good = Path(tmp) / "cov.json"
        good.write_text(json.dumps(COVERAGE))
        assert main(["--coverage", str(good)]) == 0

    print("diagram.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
