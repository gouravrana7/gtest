"""Unit: coverage adapters, scoring and the diff intersection."""
import json

import pytest
import rank


def report(files):
    return {"meta": {"version": "7"}, "files": files}


def test_coverage_py_adapter_reads_lines_branches_and_statements():
    gaps = rank.load(report({
        "src/a.py": {
            "summary": {"num_statements": 8},
            "missing_lines": [2, 3],
            "missing_branches": [[1, 2]],
        }
    }))
    assert len(gaps) == 1
    assert gaps[0].missed == (2, 3)
    assert gaps[0].branches == 1
    assert gaps[0].statements == 8


def test_adapter_tolerates_missing_optional_keys():
    gaps = rank.load(report({"src/a.py": {"missing_lines": [1]}}))
    assert gaps[0].statements == 0
    assert gaps[0].branches == 0


def test_adapter_ignores_non_dict_file_entries():
    assert rank.load(report({"src/a.py": "nonsense"})) == ()


@pytest.mark.parametrize("path", [
    "tests/test_a.py", "src/a_test.py", "conftest.py", "pkg/__init__.py",
    "app/migrations/0001_init.py", "proto/thing_pb2.py", "web/a.spec.ts",
    "node_modules/x/index.js", "dist/bundle.js",
])
def test_paths_nobody_should_test_are_skipped(path):
    assert rank.load(report({path: {"missing_lines": [1]}})) == ()


@pytest.mark.parametrize("path", ["src/app.py", "src/contest.py", "lib/testing.py"])
def test_real_source_paths_survive(path):
    assert len(rank.load(report({path: {"missing_lines": [1]}}))) == 1


def test_missing_branches_outweigh_plain_lines():
    plain = rank.Gap("src/a.py", (1,), 0, 10)
    branchy = rank.Gap("src/b.py", (1,), 3, 10)
    assert rank.score(branchy, frozenset())[0] > rank.score(plain, frozenset())[0]


def test_every_weight_is_reachable():
    gap = rank.Gap("src/auth/pay.py", tuple(range(1, 11)), 2, 10)
    points, reasons = rank.score(gap, frozenset({"src/auth/pay.py"}), peak=12)
    assert points == sum(rank.WEIGHTS.values())
    assert len(reasons) == len(rank.WEIGHTS)


def test_score_is_zero_without_any_signal():
    assert rank.score(rank.Gap("src/a.py", (1,), 0, 0), frozenset()) == (0, ())


def test_fully_covered_files_are_not_ranked():
    gaps = (rank.Gap("src/a.py", (), 0, 10),)
    assert rank.rank(gaps, frozenset()) == ()
    assert rank.totals(gaps)["clean_files"] == 1


def test_totals_handles_a_report_with_no_statements():
    assert rank.totals((rank.Gap("src/a.py", (1,), 0, 0),))["covered_pct"] == 0.0


@pytest.mark.parametrize("lines,expected", [
    ((), ""), ((5,), "5"), ((1, 2), "1-2"), ((1, 3), "1,3"),
    ((1, 2, 3, 8, 9), "1-3,8-9"),
])
def test_line_ranges_collapse(lines, expected):
    assert rank.ranges(lines) == expected


def test_istanbul_adapter_maps_statements_to_lines():
    gaps = rank.from_istanbul({
        "a.js": {
            "path": "a.js",
            "statementMap": {"0": {"start": {"line": 7}}, "1": {"start": {"line": 8}}},
            "s": {"0": 0, "1": 3},
            "b": {"0": [0, 1], "1": [1, 1]},
        }
    })
    assert gaps[0].missed == (7,)
    assert gaps[0].branches == 1


def test_istanbul_entry_without_a_start_position_is_ignored():
    gaps = rank.from_istanbul({
        "a.js": {"path": "a.js", "statementMap": {"0": {}}, "s": {"0": 0}}
    })
    assert gaps[0].missed == ()


def test_unknown_report_shape_is_rejected():
    with pytest.raises(ValueError, match="unrecognised"):
        rank.detect({"totals": {}})


def test_diff_hunks_become_line_numbers():
    changed = rank.parse_diff(
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "@@ -9,0 +10,3 @@\n"
    )
    assert changed["src/a.py"] == frozenset({1, 10, 11, 12})


def test_deleted_files_are_not_in_the_diff():
    assert rank.parse_diff("+++ /dev/null\n@@ -1,2 +0,0 @@\n") == {}


def test_hunks_before_any_file_header_are_ignored():
    assert rank.parse_diff("@@ -1 +1 @@\n") == {}


def test_restrict_keeps_only_lines_the_diff_touched():
    gaps = (rank.Gap("src/a.py", (1, 5, 9), 0, 10),)
    kept = rank.restrict(gaps, {"src/a.py": frozenset({5, 100})})
    assert kept[0].missed == (5,)


def test_restrict_drops_files_the_diff_never_mentions():
    gaps = (rank.Gap("src/a.py", (1,), 0, 10),)
    assert rank.restrict(gaps, {"src/other.py": frozenset({1})}) == ()


@pytest.mark.parametrize("ref", ["--upload-pack=x", "-x", "a b", "a;b", "`x`", "a" * 300])
def test_dangerous_git_refs_are_refused(ref):
    with pytest.raises(RuntimeError, match="refusing"):
        rank.changed_lines(ref)


def test_recent_files_degrades_to_empty_outside_a_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert isinstance(rank.recent_files(), frozenset)


def test_render_reports_a_clean_repo_plainly():
    text = rank.render((), rank.totals(()), 5)
    assert "No gaps worth reporting" in text


def test_render_truncates_and_says_how_many_are_left():
    gaps = tuple(rank.Gap(f"src/f{i}.py", (1,), 0, 10) for i in range(8))
    rows = rank.rank(gaps, frozenset())
    text = rank.render(rows, rank.totals(gaps), 2)
    assert "and 6 more files" in text


def test_json_output_round_trips(tmp_path, capsys):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report({"src/a.py": {
        "summary": {"num_statements": 4}, "missing_lines": [1, 2]}})))
    assert rank.main(["--coverage", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["missed"] == 2
    assert payload["gaps"][0]["file"] == "src/a.py"
