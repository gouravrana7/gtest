"""Unit: tests that assert nothing.

The single most useful thing to report next to a coverage percentage: a test
that executes the code and then checks none of it.
"""
import pytest
import pysource


def bare(source):
    return pysource.assertionless(source)


@pytest.mark.parametrize("body", [
    "assert x == 1",
    "self.assertEqual(x, 1)",
    "self.assertTrue(x)",
    "assert_that(x).is_equal_to(1)",
    "mock.assert_called_once()",
    "mock.assert_not_called()",
    "expect(x).to_be(1)",
    "raise AssertionError('no')",
])
def test_a_test_that_can_fail_is_not_flagged(body):
    assert bare(f"def test_x(self):\n    {body}\n") == ()


def test_pytest_raises_counts_as_an_assertion():
    source = "def test_x():\n    with pytest.raises(ValueError):\n        boom()\n"
    assert bare(source) == ()


def test_pytest_warns_counts_as_an_assertion():
    source = "def test_x():\n    with pytest.warns(UserWarning):\n        warn()\n"
    assert bare(source) == ()


@pytest.mark.parametrize("body", [
    "pass",
    "compute(1, 2)",
    "print(compute(1))",
    "x = compute(1)",
    "for i in range(3):\n        compute(i)",
])
def test_a_test_that_cannot_fail_is_flagged(body):
    assert bare(f"def test_x():\n    {body}\n") == ("test_x",)


def test_methods_are_named_with_their_class():
    source = "class TestThing:\n    def test_x(self):\n        self.run()\n"
    assert bare(source) == ("TestThing.test_x",)


def test_non_test_functions_are_never_flagged():
    source = "def helper():\n    compute(1)\n\ndef fixture_thing():\n    return 1\n"
    assert bare(source) == ()


def test_an_assertion_in_a_nested_helper_does_not_rescue_the_test():
    source = "def test_x():\n    def check():\n        assert False\n"
    assert bare(source) == ("test_x",)


def test_an_async_test_is_checked_too():
    assert bare("async def test_x():\n    await run()\n") == ("test_x",)
    assert bare("async def test_y():\n    assert await run()\n") == ()


def test_unparseable_source_flags_nothing():
    assert bare("def test_x(:\n") == ()


def test_the_cli_reports_bare_tests(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_nothing():\n    compute()\n")
    assert pysource.main([str(path), "--assertionless"]) == 0
    assert "no explicit assertion: test_nothing" in capsys.readouterr().out


def test_the_cli_says_so_when_the_file_is_fine(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_ok():\n    assert True\n")
    assert pysource.main([str(path), "--assertionless"]) == 0
    assert "every test in this file asserts something" in capsys.readouterr().out


def test_the_inventory_reports_them_per_file(tmp_path):
    import stacks
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_bare():\n    run()\ndef test_good():\n    assert 1\n")
    report = stacks.inventory(tmp_path)
    assert report["assertionless_count"] == 1
    assert report["assertionless"][0]["tests"] == ["test_bare"]
    assert "no explicit assertion" in stacks.render(report)


def test_a_bare_call_is_weaker_not_worthless():
    """validate_x(...) alone fails when it raises, which is a real check.

    Found by running the plugin on a repo where every such test was paired
    with a pytest.raises test for the negative case.
    """
    records = pysource.unasserted("def test_x():\n    validate_codec('h264', 'a.mp4')\n")
    assert records[0]["kind"] == "relies_on_no_raise"


@pytest.mark.parametrize("body,kind", [
    ("run()", "relies_on_no_raise"),
    ("run()\n    other()", "relies_on_no_raise"),
    ("print(run())", "relies_on_no_raise"),
    ("pass", "checks_nothing"),
    ("x = 1", "checks_nothing"),
    ("x = run()", "checks_nothing"),
    ("return None", "checks_nothing"),
])
def test_the_two_categories_are_told_apart(body, kind):
    records = pysource.unasserted(f"def test_x():\n    {body}\n")
    assert records[0]["kind"] == kind


def test_a_docstring_does_not_count_as_a_statement():
    source = 'def test_x():\n    """why"""\n    run()\n'
    assert pysource.unasserted(source)[0]["kind"] == "relies_on_no_raise"


def test_the_counts_separate_the_two(tmp_path):
    import stacks
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_bare():\n    run()\ndef test_empty():\n    pass\n")
    report = stacks.inventory(tmp_path)
    assert report["assertionless_count"] == 2
    assert report["checks_nothing_count"] == 1
    item = report["assertionless"][0]
    assert item["relies_on_no_raise"] == ["test_bare"]
    assert item["checks_nothing"] == ["test_empty"]


def test_the_cli_distinguishes_them(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_bare():\n    run()\ndef test_empty():\n    pass\n")
    assert pysource.main([str(path), "--assertionless"]) == 0
    out = capsys.readouterr().out
    assert "relies on the call not raising" in out
    assert "checks nothing at all" in out


def test_javascript_test_files_are_analysed_too(tmp_path):
    """Python is read with ast and JS with a scanner, but both are read."""
    import stacks
    (tmp_path / "a.test.ts").write_text("it('x', () => { run() })\n")
    report = stacks.inventory(tmp_path)
    assert report["assertionless_count"] == 1
    assert report["assertionless"][0]["relies_on_no_raise"] == ["x"]


def test_a_language_with_no_reader_is_left_alone(tmp_path):
    import stacks
    (tmp_path / "a_test.go").write_text("func TestX(t *testing.T) {}\n")
    report = stacks.inventory(tmp_path)
    assert report["total_tests"] == 1, "it is still counted"
    assert report["assertionless_count"] == 0, "but Go is not parsed for assertions"
