"""Unit: the Go coverprofile adapter.

Go writes a text profile, not JSON. Every other adapter reads JSON, so this
is the one that has to agree with the shared Gap record on its own.
"""
from pathlib import Path

import pytest
import rank

PROFILE = """mode: count
github.com/me/app/pkg/auth.go:10.13,12.2 2 0
github.com/me/app/pkg/auth.go:14.2,15.3 1 7
github.com/me/app/pkg/pay.go:3.1,4.2 1 0
"""


def test_a_profile_is_recognised_by_its_mode_line():
    assert rank.is_go_profile(PROFILE)
    assert rank.is_go_profile("   mode: set\n")
    assert not rank.is_go_profile('{"files": {}}')


def test_uncovered_blocks_become_line_ranges():
    gaps = {g.path: g for g in rank.from_go_coverprofile(PROFILE, Path("/nowhere"))}
    assert gaps["pkg/auth.go"].missed == (10, 11, 12)
    assert gaps["pkg/pay.go"].missed == (3, 4)


def test_statements_count_covered_and_uncovered_blocks():
    gaps = {g.path: g for g in rank.from_go_coverprofile(PROFILE, Path("/nowhere"))}
    assert gaps["pkg/auth.go"].statements == 3


def test_a_profile_carries_no_branch_data():
    for gap in rank.from_go_coverprofile(PROFILE, Path("/nowhere")):
        assert gap.branches == 0


def test_blocks_run_more_than_zero_times_are_not_gaps():
    only_covered = "mode: set\ngithub.com/me/app/pkg/a.go:1.1,2.2 1 3\n"
    gaps = rank.from_go_coverprofile(only_covered, Path("/nowhere"))
    assert gaps[0].missed == ()


def test_unparseable_lines_are_ignored():
    noisy = PROFILE + "this is not a block\n\n"
    assert len(rank.from_go_coverprofile(noisy, Path("/nowhere"))) == 2


def test_go_test_files_are_skipped_like_every_other_language():
    with_test = PROFILE + "github.com/me/app/pkg/auth_test.go:1.1,2.2 1 0\n"
    paths = [g.path for g in rank.load_report(with_test, Path("/nowhere"))]
    assert "pkg/auth_test.go" not in paths
    assert sorted(paths) == ["pkg/auth.go", "pkg/pay.go"]


def test_an_import_path_resolves_against_the_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "auth.go").write_text("package pkg\n")
    assert rank.go_path("github.com/me/app/pkg/auth.go", tmp_path) == "pkg/auth.go"


def test_an_unresolvable_path_drops_the_module_prefix(tmp_path):
    assert rank.go_path("github.com/me/app/pkg/x.go", tmp_path) == "pkg/x.go"


def test_a_path_with_no_module_prefix_is_left_alone(tmp_path):
    assert rank.go_path("pkg/x.go", tmp_path) == "pkg/x.go"


def test_load_report_dispatches_on_content_not_filename():
    assert [g.path for g in rank.load_report(PROFILE, Path("/nowhere"))]
    as_json = '{"files": {"src/a.py": {"missing_lines": [1], "summary": {"num_statements": 2}}}}'
    assert [g.path for g in rank.load_report(as_json)] == ["src/a.py"]


def test_a_report_that_is_neither_is_refused():
    with pytest.raises(ValueError, match="neither JSON nor a Go coverprofile"):
        rank.load_report("hello")


def test_a_go_profile_ranks_like_any_other_report(tmp_path):
    gaps = rank.load_report(PROFILE, tmp_path)
    rows = rank.rank(gaps, frozenset())
    assert [r["file"] for r in rows] == ["pkg/auth.go", "pkg/pay.go"]
    assert rows[0]["ranges"] == "10-12"
    assert rows[0]["functions"] == [], "function mapping is Python only"
