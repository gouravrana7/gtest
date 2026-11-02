"""Unit: test durations and the quarantine list.

The duration is already in the JUnit report, so the slowest tests cost
nothing to find and are usually the cheapest CI win available.
"""
import pytest
import flaky


def xml(*cases):
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def test_durations_are_read_per_test():
    times = flaky.parse_times(xml(
        '<testcase classname="t" name="a" time="0.25"/>',
        '<testcase classname="t" name="b" time="3"/>',
    ))
    assert times == {"t::a": 0.25, "t::b": 3.0}


def test_a_missing_duration_is_zero():
    assert flaky.parse_times(xml('<testcase classname="t" name="a"/>')) == {"t::a": 0.0}


def test_an_unparseable_duration_is_zero_not_a_crash():
    times = flaky.parse_times(xml('<testcase classname="t" name="a" time="fast"/>'))
    assert times == {"t::a": 0.0}


def test_parse_times_refuses_an_entity_declaration():
    bomb = '<!DOCTYPE x [<!ENTITY e "b">]>' + xml('<testcase name="a"/>')
    with pytest.raises(ValueError, match="DTD or entity"):
        flaky.parse_times(bomb)


def test_parse_times_refuses_an_oversized_report():
    with pytest.raises(ValueError, match="refusing to parse"):
        flaky.parse_times("x" * (flaky.MAX_XML_BYTES + 1))


def test_the_worst_run_of_each_test_is_what_counts():
    ranked = flaky.slowest([{"a": 0.1, "b": 2.0}, {"a": 9.0, "b": 2.0}])
    assert ranked[0] == {"test": "a", "seconds": 9.0}
    assert ranked[1] == {"test": "b", "seconds": 2.0}


def test_instant_tests_are_not_listed():
    assert flaky.slowest([{"a": 0.0}]) == ()


def test_the_list_is_capped():
    runs = [{f"t{i}": float(i + 1) for i in range(20)}]
    assert len(flaky.slowest(runs, count=3)) == 3


def test_no_runs_means_no_list():
    assert flaky.slowest([]) == ()


def test_ties_are_broken_by_name_so_the_order_is_stable():
    ranked = flaky.slowest([{"b": 1.0, "a": 1.0}])
    assert [item["test"] for item in ranked] == ["a", "b"]


def test_a_dotted_module_becomes_a_pytest_path():
    args = flaky.quarantine({"flaky": [{"test": "tests.unit.test_auth::test_login"}]})
    assert args == ("--deselect tests/unit/test_auth.py::test_login",)


def test_a_path_style_id_is_left_alone():
    args = flaky.quarantine({"flaky": [{"test": "tests/e2e/a.spec.ts::logs in"}]})
    assert args == ("--deselect tests/e2e/a.spec.ts::logs in",)


def test_an_id_without_a_separator_cannot_be_deselected():
    assert flaky.quarantine({"flaky": [{"test": "mystery"}]}) == ()


def test_broken_tests_are_never_quarantined():
    report = {"flaky": [], "broken": [{"test": "t::always_fails"}]}
    assert flaky.quarantine(report) == (), "a test that always fails is a bug, not a flake"


def test_an_empty_report_yields_no_arguments():
    assert flaky.quarantine({}) == ()


def test_the_cli_reports_timing_and_quarantine(tmp_path, capsys):
    good = tmp_path / "a.xml"
    bad = tmp_path / "b.xml"
    good.write_text(xml('<testcase classname="t.test_x" name="a" time="0.1"/>'))
    bad.write_text(xml(
        '<testcase classname="t.test_x" name="a" time="5.0"><failure/></testcase>'))
    assert flaky.main([str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "SLOWEST" in out and "5.000s" in out
    assert "To unblock CI" in out
    assert "--deselect t/test_x.py::a" in out


def test_the_json_output_carries_both(tmp_path, capsys):
    import json
    for name in ("a.xml", "b.xml"):
        (tmp_path / name).write_text(xml('<testcase classname="t" name="a" time="1.0"/>'))
    assert flaky.main([str(tmp_path / "a.xml"), str(tmp_path / "b.xml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["slowest"] == [{"test": "t::a", "seconds": 1.0}]
    assert payload["quarantine"] == []
