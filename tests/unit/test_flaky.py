"""Unit: JUnit parsing and the flaky/broken split."""
import pytest
import flaky


def xml(*cases):
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


OK = '<testcase classname="t" name="a"/>'
BAD = '<testcase classname="t" name="a"><failure/></testcase>'
ERR = '<testcase classname="t" name="a"><error/></testcase>'
SKIPPED = '<testcase classname="t" name="a"><skipped/></testcase>'


@pytest.mark.parametrize("case,expected", [
    (OK, flaky.PASS), (BAD, flaky.FAIL), (ERR, flaky.FAIL), (SKIPPED, flaky.SKIP),
])
def test_each_outcome_is_recognised(case, expected):
    assert flaky.parse_xml(xml(case)) == {"t::a": expected}


def test_a_case_without_a_classname_still_gets_an_id():
    assert flaky.parse_xml(xml('<testcase name="bare"/>')) == {"bare": flaky.PASS}


def test_a_case_without_a_name_is_skipped_over():
    with pytest.raises(ValueError, match="no <testcase>"):
        flaky.parse_xml(xml('<testcase classname="t"/>'))


def test_entity_declarations_are_refused():
    bomb = '<!DOCTYPE x [<!ENTITY e "boom">]>' + xml(OK)
    with pytest.raises(ValueError, match="DTD or entity"):
        flaky.parse_xml(bomb)


def test_malformed_xml_is_refused():
    with pytest.raises(ValueError, match="not valid JUnit XML"):
        flaky.parse_xml("<open")


def test_an_oversized_report_is_refused():
    with pytest.raises(ValueError, match="refusing to parse"):
        flaky.parse_xml("x" * (flaky.MAX_XML_BYTES + 1))


def test_a_test_that_disagrees_with_itself_is_flaky():
    report = flaky.classify([{"a": flaky.PASS}, {"a": flaky.FAIL}])
    assert [f["test"] for f in report["flaky"]] == ["a"]
    assert report["flaky"][0] == {"test": "a", "outcomes": ["pass", "fail"],
                                  "passed": 1, "failed": 1}


def test_a_test_failing_every_run_is_broken_not_flaky():
    report = flaky.classify([{"a": flaky.FAIL}, {"a": flaky.FAIL}])
    assert report["flaky"] == []
    assert [b["test"] for b in report["broken"]] == ["a"]


def test_a_skip_beside_a_pass_is_stable():
    report = flaky.classify([{"a": flaky.PASS}, {"a": flaky.SKIP}])
    assert report["flaky"] == [] and report["broken"] == []
    assert report["stable"] == 1


def test_a_test_missing_from_a_run_is_a_collection_change():
    report = flaky.classify([{"a": flaky.PASS}, {}])
    assert report["flaky"] == []
    assert [p["test"] for p in report["partial"]] == ["a"]
    assert report["partial"][0]["outcomes"] == ["pass", "absent"]


def test_comparing_needs_two_runs():
    with pytest.raises(ValueError, match="at least two runs"):
        flaky.classify([{"a": flaky.PASS}])


def test_read_run_rejects_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="no such run file"):
        flaky.read_run(tmp_path / "nope.xml")


def test_read_run_parses_a_file(tmp_path):
    path = tmp_path / "run.xml"
    path.write_text(xml(OK))
    assert flaky.read_run(path) == {"t::a": flaky.PASS}


def test_render_names_both_problem_classes():
    report = flaky.classify([
        {"a": flaky.PASS, "b": flaky.FAIL},
        {"a": flaky.FAIL, "b": flaky.FAIL},
    ])
    text = flaky.render(report)
    assert "FLAKY" in text and "ALWAYS FAILING" in text


def test_render_says_so_when_nothing_is_wrong():
    text = flaky.render(flaky.classify([{"a": flaky.PASS}, {"a": flaky.PASS}]))
    assert "agreed with itself" in text


def test_exit_code_signals_flakiness(tmp_path, capsys):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text(xml(OK))
    b.write_text(xml(BAD))
    assert flaky.main([str(a), str(b)]) == 1
    assert "FLAKY" in capsys.readouterr().out


def test_exit_code_is_zero_for_a_steady_suite(tmp_path):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text(xml(OK))
    b.write_text(xml(OK))
    assert flaky.main([str(a), str(b)]) == 0
