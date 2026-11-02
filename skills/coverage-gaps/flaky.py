#!/usr/bin/env python3
"""Find tests that do not agree with themselves.

Takes the JUnit XML from several runs of the same suite and compares each
test's outcome across them.

    pytest --junitxml=.gtest/run1.xml -q   # repeat N times
    python3 flaky.py .gtest/run*.xml
    python3 flaky.py --self-check

A test with different outcomes across runs is flaky. A test that fails in
every run is simply broken, which is a different problem, so they are
reported apart. Never writes files and never runs the suite itself: the
caller decides when a test suite may run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PASS, FAIL, SKIP = "pass", "fail", "skip"

# A result the test itself produced, as opposed to being skipped or absent.
DECISIVE = (PASS, FAIL)


# xml.etree expands internal entities, so a crafted report can blow up
# memory ("billion laughs"). A JUnit report has no legitimate use for a DTD
# or an entity, so refuse both rather than depend on a hardened parser.
UNSAFE_XML = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)

MAX_XML_BYTES = 64 * 1024 * 1024


def parse_xml(text: str) -> dict[str, str]:
    """JUnit XML -> {test id: pass|fail|skip}. Raises ValueError on junk."""
    if len(text) > MAX_XML_BYTES:
        raise ValueError(f"report larger than {MAX_XML_BYTES // 1024 // 1024}MB, refusing to parse")
    if UNSAFE_XML.search(text):
        raise ValueError("report declares a DTD or entity; refusing to parse it")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"not valid JUnit XML: {exc}") from exc

    results: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name")
        if not name:
            continue
        suite = case.get("classname") or case.get("file") or ""
        test_id = f"{suite}::{name}" if suite else name
        results[test_id] = _outcome(case)
    if not results:
        raise ValueError("no <testcase> elements found")
    return results


def _outcome(case: ET.Element) -> str:
    for child in case:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("failure", "error"):
            return FAIL
        if tag == "skipped":
            return SKIP
    return PASS


def parse_times(text: str) -> dict[str, float]:
    """JUnit XML -> {test id: seconds}. The slowest tests are usually the
    first thing worth fixing after the flaky ones, and the duration is already
    in the report."""
    if len(text) > MAX_XML_BYTES:
        raise ValueError(f"report larger than {MAX_XML_BYTES // 1024 // 1024}MB, refusing to parse")
    if UNSAFE_XML.search(text):
        raise ValueError("report declares a DTD or entity; refusing to parse it")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"not valid JUnit XML: {exc}") from exc

    times: dict[str, float] = {}
    for case in root.iter("testcase"):
        name = case.get("name")
        if not name:
            continue
        suite = case.get("classname") or case.get("file") or ""
        test_id = f"{suite}::{name}" if suite else name
        try:
            times[test_id] = float(case.get("time") or 0.0)
        except ValueError:
            times[test_id] = 0.0
    return times


def slowest(runs: list[dict[str, float]], count: int = 5) -> tuple[dict, ...]:
    """The slowest tests, by their worst run. A test that is slow once blocks
    CI just as badly as one that is slow every time."""
    worst: dict[str, float] = {}
    for run in runs:
        for test, seconds in run.items():
            worst[test] = max(worst.get(test, 0.0), seconds)
    ranked = sorted(worst.items(), key=lambda pair: (-pair[1], pair[0]))
    return tuple(
        {"test": test, "seconds": round(seconds, 3)}
        for test, seconds in ranked[:count]
        if seconds > 0
    )


def quarantine(report: dict) -> tuple[str, ...]:
    """pytest --deselect arguments for the flaky tests, ready to paste.

    Quarantine is temporary: a test nobody runs catches nothing. These are for
    unblocking CI while the flake is fixed, not for burying it.
    """
    args = []
    for item in report.get("flaky") or ():
        test = str(item.get("test", ""))
        if "::" not in test:
            continue
        suite, name = test.split("::", 1)
        if "/" in suite or suite.endswith((".py", ".ts", ".js", ".tsx", ".jsx")):
            path = suite
        else:
            path = suite.replace(".", "/") + ".py"
        args.append(f"--deselect {path}::{name}")
    return tuple(args)


def read_run(path: str | Path) -> dict[str, str]:
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"no such run file: {file}")
    try:
        return parse_xml(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {file}: {exc}") from exc


def classify(runs: list[dict[str, str]]) -> dict:
    """Split every test seen into flaky, broken, stable or partial."""
    if len(runs) < 2:
        raise ValueError("need at least two runs to compare")

    every_test = sorted({test for run in runs for test in run})
    flaky, broken, stable, partial = [], [], [], []

    for test in every_test:
        seen = [run.get(test, "absent") for run in runs]
        decisive = [s for s in seen if s in DECISIVE]
        if len(set(decisive)) > 1:
            flaky.append({
                "test": test,
                "outcomes": seen,
                "passed": decisive.count(PASS),
                "failed": decisive.count(FAIL),
            })
        elif decisive and set(decisive) == {FAIL}:
            broken.append({"test": test, "outcomes": seen})
        elif "absent" in seen:
            partial.append({"test": test, "outcomes": seen})
        else:
            stable.append(test)

    return {
        "runs": len(runs),
        "tests": len(every_test),
        "flaky": flaky,
        "broken": broken,
        "partial": partial,
        "stable": len(stable),
    }


def render(report: dict) -> str:
    head = (
        f"{report['tests']} tests across {report['runs']} runs  |  "
        f"{len(report['flaky'])} flaky  |  {len(report['broken'])} always failing  |  "
        f"{report['stable']} stable"
    )
    lines = [head]

    if report["flaky"]:
        lines += ["", "FLAKY — same test, different answers:"]
        for item in report["flaky"]:
            lines.append(
                f"  {item['test']}  ({item['passed']} pass / {item['failed']} fail)"
            )
            lines.append(f"      {' '.join(item['outcomes'])}")

    if report["broken"]:
        lines += ["", "ALWAYS FAILING — broken, not flaky:"]
        lines += [f"  {item['test']}" for item in report["broken"]]

    if report["partial"]:
        lines += ["", "NOT IN EVERY RUN — collection changed between runs:"]
        lines += [f"  {item['test']}  {' '.join(item['outcomes'])}" for item in report["partial"]]

    if report.get("quarantine"):
        lines += ["", "To unblock CI while you fix them (temporary):"]
        lines += [f"  pytest {arg}" for arg in report["quarantine"]]

    if report.get("slowest"):
        lines += ["", "SLOWEST — worst run of each:"]
        for item in report["slowest"]:
            lines.append(f"  {item['seconds']:>8.3f}s  {item['test']}")

    if not (report["flaky"] or report["broken"] or report["partial"]):
        lines += ["", "Every test agreed with itself in every run."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", help="JUnit XML files, one per run")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--slowest", type=int, default=5, help="how many slow tests to list")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if len(args.runs) < 2:
        parser.error("pass at least two JUnit XML files (or --self-check)")

    try:
        texts = []
        for path in args.runs:
            file = Path(path)
            if not file.is_file():
                raise ValueError(f"no such run file: {file}")
            texts.append(file.read_text(encoding="utf-8"))
        report = classify([parse_xml(text) for text in texts])
        report["slowest"] = list(slowest([parse_times(text) for text in texts], args.slowest))
        report["quarantine"] = list(quarantine(report))
    except (ValueError, OSError) as exc:
        print(f"gtest: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 1 if report["flaky"] else 0


# --- self-check ---

def _xml(cases: str) -> str:
    return f'<testsuites><testsuite name="s" tests="1">{cases}</testsuite></testsuites>'


def self_check() -> int:
    passing = '<testcase classname="t.test_a" name="test_ok"/>'
    failing = '<testcase classname="t.test_a" name="test_ok"><failure message="boom"/></testcase>'
    erroring = '<testcase classname="t.test_a" name="test_ok"><error message="boom"/></testcase>'
    skipped = '<testcase classname="t.test_a" name="test_ok"><skipped/></testcase>'

    assert parse_xml(_xml(passing)) == {"t.test_a::test_ok": PASS}
    assert parse_xml(_xml(failing)) == {"t.test_a::test_ok": FAIL}
    assert parse_xml(_xml(erroring)) == {"t.test_a::test_ok": FAIL}, "error counts as fail"
    assert parse_xml(_xml(skipped)) == {"t.test_a::test_ok": SKIP}

    # classname is optional (vitest omits it on some reporters)
    assert parse_xml(_xml('<testcase name="bare"/>')) == {"bare": PASS}

    run_a = {"a::one": PASS, "a::two": PASS, "a::three": FAIL}
    run_b = {"a::one": FAIL, "a::two": PASS, "a::three": FAIL}
    run_c = {"a::one": PASS, "a::two": PASS, "a::three": FAIL}
    report = classify([run_a, run_b, run_c])

    assert [f["test"] for f in report["flaky"]] == ["a::one"], report["flaky"]
    assert report["flaky"][0]["passed"] == 2, report["flaky"][0]
    assert report["flaky"][0]["failed"] == 1, report["flaky"][0]
    assert [b["test"] for b in report["broken"]] == ["a::three"], report["broken"]
    assert report["stable"] == 1, report
    assert report["tests"] == 3, report

    # a skip next to a pass is not flakiness
    quiet = classify([{"a::one": PASS}, {"a::one": SKIP}])
    assert quiet["flaky"] == [], quiet
    assert quiet["stable"] == 1, "a pass next to a skip is stable, not a gap"
    assert quiet["broken"] == [] and quiet["partial"] == [], quiet

    # a test that only exists in one run is a collection change, not flake
    moved = classify([{"a::one": PASS}, {"a::two": PASS}])
    assert moved["flaky"] == [], moved
    assert [p["test"] for p in moved["partial"]] == ["a::one", "a::two"], moved

    assert "Every test agreed" in render(classify([{"a::one": PASS}, {"a::one": PASS}]))
    assert "FLAKY" in render(report) and "ALWAYS FAILING" in render(report)

    bomb = (
        '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        '<testsuites><testsuite><testcase name="&lol;"/></testsuite></testsuites>'
    )
    for bad, why in (
        ("<not xml", "malformed"),
        (_xml(""), "no testcases"),
        (bomb, "entity declaration"),
    ):
        try:
            parse_xml(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{why} input must raise")

    try:
        classify([{"a::one": PASS}])
    except ValueError:
        pass
    else:
        raise AssertionError("a single run must raise")

    timed = _xml(
        '<testcase classname="t" name="fast" time="0.01"/>'
        '<testcase classname="t" name="slow" time="4.5"/>'
        '<testcase classname="t" name="broken_time" time="NaN-ish"/>'
    )
    times = parse_times(timed)
    assert times["t::fast"] == 0.01 and times["t::slow"] == 4.5
    assert times["t::broken_time"] == 0.0, "an unparseable time is zero, not a crash"

    ranked = slowest([times, {"t::fast": 9.0}])
    assert ranked[0] == {"test": "t::fast", "seconds": 9.0}, ranked
    assert ranked[1] == {"test": "t::slow", "seconds": 4.5}, ranked
    assert all(item["seconds"] > 0 for item in ranked), "zero-time tests are not listed"
    assert len(slowest([times], count=1)) == 1
    assert slowest([]) == ()

    args = quarantine({"flaky": [
        {"test": "tests.unit.test_auth::test_login"},
        {"test": "tests/e2e/login.spec.ts::logs in"},
        {"test": "no_separator"},
    ]})
    assert args[0] == "--deselect tests/unit/test_auth.py::test_login", args
    assert args[1] == "--deselect tests/e2e/login.spec.ts::logs in", args
    assert len(args) == 2, "an id without :: cannot be deselected"
    assert quarantine({}) == ()

    shown = render({**report, "slowest": list(ranked), "quarantine": list(args)})
    assert "SLOWEST" in shown and "9.000s" in shown
    assert "To unblock CI" in shown and "--deselect" in shown

    for bad_fn in (parse_times,):
        try:
            bad_fn("<!DOCTYPE x [<!ENTITY e \"b\">]>" + _xml(passing))
        except ValueError:
            pass
        else:
            raise AssertionError("parse_times must refuse an entity declaration too")

    print("flaky.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
