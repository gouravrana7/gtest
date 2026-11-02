"""Unit: the argparse layer of each script, called in process.

The integration suite spawns these as subprocesses, which proves they work
but measures nothing. Calling main() directly covers the wiring: exit codes,
the required-argument errors and the self-check entry point.
"""
import json

import pytest
import flaky
import pysource
import rank
import stacks

MAINS = [rank.main, flaky.main, pysource.main, stacks.main]


@pytest.mark.parametrize("main", MAINS)
def test_self_check_exits_clean(main, capsys):
    assert main(["--self-check"]) == 0
    assert "all assertions passed" in capsys.readouterr().out


@pytest.mark.parametrize("main", [rank.main, flaky.main, pysource.main])
def test_missing_required_argument_is_a_usage_error(main):
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_stacks_defaults_to_the_current_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert stacks.main([]) == 0
    assert "0 tests in 0 files" in capsys.readouterr().out


def test_stacks_json_mode(tmp_path, capsys):
    (tmp_path / "test_a.py").write_text("def test_x():\n  pass\n")
    assert stacks.main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_tests"] == 1


def test_stacks_rejects_a_file(tmp_path, capsys):
    target = tmp_path / "a.txt"
    target.write_text("x")
    assert stacks.main([str(target)]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_pysource_lists_functions(tmp_path, capsys):
    target = tmp_path / "m.py"
    target.write_text("def f():\n    if 1:\n        return 2\n")
    assert pysource.main([str(target)]) == 0
    out = capsys.readouterr().out
    assert "c=2" in out and " f" in out


def test_pysource_rejects_a_missing_file(tmp_path, capsys):
    assert pysource.main([str(tmp_path / "gone.py")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_rank_diff_mode_narrows_to_changed_lines(tmp_path, monkeypatch, capsys):
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"files": {
        "src/a.py": {"summary": {"num_statements": 10}, "missing_lines": [1, 2, 3]}}}))
    monkeypatch.setattr(rank, "changed_lines", lambda base: {"src/a.py": frozenset({2})})
    assert rank.main(["--coverage", str(cov), "--diff", "main", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gaps"][0]["missed_lines"] == [2]


def test_rank_surfaces_a_git_failure(tmp_path, monkeypatch, capsys):
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"files": {
        "src/a.py": {"summary": {"num_statements": 2}, "missing_lines": [1]}}}))

    def boom(base):
        raise RuntimeError("git diff failed: no such ref")

    monkeypatch.setattr(rank, "changed_lines", boom)
    assert rank.main(["--coverage", str(cov), "--diff", "nope"]) == 2
    assert "no such ref" in capsys.readouterr().err


def test_rank_without_pysource_still_ranks(tmp_path, monkeypatch):
    """The function map is a bonus; losing it must not break the report."""
    monkeypatch.setattr(rank, "pysource", None)
    rows = rank.rank((rank.Gap("src/a.py", (1,), 0, 4),), frozenset(), root=tmp_path)
    assert rows[0]["functions"] == []
    assert rows[0]["complexity"] == 0


def test_a_path_with_a_null_byte_is_treated_as_outside(tmp_path):
    assert pysource.inside("bad\x00path.py", tmp_path) is False
    assert pysource.group("bad\x00path.py", (1,), root=tmp_path) == ()


def test_unreadable_source_is_skipped_not_fatal(tmp_path):
    binary = tmp_path / "m.py"
    binary.write_bytes(b"\xff\xfe def f(): pass")
    assert pysource.group(binary, (1,), root=tmp_path) == ()


def test_a_file_the_walker_cannot_read_is_counted_as_empty(tmp_path, monkeypatch):
    (tmp_path / "test_a.py").write_text("def test_x():\n  pass\n")

    original = Path = type(tmp_path)

    def explode(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(original, "read_text", explode)
    report = stacks.inventory(tmp_path)
    assert report["total_files"] == 1
    assert report["files"][0]["tests"] == 0
