#!/usr/bin/env python3
"""Find out what kinds of tests a repo actually has, and how to run them.

Coverage percentages only answer half the question. The other half is which
kinds of tests exist at all: a repo at 90% line coverage with no end-to-end
test is not a well-tested repo. This walks the tree, sorts every test file
into a kind, counts the test functions, and reports which kinds are missing.

    python3 stacks.py                 # inventory of the current repo
    python3 stacks.py --json
    python3 stacks.py --self-check

Suggests the command for each runner it recognises. Never runs anything:
deciding to execute a test suite is the caller's call, not this script's.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PRUNE = {
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "coverage", "htmlcov", ".next", ".nuxt", ".tox",
    "target", "vendor", ".mypy_cache", ".pytest_cache", ".gtest",
}

# Kind is decided by path, not by runner: the same runner is used for unit
# and end-to-end tests in most repos. First hint that matches wins, so the
# narrow kinds are listed before the broad ones.
KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("e2e", ("e2e", "end2end", "end-to-end", "playwright", "cypress",
             "selenium", "puppeteer", "browser", "journey")),
    ("smoke", ("smoke", "sanity")),
    ("performance", ("perf", "benchmark", "bench", "load", "stress")),
    ("contract", ("contract", "pact", "schemathesis")),
    ("integration", ("integration", "integrations", "api_test", "test_api",
                     "functional", "acceptance")),
    ("unit", ()),
)

ALL_KINDS = tuple(kind for kind, _ in KIND_HINTS)

# A kind is a gap only when something in the repo says it belongs there.
# Without that evidence the kind is not applicable: "no contract tests" is
# noise about a CLI, and "no end-to-end tests" is noise about a pure library.
# Each entry says what would make the kind expected, and why, so the report
# can show its reasoning instead of asserting it.
KIND_EVIDENCE: dict[str, dict] = {
    "unit": {
        "always": True,
        "because": "there is code to test in isolation",
    },
    "integration": {
        "deps": (
            "sqlalchemy", "psycopg", "psycopg2", "asyncpg", "pymongo", "redis",
            "boto3", "requests", "httpx", "aiohttp", "urllib3", "kafka",
            "celery", "elasticsearch", "pika", "mysqlclient", "sqlite-utils",
            "prisma", "mongoose", "pg", "knex", "axios", "node-fetch",
        ),
        "files": ("docker-compose.yml", "docker-compose.yaml"),
        "because": "it talks to something outside its own process",
    },
    "e2e": {
        "deps": (
            "fastapi", "flask", "django", "starlette", "tornado", "sanic",
            "express", "koa", "next", "nuxt", "vue", "react", "svelte",
            "streamlit", "gradio",
        ),
        "files": (
            "playwright.config.ts", "playwright.config.js",
            "cypress.config.ts", "cypress.config.js", "cypress.json",
            "Dockerfile",
        ),
        "entry_point": True,
        "because": "it ships something a user starts: a CLI, a server or a page",
    },
    "smoke": {
        "entry_point": True,
        "files": ("Dockerfile",),
        "because": "it ships something that has to start up at all",
    },
    "contract": {
        "files": (
            "openapi.json", "openapi.yaml", "openapi.yml",
            "swagger.json", "swagger.yaml", "asyncapi.yaml", "asyncapi.yml",
            "pact.json", "schema.graphql",
        ),
        "dirs": ("proto", "protos"),
        "deps": ("grpcio", "strawberry-graphql", "graphene", "pact-python",
                 "schemathesis", "apollo-server", "graphql", "msw",
                 "nock", "responses", "respx", "vcrpy", "wiremock"),
        "because": ("it depends on an API whose shape is faked in tests, and a "
                    "handwritten fake drifts from the real thing silently"),
    },
    "performance": {
        "deps": ("pytest-benchmark", "locust", "asv", "k6", "autocannon",
                 "benchmark", "criterion"),
        "because": "it already carries a benchmarking tool",
    },
}

# Files that declare what the project ships to a user.
MANIFESTS = (
    "pyproject.toml", "setup.cfg", "setup.py", "Pipfile",
    "package.json", "go.mod", "Cargo.toml",
    "requirements.txt", "requirements-dev.txt",
)

# Markers in a manifest that mean "this installs a command or a binary".
ENTRY_POINT_MARKERS = (
    "[project.scripts]", "[project.gui-scripts]", "console_scripts",
    '"bin"', "[[bin]]", "func main(",
)

# filename pattern -> (language, regex counting one test each)
TEST_FILES: tuple[tuple[re.Pattern, str, re.Pattern], ...] = (
    (re.compile(r"(^test_.*|.*_test)\.py$"), "python",
     re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)),
    (re.compile(r".*\.(spec|test)\.[jt]sx?$"), "javascript",
     re.compile(r"\b(?:it|test)(?:\.\w+)?\s*\(", re.M)),
    (re.compile(r".*_test\.go$"), "go",
     re.compile(r"^func\s+(Test\w+)", re.M)),
    (re.compile(r".*\.(spec|test)\.rb$|^test_.*\.rb$"), "ruby",
     re.compile(r"^\s*(?:it|test)\s+[\"']", re.M)),
)

# evidence file -> what to run. Commands that cannot produce line coverage
# say so, instead of pretending.
RUNNERS: tuple[dict, ...] = (
    {
        "runner": "pytest", "language": "python",
        "evidence": ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py"),
        "needs": "pytest-cov",
        "command": "pytest --cov --cov-report=json --cov-report=term-missing",
        "report": "coverage.json", "coverage": True,
        "install": "pip install pytest-cov",
        "setup": "",
        "runs_command": "pytest --junitxml={out} -q",
    },
    {
        "runner": "vitest", "language": "javascript",
        "evidence": ("vitest.config.ts", "vitest.config.js", "vite.config.ts", "vite.config.js"),
        "needs": "@vitest/coverage-v8",
        "command": "npx vitest run --coverage --coverage.reporter=json",
        "report": "coverage/coverage-final.json", "coverage": True,
        "install": "npm i -D @vitest/coverage-v8",
        "setup": "",
        "runs_command": "npx vitest run --reporter=junit --outputFile={out}",
    },
    {
        "runner": "jest", "language": "javascript",
        "evidence": ("jest.config.js", "jest.config.ts", "jest.config.mjs"),
        "needs": "jest",
        "command": "npx jest --coverage --coverageReporters=json",
        "report": "coverage/coverage-final.json", "coverage": True,
        "install": "npm i -D jest",
        "setup": "",
        "runs_command": "npx jest --reporters=jest-junit",
    },
    {
        "runner": "playwright", "language": "javascript",
        "evidence": ("playwright.config.ts", "playwright.config.js"),
        "needs": "monocart-reporter",
        "command": "npx playwright test --reporter=monocart-reporter",
        "report": "coverage/coverage-final.json", "coverage": True,
        "install": "npm i -D @playwright/test monocart-reporter && npx playwright install",
        "setup": ("add monocart-reporter to playwright.config with "
                  "coverageReport: { reports: ['v8', 'codecov'], "
                  "outputDir: 'coverage' } -- browser code needs the reporter "
                  "to collect V8 coverage, the runner alone emits none"),
        "runs_command": "npx playwright test --reporter=junit",
    },
    {
        "runner": "cypress", "language": "javascript",
        "evidence": ("cypress.config.ts", "cypress.config.js", "cypress.json"),
        "needs": "@cypress/code-coverage",
        "command": "npx cypress run",
        "report": "coverage/coverage-final.json", "coverage": True,
        "install": "npm i -D cypress @cypress/code-coverage nyc babel-plugin-istanbul",
        "setup": ("require('@cypress/code-coverage/task')(on, config) in "
                  "setupNodeEvents, import the support file, and instrument the "
                  "app with babel-plugin-istanbul -- without instrumentation "
                  "there is nothing to collect"),
        "runs_command": "npx cypress run --reporter junit",
    },
    {
        "runner": "go test", "language": "go",
        "evidence": ("go.mod",),
        "needs": "go",
        "command": "go test ./... -coverprofile=coverage.out -covermode=count",
        "report": "coverage.out", "coverage": True,
        "install": "https://go.dev/dl/",
        "setup": "",
        "runs_command": "go test ./... -json",
    },
)


@dataclass(frozen=True)
class TestFile:
    path: str
    kind: str
    language: str
    tests: int


def manifest_text(root: str | Path) -> str:
    """Every manifest in the tree, concatenated. Empty when there is none."""
    base = Path(root)
    chunks = []
    for path in walk(base):
        if path.name in MANIFESTS:
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(chunks)


def _mentions(text: str, name: str) -> bool:
    """A dependency name in a manifest, with - and _ treated as the same.

    Split on the separators and rejoin with a class, so pytest-benchmark and
    pytest_benchmark both match either spelling. The lookarounds keep it from
    matching inside a longer name: react must not match reactive-streams.
    """
    pattern = "[-_]".join(re.escape(part) for part in re.split(r"[-_]", name))
    return re.search(rf"(?<![\w.-]){pattern}(?![\w-])", text, re.I) is not None


def expectations(root: str | Path = ".") -> dict[str, dict]:
    """Which kinds of test this repo should have, and the evidence for each."""
    base = Path(root)
    text = manifest_text(base)
    present = {p.name for p in walk(base)}
    dirs = {p.name for p in base.rglob("*")
            if p.is_dir() and not any(part in PRUNE for part in p.parts)}
    has_entry_point = any(marker in text for marker in ENTRY_POINT_MARKERS)

    out = {}
    for kind, rule in KIND_EVIDENCE.items():
        found: list[str] = []
        if rule.get("always"):
            found.append("any code at all")
        for dep in rule.get("deps", ()):
            if _mentions(text, dep):
                found.append(dep)
        for name in rule.get("files", ()):
            if name in present:
                found.append(name)
        for name in rule.get("dirs", ()):
            if name in dirs:
                found.append(f"{name}/")
        if rule.get("entry_point") and has_entry_point:
            found.append("an installed entry point")
        out[kind] = {
            "expected": bool(found),
            "because": rule["because"],
            "evidence": sorted(set(found))[:4],
        }
    return out


def classify(path: str) -> str:
    lowered = path.lower().replace("\\", "/")
    for kind, hints in KIND_HINTS:
        if any(hint in lowered for hint in hints):
            return kind
    return "unit"


def count_tests(text: str, pattern: re.Pattern) -> int:
    return len(pattern.findall(text))


def walk(root: Path):
    """Every file under root, skipping the directories nobody tests."""
    for path in root.rglob("*"):
        # A symlink can point outside the repo entirely; counting tests is
        # never worth following one.
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in PRUNE for part in path.parts):
            continue
        yield path


MOCK_SETUP_HINTS = ("msw", "mock", "setup", "handler", "fixture", "conftest",
                    "cassette", "vcr", "stub", "fake")

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def mock_setups(root: str | Path) -> tuple[dict, ...]:
    """Problems with a faked backend or a faked object, wherever it is set up.

    The setup is usually not a test file -- src/mocks/server.ts, setupTests.ts,
    conftest.py -- so this looks beyond the test files. Python test files are
    read as well, because a spec-less patch sits in the test that uses it.
    """
    try:
        import jssource
    except ImportError:  # pragma: no cover - only if the file is moved away
        jssource = None
    try:
        import pysource
    except ImportError:  # pragma: no cover - only if the file is moved away
        pysource = None

    base = Path(root)
    found: list[dict] = []
    for path in walk(base):
        lowered = f"{path.parent}/{path.name}".lower()
        hinted = any(hint in lowered for hint in MOCK_SETUP_HINTS)

        if path.suffix in JS_SUFFIXES and jssource is not None and hinted:
            reader, check = jssource, "msw_findings"
        elif path.suffix == ".py" and pysource is not None and (
            hinted or TEST_FILES[0][0].match(path.name)
        ):
            reader, check = pysource, "mock_findings"
        else:
            continue

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for record in getattr(reader, check)(source):
            found.append({"path": str(path.relative_to(base)), **record})
    return tuple(found)


def analyse_tests(
    root: str | Path, files: tuple[TestFile, ...]
) -> tuple[tuple[dict, ...], tuple[dict, ...], tuple[dict, ...]]:
    """Tests with no assertion, tests that cannot fail, and tests that never run.

    A test that runs code and checks nothing still counts towards coverage,
    and so does one whose assertion is a tautology, and so does one that is
    skipped. All three are the gap a percentage cannot show, so all three are
    reported next to it. Python is read with ast, JavaScript and TypeScript
    with a scanner, and both answer the same questions.
    """
    try:
        import pysource
    except ImportError:  # pragma: no cover - only if the file is moved away
        pysource = None
    try:
        import jssource
    except ImportError:  # pragma: no cover - only if the file is moved away
        jssource = None
    if pysource is None and jssource is None:  # pragma: no cover
        return (), (), ()

    readers = {"python": pysource, "javascript": jssource}

    base = Path(root)
    found: list[dict] = []
    suspects: list[dict] = []
    never_run: list[dict] = []
    for test in files:
        reader = readers.get(test.language)
        if reader is None:
            continue
        path = base / test.path
        if path.is_symlink() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for record in reader.suspect_tests(source):
            suspects.append({"path": test.path, **record})
        if hasattr(reader, "dead_fixtures"):
            for record in reader.dead_fixtures(
                source, is_conftest=path.name == "conftest.py"
            ):
                suspects.append({"path": test.path, **record})
        for record in reader.focus_and_skips(source):
            never_run.append({"path": test.path, **record})
        bare = reader.unasserted(source)
        if bare:
            found.append({
                "path": test.path,
                "kind": test.kind,
                "tests": [r["test"] for r in bare],
                "checks_nothing": [r["test"] for r in bare
                                   if r["kind"] == "checks_nothing"],
                "relies_on_no_raise": [r["test"] for r in bare
                                       if r["kind"] == "relies_on_no_raise"],
            })
    return tuple(found), tuple(suspects), tuple(never_run)


def discover(root: str | Path = ".") -> tuple[TestFile, ...]:
    base = Path(root)
    found = []
    for path in walk(base):
        for name_pattern, language, test_pattern in TEST_FILES:
            if not name_pattern.match(path.name):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            rel = str(path.relative_to(base))
            found.append(TestFile(
                path=rel,
                kind=classify(rel),
                language=language,
                tests=count_tests(text, test_pattern),
            ))
            break
    return tuple(sorted(found, key=lambda f: f.path))


def runners(root: str | Path = ".") -> tuple[dict, ...]:
    """Runners whose evidence file is present. Nested configs count too."""
    base = Path(root)
    present = {p.name for p in walk(base)}
    return tuple(r for r in RUNNERS if any(e in present for e in r["evidence"]))


def inventory(root: str | Path = ".") -> dict:
    files = discover(root)
    by_kind = {
        kind: {
            "files": sum(1 for f in files if f.kind == kind),
            "tests": sum(f.tests for f in files if f.kind == kind),
        }
        for kind in ALL_KINDS
    }
    found = runners(root)
    bare, suspects, never_run = analyse_tests(root, files)
    mocks = mock_setups(root)
    expected = expectations(root)
    for kind, row in by_kind.items():
        rule = expected[kind]
        if row["files"]:
            row["state"] = "covered"
        elif rule["expected"]:
            row["state"] = "gap"
        else:
            row["state"] = "not applicable"
        row["expected"] = rule["expected"]
        row["because"] = rule["because"]
        row["evidence"] = rule["evidence"]
    return {
        "kinds": by_kind,
        "assertionless": [dict(item) for item in bare],
        "assertionless_count": sum(len(item["tests"]) for item in bare),
        "checks_nothing_count": sum(len(item["checks_nothing"]) for item in bare),
        "suspect": [dict(item) for item in suspects],
        "suspect_count": len(suspects),
        "never_run": [dict(item) for item in never_run],
        "never_run_count": len(never_run),
        "mock_setup": [dict(item) for item in mocks],
        "mock_setup_count": len(mocks),
        "focused_count": sum(1 for item in never_run if item["kind"] == "focused"),
        # Only kinds the repo is expected to have. A kind with no evidence
        # behind it is not a gap, and reporting it as one is a false alarm.
        "missing_kinds": [k for k in ALL_KINDS if by_kind[k]["state"] == "gap"],
        "not_applicable": [k for k in ALL_KINDS if by_kind[k]["state"] == "not applicable"],
        "total_files": len(files),
        "total_tests": sum(f.tests for f in files),
        "languages": sorted({f.language for f in files}),
        "runners": [
            {k: r.get(k, "") for k in
             ("runner", "language", "command", "report", "coverage", "install", "setup")}
            for r in found
        ],
        "coverage_runners": [r["runner"] for r in found if r["coverage"]],
        "files": [vars(f) for f in files],
    }


def render(report: dict) -> str:
    lines = [
        f"{report['total_tests']} tests in {report['total_files']} files"
        f"  |  {', '.join(report['languages']) or 'no test files found'}"
    ]
    lines.append("")
    for kind in ALL_KINDS:
        row = report["kinds"][kind]
        mark = {"covered": "  ", "gap": "--", "not applicable": "  "}[row["state"]]
        note = "" if row["state"] != "not applicable" else "   not expected here"
        lines.append(
            f" {mark} {kind:<12} {row['tests']:>4} tests  {row['files']:>3} files{note}"
        )

    if report["missing_kinds"]:
        lines.append("")
        for kind in report["missing_kinds"]:
            row = report["kinds"][kind]
            why = ", ".join(row["evidence"]) or "the repo layout"
            lines.append(f"No {kind} tests, and this repo wants them: "
                         f"{row['because']} ({why}).")

    if report.get("assertionless_count"):
        nothing = report.get("checks_nothing_count", 0)
        lines += ["", f"{report['assertionless_count']} test(s) with no explicit "
                      f"assertion, {nothing} of which cannot fail at all:"]
        for item in report["assertionless"]:
            if item.get("checks_nothing"):
                lines.append(f"  {item['path']} (cannot fail): "
                             f"{', '.join(item['checks_nothing'])}")
            if item.get("relies_on_no_raise"):
                lines.append(f"  {item['path']} (passes unless it raises): "
                             f"{', '.join(item['relies_on_no_raise'])}")

    if report.get("mock_setup_count"):
        lines += ["", f"{report['mock_setup_count']} problem(s) with the faked backend:"]
        for item in report["mock_setup"]:
            lines.append(f"  {item['path']}")
            lines.append(f"    {item['why']}")

    focused = [i for i in (report.get("never_run") or []) if i["kind"] == "focused"]
    if focused:
        lines += ["", f"{len(focused)} focused test(s) -- everything else in "
                      f"their file is silently not running:"]
        for item in focused:
            lines.append(f"  {item['path']}:{item['line']} {item['test']}")

    skipped = [i for i in (report.get("never_run") or []) if i["kind"] == "skipped"]
    if skipped:
        lines += ["", f"{len(skipped)} test(s) that never run:"]
        for item in skipped:
            lines.append(f"  {item['path']}::{item['test']} -- {item['why']}")

    if report.get("suspect_count"):
        lines += ["", f"{report['suspect_count']} suspect test(s) -- green, "
                      f"but not testing what they look like they test:"]
        for item in report["suspect"]:
            lines.append(f"  [{item['kind']}] {item['path']}::{item['test']}"
                         f" -- {item['why']}")

    if report["runners"]:
        lines += ["", "Runners found:"]
        for r in report["runners"]:
            note = "line coverage" if r["coverage"] else "no line coverage, test counts only"
            lines.append(f"  {r['runner']:<12} {r['command']}")
            lines.append(f"  {'':<12} {note}")
            if r.get("setup"):
                lines.append(f"  {'':<12} needs setup: {r['setup']}")
    else:
        lines += ["", "No test runner config found."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    root = Path(args.root)
    if not root.is_dir():
        print(f"gtest: not a directory: {root}", file=sys.stderr)
        return 2
    report = inventory(root)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


# --- self-check ---

def self_check() -> int:
    assert classify("tests/unit/test_math.py") == "unit"
    assert classify("tests/integration/test_api.py") == "integration"
    assert classify("tests/e2e/test_login.py") == "e2e"
    assert classify("e2e/checkout.spec.ts") == "e2e"
    assert classify("tests/smoke/test_boot.py") == "smoke"
    assert classify("bench/test_throughput.py") == "performance"
    assert classify("tests/contract/test_pact.py") == "contract"
    # the narrow kind wins when a path could be read two ways
    assert classify("tests/integration/e2e/test_x.py") == "e2e", "e2e is checked first"
    assert classify("src/test_util.py") == "unit", "no hint means unit"

    py = TEST_FILES[0][2]
    assert count_tests("def test_a():\n  pass\nasync def test_b():\n  pass\n", py) == 2
    assert count_tests("def helper():\n  pass\n", py) == 0
    js = TEST_FILES[1][2]
    assert count_tests("it('works', () => {})\ntest('also', () => {})\n", js) == 2
    assert count_tests("it.each([1])('x', () => {})\n", js) == 1, "it.each counts"
    assert count_tests("describe('group', () => {})\n", js) == 0, "describe is not a test"
    go = TEST_FILES[2][2]
    assert count_tests("func TestThing(t *testing.T) {}\nfunc helper() {}\n", go) == 1

    assert TEST_FILES[0][0].match("test_x.py") and TEST_FILES[0][0].match("x_test.py")
    assert not TEST_FILES[0][0].match("contest.py"), "only real test filenames"
    assert TEST_FILES[1][0].match("a.spec.tsx") and TEST_FILES[1][0].match("a.test.js")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tests" / "unit").mkdir(parents=True)
        (root / "tests" / "e2e").mkdir(parents=True)
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "tests" / "unit" / "test_math.py").write_text(
            "def test_add():\n  pass\ndef test_sub():\n  pass\n")
        (root / "tests" / "e2e" / "login.spec.ts").write_text("it('logs in', () => {})\n")
        (root / "node_modules" / "pkg" / "test_junk.py").write_text("def test_nope():\n  pass\n")
        (root / "pyproject.toml").write_text("[project]\nname='x'\n")
        (root / "playwright.config.ts").write_text("export default {}\n")

        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "outside_test.py"
            outside.write_text("def test_outside():\n  pass\n")
            link = root / "tests" / "linked_test.py"
            link.symlink_to(outside)
            linked = inventory(root)
            assert not any("linked" in f["path"] for f in linked["files"]), \
                "a symlinked test file must not be counted"
            link.unlink()

        report = inventory(root)
        assert report["total_files"] == 2, report["files"]
        assert report["total_tests"] == 3, report
        def counts(kind):
            row = report["kinds"][kind]
            return {"files": row["files"], "tests": row["tests"]}

        assert counts("unit") == {"files": 1, "tests": 2}, report["kinds"]
        assert counts("e2e") == {"files": 1, "tests": 1}, report["kinds"]
        # The fixture is a plain package with no server, no CLI and no API
        # surface, so the absent kinds are not applicable rather than gaps.
        assert report["missing_kinds"] == [], report["missing_kinds"]
        assert "contract" in report["not_applicable"], report["not_applicable"]
        assert "performance" in report["not_applicable"], report["not_applicable"]
        assert report["kinds"]["unit"]["state"] == "covered"
        assert report["kinds"]["contract"]["state"] == "not applicable"
        assert "not expected here" in render(report)
        assert sorted(report["languages"]) == ["javascript", "python"], report["languages"]

        names = [r["runner"] for r in report["runners"]]
        assert names == ["pytest", "playwright"], names
        assert set(report["coverage_runners"]) == {"pytest", "playwright"}, \
            report["coverage_runners"]
        found = {r["runner"]: r for r in report["runners"]}
        assert found["playwright"]["coverage"] is True, \
            "playwright reports coverage once the reporter is configured"
        assert found["playwright"]["setup"], "and the extra setup must be stated"
        assert found["pytest"]["report"] == "coverage.json"
        assert not found["pytest"]["setup"], "pytest-cov needs no extra wiring"

        text = render(report)
        assert "integration" in text and "not expected here" in text
        assert "No tests at all of this kind" not in text, \
            "the blanket complaint is gone; only real gaps are named"
        assert "needs setup:" in text, text

        # a test that asserts nothing is reported beside the counts
        (root / "tests" / "unit" / "test_bare.py").write_text(
            "def test_runs_nothing():\n    compute(1)\n")
        bare = inventory(root)
        by_path = {item["path"]: item for item in bare["assertionless"]}
        assert by_path["tests/unit/test_bare.py"]["tests"] == ["test_runs_nothing"], by_path
        assert by_path["tests/unit/test_bare.py"]["relies_on_no_raise"] == \
            ["test_runs_nothing"], "a bare call only fails if it raises"
        assert by_path["tests/unit/test_math.py"]["checks_nothing"] == \
            ["test_add", "test_sub"], "a pass-bodied test can never fail"
        # the JavaScript file is read too: it('logs in', () => {}) checks nothing
        assert "tests/e2e/login.spec.ts" in by_path, by_path
        assert by_path["tests/e2e/login.spec.ts"]["checks_nothing"] == ["logs in"], by_path
        assert bare["checks_nothing_count"] == 3, bare
        # the fixture's own `pass`-bodied tests are flagged too, as they should be
        assert by_path["tests/unit/test_math.py"]["tests"] == ["test_add", "test_sub"], by_path
        assert bare["assertionless_count"] == 4, bare["assertionless"]
        assert "no explicit assertion" in render(bare)
        assert "cannot fail" in render(bare) and "unless it raises" in render(bare)
        (root / "tests" / "unit" / "test_bare.py").unlink()

        # a test file that does assert is not flagged
        (root / "tests" / "unit" / "test_math.py").write_text(
            "def test_add():\n    assert 1 + 1 == 2\n")
        # only the empty JavaScript test is left, which is also correct
        still = inventory(root)
        assert still["assertionless_count"] == 1, still["assertionless"]
        assert still["assertionless"][0]["path"] == "tests/e2e/login.spec.ts"
        (root / "tests" / "unit" / "test_math.py").write_text(
            "def test_add():\n  pass\ndef test_sub():\n  pass\n")

        # a tautology is reported next to the counts, not buried
        (root / "tests" / "unit" / "test_fake.py").write_text(
            "def test_always_passes():\n    run()\n    assert True\n")
        sus = inventory(root)
        by_kind = {item["kind"]: item for item in sus["suspect"]}
        assert by_kind["always_true"]["test"] == "test_always_passes", sus["suspect"]
        # the fixture's two pass-bodied tests are the same test twice, and the
        # duplicate check is right to say so
        assert by_kind["duplicate"]["test"] == "test_sub", sus["suspect"]
        assert sus["suspect_count"] == 2, sus["suspect"]
        assert "suspect test" in render(sus)
        (root / "tests" / "unit" / "test_fake.py").unlink()
        assert inventory(root)["suspect_count"] == 1, "the duplicate pair remains"

        every_runner = {r["runner"]: r for r in RUNNERS}
        assert all("setup" in r for r in RUNNERS), "every runner declares its setup"
        assert every_runner["go test"]["coverage"] is True, "the Go profile is parsed now"
        assert every_runner["cypress"]["coverage"] is True

        empty = inventory(Path(tmp) / "nothing")
        assert empty["total_files"] == 0 and empty["runners"] == []
        # Units are expected of any code; nothing else is, with no evidence.
        assert empty["missing_kinds"] == ["unit"], empty["missing_kinds"]
        assert set(empty["not_applicable"]) == set(ALL_KINDS) - {"unit"}, empty
        assert "No test runner config found" in render(empty)

    # A kind becomes a gap only once something in the repo asks for it.
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp)
        (app / "pyproject.toml").write_text(
            "[project]\nname='api'\ndependencies = ['fastapi', 'sqlalchemy']\n"
            "[project.scripts]\napi = 'api:main'\n"
        )
        (app / "openapi.json").write_text("{}")
        (app / "tests").mkdir()
        (app / "tests" / "test_unit.py").write_text("def test_x():\n    assert f() == 1\n")

        wants = expectations(app)
        assert wants["e2e"]["expected"], wants["e2e"]
        assert "fastapi" in wants["e2e"]["evidence"], wants["e2e"]
        assert wants["integration"]["expected"], wants["integration"]
        assert "sqlalchemy" in wants["integration"]["evidence"], wants["integration"]
        assert wants["contract"]["expected"], wants["contract"]
        assert "openapi.json" in wants["contract"]["evidence"], wants["contract"]
        assert wants["smoke"]["expected"], "an installed entry point has to start"
        assert not wants["performance"]["expected"], "no benchmarking tool, no claim"

        report = inventory(app)
        assert set(report["missing_kinds"]) == {"e2e", "smoke", "contract", "integration"}, \
            report["missing_kinds"]
        assert report["not_applicable"] == ["performance"], report["not_applicable"]
        text = render(report)
        assert "No e2e tests, and this repo wants them" in text, text
        assert "fastapi" in text, "the evidence is shown, not just the verdict"
        assert "No performance tests" not in text, "a non-applicable kind is not nagged about"

        # a library with none of that evidence gets none of those gaps
        lib = Path(tmp) / "lib"
        (lib / "tests").mkdir(parents=True)
        (lib / "pyproject.toml").write_text("[project]\nname='lib'\n")
        (lib / "tests" / "test_unit.py").write_text("def test_x():\n    assert f() == 1\n")
        assert inventory(lib)["missing_kinds"] == [], inventory(lib)["missing_kinds"]

    # dependency names match with - and _ interchangeable, and not inside words
    assert _mentions("pytest-benchmark", "pytest_benchmark")
    assert _mentions('"react": "^18"', "react")
    assert not _mentions("reactive-streams", "react"), "a longer name is a different name"
    assert not _mentions("my_flask_helper", "flask"), "not a substring of another name"

    # a faked backend is found wherever its setup lives, not just in tests
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp)
        (app / "src" / "mocks").mkdir(parents=True)
        (app / "src" / "mocks" / "server.ts").write_text(
            "import { setupServer } from 'msw/node'\n"
            "export const server = setupServer(...handlers)\n"
            "beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))\n",
            encoding="utf-8",
        )
        (app / "package.json").write_text('{"devDependencies": {"msw": "^2.0.0"}}')
        (app / "tests").mkdir()
        (app / "tests" / "a.spec.ts").write_text("it('x', () => { expect(1).toBe(1) })")

        mocks = mock_setups(app)
        kinds_found = {m["kind"] for m in mocks}
        assert kinds_found == {"msw_lenient", "msw_no_reset"}, mocks
        assert mocks[0]["path"].endswith("server.ts"), mocks

        report = inventory(app)
        assert report["mock_setup_count"] == 2, report["mock_setup"]
        assert "faked backend" in render(report)
        # msw in the manifest makes contract tests worth having
        assert "contract" in report["missing_kinds"], report["missing_kinds"]
        assert "msw" in report["kinds"]["contract"]["evidence"], report["kinds"]["contract"]

        # Python fakes are checked too, in conftest and in the tests themselves
        py = Path(tmp) / "py"
        (py / "tests").mkdir(parents=True)
        (py / "tests" / "conftest.py").write_text(
            "import responses\n"
            "mock = responses.RequestsMock(assert_all_requests_are_fired=False)\n",
            encoding="utf-8",
        )
        (py / "tests" / "test_a.py").write_text(
            "from unittest.mock import patch\n"
            "def test_x():\n"
            "    with patch('app.send') as send:\n"
            "        run()\n"
            "    send.assert_called_once_with(1)\n",
            encoding="utf-8",
        )
        py_found = {m["kind"] for m in mock_setups(py)}
        assert py_found == {"mock_unfired", "mock_no_spec"}, mock_setups(py)
        assert "faked backend" in render(inventory(py))

        # a repo with no mocking setup reports nothing about one
        plain = Path(tmp) / "plain"
        (plain / "tests").mkdir(parents=True)
        (plain / "tests" / "a.spec.ts").write_text("it('x', () => { expect(1).toBe(1) })")
        assert mock_setups(plain) == ()
        assert "faked backend" not in render(inventory(plain))

    print("stacks.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
