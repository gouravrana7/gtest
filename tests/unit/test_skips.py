"""Unit: Python tests that never run.

A skipped test is counted like any other test and catches nothing. One left
skipped without a reason is the kind nobody ever comes back to.
"""

import pytest
import pysource


def whys(source):
    return {record["test"]: record["why"] for record in pysource.focus_and_skips(source)}


def test_a_skip_with_a_reason_says_the_reason():
    source = "@pytest.mark.skip(reason='api not ready')\ndef test_x():\n    assert f() == 1\n"
    assert "api not ready" in whys(source)["test_x"]


def test_a_bare_skip_is_called_out_as_having_no_reason():
    source = "@pytest.mark.skip\ndef test_x():\n    assert f() == 1\n"
    assert "no reason given" in whys(source)["test_x"]


def test_a_skip_called_without_a_reason_is_too():
    source = "@pytest.mark.skip()\ndef test_x():\n    assert f() == 1\n"
    assert "no reason given" in whys(source)["test_x"]


def test_a_conditional_skip_is_marked_conditional():
    source = (
        "@pytest.mark.skipif(sys.platform == 'win32', reason='posix')\n"
        "def test_x():\n    assert f() == 1\n"
    )
    why = whys(source)["test_x"]
    assert "conditional" in why and "posix" in why


def test_an_xfail_never_proves_anything_either():
    source = "@pytest.mark.xfail\ndef test_x():\n    assert f() == 1\n"
    assert "xfail" in whys(source)["test_x"]


def test_a_test_that_skips_itself_on_the_first_line():
    source = "def test_x():\n    pytest.skip('not ready')\n    assert f() == 1\n"
    assert "first line" in whys(source)["test_x"]


def test_a_skip_later_in_the_body_is_conditional_and_not_flagged():
    source = (
        "def test_x():\n"
        "    if not ready():\n"
        "        pytest.skip('later')\n"
        "    assert f() == 1\n"
    )
    assert whys(source) == {}, "a guarded skip still runs the test sometimes"


@pytest.mark.parametrize("decorator", [
    "@pytest.mark.parametrize('x', [1])",
    "@pytest.fixture",
    "@functools.cache",
])
def test_other_decorators_are_not_skips(decorator):
    source = f"{decorator}\ndef test_x(x=1):\n    assert f() == 1\n"
    assert whys(source) == {}


def test_a_plain_test_is_not_reported():
    assert whys("def test_x():\n    assert f() == 1\n") == {}


def test_the_line_number_points_at_the_function():
    source = "\n\n@pytest.mark.skip\ndef test_x():\n    assert f() == 1\n"
    assert pysource.focus_and_skips(source)[0]["line"] == 4


def test_unparseable_source_yields_nothing():
    assert pysource.focus_and_skips("def test_x(:\n") == ()


def test_the_inventory_counts_them(tmp_path):
    import stacks
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "@pytest.mark.skip\ndef test_dead():\n    assert f() == 1\n"
        "def test_live():\n    assert f() == 2\n"
    )
    report = stacks.inventory(tmp_path)
    assert report["never_run_count"] == 1
    assert report["focused_count"] == 0
    assert report["never_run"][0]["test"] == "test_dead"
    assert "never run" in stacks.render(report)


def test_the_inventory_counts_a_focused_javascript_test(tmp_path):
    import stacks
    (tmp_path / "a.spec.ts").write_text("it.only('x', () => { expect(1).toBe(1) })\n")
    report = stacks.inventory(tmp_path)
    assert report["focused_count"] == 1
    assert "silently not running" in stacks.render(report)


def test_the_cli_lists_skips(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("@pytest.mark.skip\ndef test_dead():\n    assert f() == 1\n")
    assert pysource.main([str(path), "--skips"]) == 0
    assert "test_dead" in capsys.readouterr().out


def test_the_cli_says_so_when_everything_runs(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_live():\n    assert f() == 1\n")
    assert pysource.main([str(path), "--skips"]) == 0
    assert "every test in this file runs" in capsys.readouterr().out
