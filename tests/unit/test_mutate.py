"""Unit: proposing mutants, and the guard that stops one escaping.

`--apply` is the only write anywhere in this plugin, so most of these are
about what it refuses to do.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import mutate

SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def allowed(age, member):\n"
    "    if age >= 18 and member:\n"
    "        return True\n"
    "    return False\n"
)


def swaps(source):
    return {(m.was, m.now) for m in mutate.mutants(source)}


@pytest.mark.parametrize("code,was,now", [
    ("x = a >= b\n", ">=", ">"),
    ("x = a <= b\n", "<=", "<"),
    ("x = a > b\n", ">", ">="),
    ("x = a < b\n", "<", "<="),
    ("x = a == b\n", "==", "!="),
    ("x = a != b\n", "!=", "=="),
    ("x = a + b\n", "+", "-"),
    ("x = a - b\n", "-", "+"),
    ("x = a and b\n", "and", "or"),
    ("x = a or b\n", "or", "and"),
    ("x = True\n", "True", "False"),
    ("x = False\n", "False", "True"),
])
def test_each_operator_is_swapped(code, was, now):
    assert (was, now) in swaps(code)


def test_a_comparison_in_a_comment_is_not_code():
    assert mutate.mutants("x = 1  # a >= b\n") == ()


def test_a_comparison_in_a_string_is_not_code():
    assert mutate.mutants('x = "a >= b"\n') == ()


def test_a_comparison_in_a_triple_quoted_string_is_not_code():
    assert mutate.mutants('x = """\na >= b\n"""\n') == ()


def test_import_lines_are_left_alone():
    found = mutate.mutants(SOURCE)
    assert all("import" not in m.source_line for m in found)


def test_a_pragma_line_is_left_alone():
    assert mutate.mutants("x = a >= b  # pragma: no cover\n") == ()


def test_every_mutant_still_parses():
    lines = SOURCE.splitlines(keepends=True)
    for mutant in mutate.mutants(SOURCE):
        rewritten = mutate._rewrite(lines, mutant.line, mutant.column,
                                    mutant.was, mutant.now)
        assert rewritten is not None
        assert rewritten != SOURCE


def test_a_mutant_changes_exactly_one_line():
    lines = SOURCE.splitlines(keepends=True)
    for mutant in mutate.mutants(SOURCE):
        rewritten = mutate._rewrite(lines, mutant.line, mutant.column,
                                    mutant.was, mutant.now)
        assert mutate._only_one_line_differs(SOURCE, rewritten)


def test_indexes_are_stable_and_contiguous():
    found = mutate.mutants(SOURCE)
    assert [m.index for m in found] == list(range(len(found)))


def test_unparseable_source_yields_no_mutants():
    assert mutate.mutants("def broken(:\n") == ()
    assert mutate.mutants("") == ()


def test_a_rewrite_that_would_not_parse_is_discarded():
    # the token is not where the caller claims, so nothing is rewritten
    assert mutate._rewrite(["x = 1\n"], 1, 0, ">=", ">") is None


@pytest.mark.parametrize("before,after,one", [
    ("a\nb\n", "a\nc\n", True),
    ("a\nb\n", "x\ny\n", False),
    ("a\nb\n", "a\nb\nc\n", False),
    ("a\nb\n", "a\nb\n", False),
])
def test_the_one_line_check(before, after, one):
    assert mutate._only_one_line_differs(before, after) is one


def test_the_summary_names_the_swap_and_the_reason():
    mutant = mutate.mutants(SOURCE)[0]
    text = mutant.summary()
    assert mutant.was in text and mutant.now in text
    assert f"line {mutant.line}" in text
    assert mutant.describes in text


def test_the_render_states_the_cost_and_honours_the_limit():
    found = mutate.mutants(SOURCE)
    text = mutate.render(found, 2)
    assert "one test run each" in text
    assert "run and do not check" in text
    assert "[0]" in text and "[1]" in text and "[2]" not in text
    assert "raise --limit" in text


def test_the_render_says_so_when_there_is_nothing_to_mutate():
    assert mutate.render((), 2) == "No mutants available here."


def test_the_default_limit_is_two():
    assert mutate.DEFAULT_LIMIT == 2


def test_listing_requires_a_file():
    with pytest.raises(SystemExit) as exit_info:
        mutate.main([])
    assert exit_info.value.code == 2


def test_listing_a_missing_file_is_a_clean_error(tmp_path, capsys):
    assert mutate.main(["--file", str(tmp_path / "gone.py")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_listing_prints_the_mutants(tmp_path, capsys):
    target = tmp_path / "a.py"
    target.write_text(SOURCE)
    assert mutate.main(["--file", str(target)]) == 0
    assert "one test run each" in capsys.readouterr().out


def test_the_json_output_carries_the_limit(tmp_path, capsys):
    target = tmp_path / "a.py"
    target.write_text(SOURCE)
    assert mutate.main(["--file", str(target), "--json", "--limit", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["limit"] == 1
    assert len(payload["mutants"]) == 1
    assert payload["available"] > 1


def test_listing_never_touches_the_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text(SOURCE)
    mutate.main(["--file", str(target)])
    assert target.read_text() == SOURCE


def test_applying_outside_a_repository_is_refused(tmp_path):
    target = tmp_path / "a.py"
    target.write_text(SOURCE)
    with pytest.raises(ValueError, match="refusing to mutate"):
        mutate.apply(target, 0)
    assert target.read_text() == SOURCE


def test_applying_to_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no such file"):
        mutate.apply(tmp_path / "gone.py", 0)


def git_repo(tmp_path: Path, body: str) -> Path:
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, timeout=120)
    target = tmp_path / "a.py"
    target.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=120)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True,
                   timeout=120)
    return target


def test_a_committed_file_reports_clean(tmp_path):
    target = git_repo(tmp_path, SOURCE)
    assert mutate.git_state(target) == "clean"


def test_an_edited_file_reports_modified(tmp_path):
    target = git_repo(tmp_path, SOURCE)
    target.write_text(SOURCE + "x = 1\n")
    assert mutate.git_state(target) == "modified"


def test_a_new_file_reports_untracked(tmp_path):
    git_repo(tmp_path, SOURCE)
    other = tmp_path / "b.py"
    other.write_text("x = 1\n")
    assert mutate.git_state(other) == "untracked"


def test_applying_to_a_modified_file_is_refused(tmp_path):
    target = git_repo(tmp_path, SOURCE)
    edited = SOURCE + "x = 1\n"
    target.write_text(edited)
    with pytest.raises(ValueError, match="it is modified"):
        mutate.apply(target, 0)
    assert target.read_text() == edited, "the uncommitted edit must survive"


def test_applying_an_index_that_does_not_exist_is_refused(tmp_path):
    target = git_repo(tmp_path, SOURCE)
    with pytest.raises(ValueError, match="does not exist"):
        mutate.apply(target, 999)
    assert target.read_text() == SOURCE


def test_applying_to_a_file_with_nothing_to_mutate_is_refused(tmp_path):
    target = git_repo(tmp_path, "x = 1\n")
    with pytest.raises(ValueError, match="no mutants available"):
        mutate.apply(target, 0)


def test_applying_then_restoring_round_trips(tmp_path):
    target = git_repo(tmp_path, SOURCE)
    summary = mutate.apply(target, 0)
    assert "->" in summary
    mutated = target.read_text()
    assert mutated != SOURCE
    assert mutate._only_one_line_differs(SOURCE, mutated)

    mutate.restore(target)
    assert target.read_text() == SOURCE


def test_restoring_a_file_git_does_not_know_is_an_error(tmp_path):
    git_repo(tmp_path, SOURCE)
    with pytest.raises(ValueError, match="could not restore"):
        mutate.restore(tmp_path / "nope.py")


def test_the_cli_applies_and_restores(tmp_path, capsys):
    target = git_repo(tmp_path, SOURCE)
    assert mutate.main(["--file", str(target), "--apply", "0"]) == 0
    assert "--restore" in capsys.readouterr().out
    assert target.read_text() != SOURCE

    assert mutate.main(["--file", str(target), "--restore"]) == 0
    assert "restored" in capsys.readouterr().out
    assert target.read_text() == SOURCE


def test_the_cli_reports_a_refusal_without_a_traceback(tmp_path, capsys):
    target = tmp_path / "a.py"
    target.write_text(SOURCE)
    assert mutate.main(["--file", str(target), "--apply", "0"]) == 2
    assert "refusing to mutate" in capsys.readouterr().err
