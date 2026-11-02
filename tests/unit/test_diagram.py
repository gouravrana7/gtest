"""Unit: the terminal diagram.

It is the output the user actually reads, so it has to stay inside a terminal
width, draw bars that cannot overflow, and never echo a control character
from a coverage report straight at the shell.
"""
import pytest
import diagram


def test_the_header_summarises_everything_present():
    page = diagram.draw(diagram.STACKS, diagram.COVERAGE, diagram.FLAKY)
    head = page.splitlines()[0]
    assert head.startswith("/gtest")
    assert "71.5% covered" in head and "94 tests" in head
    assert "2 prove nothing" in page
    assert "1 cannot fail" in page


def test_the_header_wraps_instead_of_overflowing():
    lines = diagram.header(diagram.STACKS, diagram.COVERAGE, diagram.FLAKY, "x" * 30)
    assert len(lines) >= 2, "every chip is a finding; none may be dropped"
    assert all(len(line) <= diagram.WIDTH for line in lines), max(lines, key=len)


def test_the_header_is_just_the_name_when_there_is_nothing_to_report():
    assert diagram.header({}, {}, {}, "x") == ["/x"]


def test_the_header_softens_when_no_test_is_worthless():
    stacks = {**diagram.STACKS, "checks_nothing_count": 0}
    # the header wraps once there are several findings, so read all of it
    head = "  ".join(diagram.header(stacks, {}, {}, "gtest"))
    assert "without an assert" in head
    assert "cannot fail" not in head


def test_the_title_is_configurable():
    assert diagram.draw({}, {}, {}, title="sweep").startswith("/sweep")


def test_it_is_terminal_output_not_markup():
    page = diagram.draw(diagram.STACKS, diagram.COVERAGE, diagram.FLAKY)
    assert "</" not in page and "<html" not in page.lower()


def test_no_line_is_wider_than_a_terminal():
    page = diagram.draw(diagram.STACKS, diagram.COVERAGE, diagram.FLAKY)
    assert all(len(line) <= 100 for line in page.splitlines())


def test_suspect_tests_get_their_own_block():
    page = diagram.draw(diagram.STACKS, {}, {})
    assert "NOT REALLY TESTING" in page
    assert "cannot fail" in page and "test_always_passes" in page
    assert "catches too much" in page and "no match=" in page


@pytest.mark.parametrize("fraction", [0.0, 0.01, 0.25, 0.5, 0.999, 1.0, 2.0, -1.0])
def test_a_bar_is_always_exactly_its_width(fraction):
    assert len(diagram.bar(fraction)) == diagram.BAR
    assert len(diagram.bar(fraction, 9)) == 9


def test_an_empty_bar_and_a_full_bar_are_distinguishable():
    assert diagram.bar(0.0) == diagram.TRACK * diagram.BAR
    assert diagram.bar(1.0) == diagram.FULL * diagram.BAR


def test_a_tiny_share_still_shows_something():
    assert diagram.bar(0.01) != diagram.TRACK * diagram.BAR


def test_a_gap_and_a_non_applicable_kind_read_differently():
    page = diagram.draw(diagram.STACKS, {}, {})
    assert page.count("<- gap") == 1
    assert page.count("n/a") == 2
    assert "No e2e tests, and this repo wants them" in page
    assert "evidence: fastapi, Dockerfile" in page
    assert "Not expected here" in page
    assert "No contract tests" not in page, "no evidence means no complaint"


def test_tests_without_assertions_get_their_own_block():
    page = diagram.draw(diagram.STACKS, {}, {})
    assert "NO EXPLICIT ASSERTION" in page
    assert "cannot fail: test_add" in page
    assert "passes unless it raises: test_sub" in page


def test_the_gap_tree_closes_on_the_last_function():
    page = diagram.draw({}, diagram.COVERAGE, {})
    assert "├─ expire" in page
    assert "└─ refresh" in page


def test_flaky_broken_and_partial_read_differently():
    page = diagram.draw({}, {}, {
        "runs": 2,
        "flaky": [{"test": "a", "outcomes": ["pass", "fail"], "passed": 1, "failed": 1}],
        "broken": [{"test": "b", "outcomes": ["fail", "fail"]}],
        "partial": [{"test": "c", "outcomes": ["pass", "absent"]}],
    })
    assert "pass fail" in page
    assert "always failing" in page
    assert "collection changed" in page


def test_a_steady_suite_says_so():
    page = diagram.draw({}, {}, {"runs": 4, "flaky": [], "broken": [], "partial": []})
    assert "agreed with itself across 4 runs" in page


def test_every_block_is_optional():
    assert "TEST KINDS" not in diagram.draw({}, diagram.COVERAGE, {})
    assert "FLAKINESS" not in diagram.draw(diagram.STACKS, {}, {})
    assert "No gaps worth reporting" in diagram.draw({}, {}, {})


def test_control_characters_never_reach_the_terminal():
    page = diagram.draw({}, {"gaps": [{
        "file": "a\x1b[31m\x07.py", "score": 1, "ranges": "1",
        "reasons": ["\x1b[2J"], "missed_lines": [1], "statements": 1, "functions": [],
    }]}, {})
    assert "\x1b" not in page and "\x07" not in page


def test_an_overlong_path_is_truncated():
    page = diagram.draw({}, {"gaps": [{
        "file": "x" * 500, "score": 1, "ranges": "1",
        "reasons": [], "missed_lines": [1], "statements": 1, "functions": [],
    }]}, {})
    assert "…" in page


def test_a_file_with_no_statements_does_not_divide_by_zero():
    page = diagram.draw({}, {"gaps": [{
        "file": "a.py", "score": 0, "ranges": "1", "reasons": [],
        "missed_lines": [1], "statements": 0, "functions": [],
    }]}, {})
    assert "0.0%" in page


def test_load_refuses_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="cannot read"):
        diagram.load(str(bad))
    with pytest.raises(ValueError, match="no such file"):
        diagram.load(str(tmp_path / "gone.json"))
    listy = tmp_path / "list.json"
    listy.write_text("[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        diagram.load(str(listy))
    assert diagram.load(None) == {}


def test_the_cli_needs_at_least_one_input():
    with pytest.raises(SystemExit) as exit_info:
        diagram.main([])
    assert exit_info.value.code == 2


def test_the_cli_prints_the_diagram(tmp_path, capsys):
    import json
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(diagram.COVERAGE))
    assert diagram.main(["--coverage", str(cov)]) == 0
    assert "WORST GAPS" in capsys.readouterr().out


def test_the_cli_reports_a_bad_file_without_a_traceback(tmp_path, capsys):
    assert diagram.main(["--coverage", str(tmp_path / "gone.json")]) == 2
    assert "no such file" in capsys.readouterr().err
