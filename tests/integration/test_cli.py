"""Integration: run each script as a real subprocess, end to end.

The unit tests import the modules. These exercise what the skill actually
does -- spawn the script with arguments and read stdout and the exit code.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "coverage-gaps"


def run(script, *args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, cwd=cwd, timeout=120,
    )


def write_coverage(tmp_path, files):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"meta": {"version": "7"}, "files": files}))
    return path


def test_rank_prints_a_ranked_report(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def expire(now):\n    if now:\n        return True\n    return False\n")
    cov = write_coverage(tmp_path, {
        "src/auth.py": {"summary": {"num_statements": 4},
                        "missing_lines": [2, 3], "missing_branches": [[2, 3]]},
    })
    done = run("rank.py", "--coverage", str(cov), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "src/auth.py:2-3" in done.stdout
    assert "risky path (auth)" in done.stdout
    assert "-> expire" in done.stdout, "the owning function should be named"


def test_rank_json_mode_is_parseable(tmp_path):
    cov = write_coverage(tmp_path, {
        "src/a.py": {"summary": {"num_statements": 2}, "missing_lines": [1]}})
    done = run("rank.py", "--coverage", str(cov), "--json", cwd=tmp_path)
    payload = json.loads(done.stdout)
    assert payload["summary"]["files"] == 1
    assert payload["gaps"][0]["file"] == "src/a.py"


def test_rank_explains_a_missing_report(tmp_path):
    done = run("rank.py", "--coverage", str(tmp_path / "nope.json"))
    assert done.returncode == 2
    assert "no coverage report at" in done.stderr


def test_rank_explains_a_report_it_cannot_read(tmp_path):
    bad = tmp_path / "coverage.json"
    bad.write_text("{not json")
    done = run("rank.py", "--coverage", str(bad))
    assert done.returncode == 2
    assert "neither JSON nor a Go coverprofile" in done.stderr


def test_rank_reads_a_go_coverprofile(tmp_path):
    profile = tmp_path / "coverage.out"
    profile.write_text(
        "mode: count\n"
        "github.com/me/app/pkg/auth.go:10.13,12.2 2 0\n"
    )
    done = run("rank.py", "--coverage", str(profile), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "pkg/auth.go:10-12" in done.stdout


def test_rank_explains_an_unknown_report_shape(tmp_path):
    odd = tmp_path / "coverage.json"
    odd.write_text(json.dumps({"totals": {"percent": 100}}))
    done = run("rank.py", "--coverage", str(odd))
    assert done.returncode == 2
    assert "unrecognised coverage report" in done.stderr


def test_rank_says_so_when_every_file_was_skipped(tmp_path):
    cov = write_coverage(tmp_path, {"tests/test_a.py": {"missing_lines": [1]}})
    done = run("rank.py", "--coverage", str(cov), cwd=tmp_path)
    assert done.returncode == 0
    assert "no measurable source files" in done.stdout


def test_rank_requires_a_report_or_a_self_check():
    done = run("rank.py")
    assert done.returncode == 2
    assert "--coverage is required" in done.stderr


def test_rank_refuses_an_injected_git_ref(tmp_path):
    cov = write_coverage(tmp_path, {
        "src/a.py": {"summary": {"num_statements": 2}, "missing_lines": [1]}})
    # the `=` form is the real risk: argparse takes it as the value for --diff
    done = run("rank.py", "--coverage", str(cov), "--diff=--upload-pack=evil", cwd=tmp_path)
    assert done.returncode == 2
    assert "refusing" in done.stderr, done.stderr
    # the bare form never reaches the validation, argparse refuses it first
    loose = run("rank.py", "--coverage", str(cov), "--diff", "--upload-pack=evil", cwd=tmp_path)
    assert loose.returncode == 2


def test_flaky_reports_a_disagreeing_test(tmp_path):
    good = tmp_path / "run1.xml"
    bad = tmp_path / "run2.xml"
    good.write_text('<testsuites><testsuite><testcase classname="t" name="a"/>'
                    '</testsuite></testsuites>')
    bad.write_text('<testsuites><testsuite><testcase classname="t" name="a">'
                   '<failure/></testcase></testsuite></testsuites>')
    done = run("flaky.py", str(good), str(bad))
    assert done.returncode == 1, done.stderr
    assert "FLAKY" in done.stdout and "t::a" in done.stdout


def test_flaky_needs_two_runs(tmp_path):
    only = tmp_path / "run1.xml"
    only.write_text('<testsuites><testsuite><testcase name="a"/></testsuite></testsuites>')
    done = run("flaky.py", str(only))
    assert done.returncode == 2
    assert "at least two" in done.stderr


def test_flaky_refuses_an_xml_bomb(tmp_path):
    for name in ("run1.xml", "run2.xml"):
        (tmp_path / name).write_text(
            '<!DOCTYPE x [<!ENTITY e "boom">]>'
            '<testsuites><testsuite><testcase name="a"/></testsuite></testsuites>')
    done = run("flaky.py", str(tmp_path / "run1.xml"), str(tmp_path / "run2.xml"))
    assert done.returncode == 2
    assert "DTD or entity" in done.stderr


def test_stacks_inventories_a_mixed_repo(tmp_path):
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text("def test_x():\n  pass\n")
    (tmp_path / "tests" / "e2e" / "a.spec.ts").write_text("it('x', () => {})\n")
    (tmp_path / "playwright.config.ts").write_text("export default {}\n")
    done = run("stacks.py", str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "2 tests in 2 files" in done.stdout
    assert "playwright" in done.stdout
    # the kinds nobody asked for are marked, not complained about
    assert "not expected here" in done.stdout
    assert "No tests at all of this kind" not in done.stdout


def test_stacks_rejects_a_path_that_is_not_a_directory(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("x")
    done = run("stacks.py", str(file))
    assert done.returncode == 2
    assert "not a directory" in done.stderr


def test_pysource_lists_functions_with_complexity(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("def f(xs):\n    for x in xs:\n        if x:\n            return x\n")
    done = run("pysource.py", str(path))
    assert done.returncode == 0, done.stderr
    assert "c=3" in done.stdout and "f" in done.stdout
