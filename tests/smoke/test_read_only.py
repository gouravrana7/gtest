"""Smoke: scanning must never change anything.

Every script here reads. The one exception is `mutate.py --apply`, which
exists to edit a file on purpose and refuses to run unless git can put it
back. This test is the guard: it snapshots a whole fixture repo, runs every
script in scan mode against it, and fails if a single byte moved.

A promise in a README is not a guard. This is.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "coverage-gaps"
TIMEOUT = 180


def snapshot(root: Path) -> dict[str, str]:
    """Every file under root, by path, with the hash of its bytes."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fixture repo with something for every script to find."""
    files = {
        "pyproject.toml": "[project]\nname='demo'\ndependencies=['fastapi','msw']\n",
        "package.json": '{"devDependencies": {"msw": "^2.0.0", "vitest": "^2.0.0"}}',
        "playwright.config.ts": "export default {}\n",
        "openapi.json": "{}",
        "src/auth.py": (
            "def allowed(age, member):\n"
            "    if age >= 18 and member:\n"
            "        return True\n"
            "    return False\n"
        ),
        "src/mocks/server.ts": (
            "import { setupServer } from 'msw/node'\n"
            "const server = setupServer(...handlers)\n"
            "beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))\n"
        ),
        "tests/conftest.py": "import pytest\n",
        "tests/test_unit.py": (
            "import pytest\n\n"
            "@pytest.fixture\ndef orphan():\n    return 1\n\n"
            "@pytest.mark.skip\ndef test_dead():\n    assert allowed(18, True)\n\n"
            "def test_drops_it():\n    result = allowed(18, True)\n\n"
            "def test_always():\n    allowed(1, True)\n    assert True\n"
        ),
        "tests/e2e/login.spec.ts": (
            "it.only('logs in', async ({ page }) => {\n"
            "  await expect(page).toHaveTitle(/x/)\n"
            "})\n"
        ),
        "coverage.json": json.dumps({
            "meta": {"version": "7"},
            "files": {"src/auth.py": {
                "summary": {"num_statements": 4},
                "missing_lines": [3, 4],
                "missing_branches": [[2, 3]],
            }},
        }),
        "run1.xml": (
            '<testsuites><testsuite><testcase classname="t" name="a" time="1"/>'
            "</testsuite></testsuites>"
        ),
        "run2.xml": (
            '<testsuites><testsuite><testcase classname="t" name="a" time="2">'
            "<failure/></testcase></testsuite></testsuites>"
        ),
    }
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd,
    )


SCANS = [
    ("stacks.py", (".", "--json")),
    ("stacks.py", (".",)),
    ("rank.py", ("--coverage", "coverage.json", "--json")),
    ("rank.py", ("--coverage", "coverage.json")),
    ("flaky.py", ("run1.xml", "run2.xml", "--json")),
    ("pysource.py", ("src/auth.py",)),
    ("pysource.py", ("tests/test_unit.py", "--assertionless")),
    ("pysource.py", ("tests/test_unit.py", "--suspect")),
    ("pysource.py", ("tests/test_unit.py", "--skips")),
    ("jssource.py", ("tests/e2e/login.spec.ts",)),
    ("mutate.py", ("--file", "src/auth.py")),
    ("mutate.py", ("--file", "src/auth.py", "--json", "--limit", "20")),
]


@pytest.mark.parametrize("script,args", SCANS, ids=[f"{s} {' '.join(a)}" for s, a in SCANS])
def test_a_scan_changes_nothing(repo: Path, script: str, args: tuple[str, ...]):
    before = snapshot(repo)
    done = run(script, *args, cwd=repo)
    assert done.returncode in (0, 1), done.stderr
    assert snapshot(repo) == before, f"{script} {' '.join(args)} modified the tree"


def test_the_whole_scan_sequence_changes_nothing(repo: Path):
    """The order the skill runs them in, back to back."""
    before = snapshot(repo)
    for script, args in SCANS:
        run(script, *args, cwd=repo)
    assert snapshot(repo) == before


def test_a_scan_creates_no_files(repo: Path):
    before = set(snapshot(repo))
    for script, args in SCANS:
        run(script, *args, cwd=repo)
    assert set(snapshot(repo)) == before, "a scan must not leave files behind"


def test_the_diagram_changes_nothing(repo: Path):
    stacks = run("stacks.py", ".", "--json", cwd=repo)
    (repo / "stacks.json").write_text(stacks.stdout)
    before = snapshot(repo)
    done = run("diagram.py", "--stacks", "stacks.json", cwd=repo)
    assert done.returncode == 0, done.stderr
    assert snapshot(repo) == before


def test_mutating_is_refused_outside_a_repository(repo: Path):
    """The one script that can write refuses when git could not undo it."""
    before = snapshot(repo)
    done = run("mutate.py", "--file", "src/auth.py", "--apply", "0", cwd=repo)
    assert done.returncode == 2
    assert "refusing to mutate" in done.stderr
    assert snapshot(repo) == before, "a refused mutation must leave the file alone"


def test_mutating_is_refused_on_a_modified_file(tmp_path: Path):
    """Committed, then edited: git restore would lose the edit, so it refuses."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=tmp_path, check=True, timeout=TIMEOUT)
    target = tmp_path / "a.py"
    target.write_text("def f(x):\n    return x >= 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True,
                   timeout=TIMEOUT)

    target.write_text("def f(x):\n    return x >= 2\n")
    edited = target.read_text()
    done = run("mutate.py", "--file", "a.py", "--apply", "0", cwd=tmp_path)
    assert done.returncode == 2
    assert "refusing to mutate" in done.stderr
    assert target.read_text() == edited, "the uncommitted edit must survive"


def test_mutating_a_clean_file_changes_exactly_one_line(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=tmp_path, check=True, timeout=TIMEOUT)
    target = tmp_path / "a.py"
    original = "def f(x):\n    return x >= 1\n"
    target.write_text(original)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=TIMEOUT)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True,
                   timeout=TIMEOUT)

    done = run("mutate.py", "--file", "a.py", "--apply", "0", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    mutated = target.read_text()
    assert mutated != original
    differing = sum(1 for a, b in zip(original.splitlines(), mutated.splitlines())
                    if a != b)
    assert differing == 1, mutated

    restored = run("mutate.py", "--file", "a.py", "--restore", cwd=tmp_path)
    assert restored.returncode == 0, restored.stderr
    assert target.read_text() == original, "restore must be exact"
