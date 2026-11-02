"""Unit: tests that are green for the wrong reason.

A test can pass while proving nothing at all. These four checks are the ones
that can be decided from the source alone, without running anything.
"""

import pytest
import pysource


def kinds(source):
    return {record["test"]: record["kind"] for record in pysource.suspect_tests(source)}


def one(body):
    return pysource.suspect_tests(f"def test_x():\n    run()\n    {body}\n")


@pytest.mark.parametrize("body", [
    "assert True",
    "assert 1",
    "assert 'message'",
    "assert [1, 2]",
    "assert (1,)",
    "assert {'a': 1}",
])
def test_an_assertion_on_a_truthy_literal_cannot_fail(body):
    assert [r["kind"] for r in one(body)] == ["always_true"]


@pytest.mark.parametrize("body", [
    "assert False",
    "assert []",
    "assert 0",
    "assert None",
])
def test_a_falsy_literal_is_a_real_failing_check(body):
    assert one(body) == (), "asserting something false is deliberate, not a tautology"


def test_comparing_an_expression_with_itself_cannot_fail():
    assert [r["kind"] for r in one("assert x == x")] == ["always_true"]
    assert [r["kind"] for r in one("assert compute(1) is compute(1)")] == ["always_true"]


def test_comparing_two_different_expressions_is_fine():
    assert one("assert compute(1) == 2") == ()


@pytest.mark.parametrize("call", [
    "self.assertEqual(compute(1), compute(1))",
    "self.assertIs(a, a)",
    "self.assertTrue(1)",
    "self.assertFalse(0)",
])
def test_unittest_tautologies_are_caught(call):
    assert [r["kind"] for r in one(call)] == ["always_true"]


def test_a_real_unittest_assertion_is_fine():
    assert one("self.assertEqual(compute(1), 2)") == ()


def test_raises_exception_without_match_catches_a_typo_as_a_pass():
    source = "def test_x():\n    with pytest.raises(Exception):\n        boom()\n"
    assert kinds(source) == {"test_x": "broad_raises"}


def test_an_assertion_on_the_captured_exception_is_the_narrowing():
    """Found by running this on a real repo, which had exactly this shape."""
    source = (
        "def test_x():\n"
        "    with pytest.raises(Exception) as info:\n"
        "        boom()\n"
        "    assert 'ffmpeg' in str(info.value).lower()\n"
    )
    assert kinds(source) == {}


def test_capturing_without_checking_is_still_too_broad():
    source = (
        "def test_x():\n"
        "    with pytest.raises(Exception) as info:\n"
        "        boom()\n"
    )
    assert kinds(source) == {"test_x": "broad_raises"}


def test_a_match_argument_makes_it_specific_enough():
    source = (
        "def test_x():\n"
        "    with pytest.raises(Exception, match='boom'):\n"
        "        boom()\n"
    )
    assert kinds(source) == {}


def test_a_named_exception_is_not_flagged():
    source = "def test_x():\n    with pytest.raises(ValueError):\n        boom()\n"
    assert kinds(source) == {}


def test_base_exception_is_just_as_broad():
    source = "def test_x():\n    with pytest.raises(BaseException):\n        boom()\n"
    assert kinds(source) == {"test_x": "broad_raises"}


def test_unittest_assert_raises_is_treated_the_same():
    source = "def test_x(self):\n    with self.assertRaises(Exception):\n        boom()\n"
    assert kinds(source) == {"test_x": "broad_raises"}


@pytest.mark.parametrize("body", [
    "assert 1 + 1 == 2",
    "assert 'a' + 'b' == 'ab'",
    "assert len([1, 2]) == 2",
])
def test_a_test_that_touches_nothing_only_checks_python(body):
    kind = kinds(f"def test_x():\n    {body}\n").get("test_x")
    assert kind == "no_subject" or kind is None


def test_calling_the_subject_clears_no_subject():
    assert kinds("def test_x():\n    assert compute(1) == 2\n") == {}


def test_asserting_on_a_module_constant_is_a_real_test():
    """Also found on a real repo: no call, but a genuine invariant."""
    source = "def test_x():\n    assert '.gif' not in interactive.ENCODE_EXTS\n"
    assert kinds(source) == {}


def test_a_subscript_counts_as_a_subject():
    assert kinds("def test_x():\n    assert CONFIG['fps'] == 30\n") == {}


def test_the_same_body_twice_is_flagged_once():
    source = (
        "def test_a():\n    assert compute(3) == 9\n\n"
        "def test_b():\n    assert compute(3) == 9\n"
    )
    assert kinds(source) == {"test_b": "duplicate"}


def test_the_duplicate_names_the_test_it_copies():
    source = (
        "def test_original():\n    assert compute(3) == 9\n\n"
        "def test_copy():\n    assert compute(3) == 9\n"
    )
    why = pysource.suspect_tests(source)[0]["why"]
    assert "test_original" in why


def test_a_differing_literal_makes_them_different_tests():
    source = (
        "def test_a():\n    assert compute(3) == 9\n\n"
        "def test_b():\n    assert compute(4) == 16\n"
    )
    assert kinds(source) == {}


def test_a_docstring_does_not_make_two_tests_differ():
    source = (
        'def test_a():\n    """first"""\n    assert compute(3) == 9\n\n'
        'def test_b():\n    """second"""\n    assert compute(3) == 9\n'
    )
    assert kinds(source) == {"test_b": "duplicate"}


def test_non_test_functions_are_never_suspect():
    source = "def helper():\n    assert True\n"
    assert pysource.suspect_tests(source) == ()


def test_one_test_can_be_suspect_for_two_reasons():
    source = (
        "def test_x():\n"
        "    with pytest.raises(Exception):\n"
        "        boom()\n"
        "    assert True\n"
    )
    found = {r["kind"] for r in pysource.suspect_tests(source)}
    assert found == {"broad_raises", "always_true"}


def test_unparseable_source_yields_nothing():
    assert pysource.suspect_tests("def test_x(:\n") == ()
    assert pysource.suspect_tests("") == ()


def test_the_cli_lists_suspects(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_fake():\n    run()\n    assert True\n")
    assert pysource.main([str(path), "--suspect"]) == 0
    out = capsys.readouterr().out
    assert "always_true" in out and "test_fake" in out


def test_the_cli_says_so_when_the_file_is_clean(tmp_path, capsys):
    path = tmp_path / "test_a.py"
    path.write_text("def test_real():\n    assert compute(1) == 2\n")
    assert pysource.main([str(path), "--suspect"]) == 0
    assert "no suspect tests" in capsys.readouterr().out


def test_a_captured_result_that_is_never_checked():
    """The test asked for the answer and dropped it; the assertion goes there."""
    records = pysource.unused_results("def test_x():\n    result = compute(1)\n")
    assert records[0]["kind"] == "unused_result"
    assert "result" in records[0]["why"]


def test_a_checked_result_is_fine():
    source = "def test_x():\n    result = compute(1)\n    assert result == 1\n"
    assert pysource.unused_results(source) == ()


def test_an_underscore_means_deliberately_ignored():
    assert pysource.unused_results("def test_x():\n    _ = compute(1)\n") == ()


def test_unpacking_and_using_one_value_is_enough():
    source = "def test_x():\n    a, b = pair()\n    assert a == 1\n"
    assert pysource.unused_results(source) == ()


def test_unpacking_and_using_neither_is_flagged():
    source = "def test_x():\n    a, b = pair()\n"
    assert pysource.unused_results(source)[0]["kind"] == "unused_result"


def test_a_call_with_no_assignment_is_a_different_finding():
    assert pysource.unused_results("def test_x():\n    compute(1)\n") == ()


def test_the_report_sees_unused_results_through_suspect_tests():
    found = {r["kind"] for r in pysource.suspect_tests("def test_x():\n    r = f(1)\n")}
    assert "unused_result" in found


def test_a_fixture_no_test_asks_for_is_dead():
    source = (
        "@pytest.fixture\ndef orphan():\n    return 1\n\n"
        "def test_x():\n    assert f() == 1\n"
    )
    records = pysource.dead_fixtures(source)
    assert [r["test"] for r in records] == ["orphan"]
    assert records[0]["kind"] == "dead_fixture"


def test_a_requested_fixture_is_not_dead():
    source = (
        "@pytest.fixture\ndef thing():\n    return 1\n\n"
        "def test_x(thing):\n    assert thing == 1\n"
    )
    assert pysource.dead_fixtures(source) == ()


def test_an_autouse_fixture_is_requested_by_definition():
    source = "@pytest.fixture(autouse=True)\ndef setup():\n    return 1\n"
    assert pysource.dead_fixtures(source) == ()


def test_a_fixture_asked_for_by_another_fixture_is_alive():
    source = (
        "@pytest.fixture\ndef inner():\n    return 1\n\n"
        "@pytest.fixture\ndef outer(inner):\n    return inner\n\n"
        "def test_x(outer):\n    assert outer == 1\n"
    )
    assert pysource.dead_fixtures(source) == ()


def test_usefixtures_names_it_as_text():
    source = (
        "@pytest.fixture\ndef by_name():\n    return 1\n\n"
        '@pytest.mark.usefixtures("by_name")\n'
        "def test_x():\n    assert f() == 1\n"
    )
    assert pysource.dead_fixtures(source) == ()


def test_a_conftest_is_never_checked():
    source = "@pytest.fixture\ndef shared():\n    return 1\n"
    assert pysource.dead_fixtures(source, is_conftest=True) == ()
    assert pysource.dead_fixtures(source) != (), "but the same file elsewhere is"


def test_the_inventory_reports_a_dead_fixture(tmp_path):
    import stacks
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "@pytest.fixture\ndef orphan():\n    return 1\n\n"
        "def test_x():\n    assert f() == 1\n"
    )
    report = stacks.inventory(tmp_path)
    kinds_found = {item["kind"] for item in report["suspect"]}
    assert "dead_fixture" in kinds_found
