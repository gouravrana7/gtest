"""Unit: assertions that prove too little.

`assert result` passes for 1, for "oops" and for a half-built object.
`assert result == 42` does not. The difference is the whole test. A test with
one strong assertion is left alone — the weak one is then a guard.
"""

import pytest
import jssource
import pysource


def why(source):
    return {r["test"]: r["why"] for r in pysource.weak_assertions(source)}


def one(body):
    return why(f"def test_x():\n    {body}\n")


@pytest.mark.parametrize("body,expected", [
    ("assert compute(1)", "truthiness of a call"),
    ("result = compute(1)\n    assert result", "truthiness of a value"),
    ("assert compute(1) is not None", "not None"),
    ("assert compute(1) != None", "not None"),
    ("assert len(items()) > 0", "not empty"),
    ("assert len(items()) != 0", "not empty"),
    ("assert len(items()) >= 1", "not empty"),
])
def test_an_assertion_that_only_proves_existence(body, expected):
    assert expected in one(body)["test_x"]


@pytest.mark.parametrize("body", [
    "assert compute(1) == 42",
    "assert compute(1) is True",
    "assert len(items()) == 3",
    "assert compute(1) in {1, 2}",
    "assert compute(1) > 5",
])
def test_an_assertion_that_pins_a_value_is_strong(body):
    assert one(body) == {}


def test_a_mock_called_flag_does_not_say_with_what():
    assert "not with what" in one("assert m.called")["test_x"]


def test_asserting_the_arguments_is_strong():
    assert one("m.assert_called_once_with(1, 2)") == {}


@pytest.mark.parametrize("call,expected", [
    ("self.assertIsNotNone(compute(1))", "not None"),
    ("self.assertIsNone(compute(1))", "is None"),
    ("self.assertTrue(compute(1))", "not a value"),
    ("self.assertFalse(compute(1))", "not a value"),
])
def test_unittest_weak_assertions(call, expected):
    assert expected in one(call)["test_x"]


def test_assert_equal_is_strong():
    assert one("self.assertEqual(compute(1), 42)") == {}


def test_a_tautology_is_reported_as_such_not_as_weak():
    """assertTrue(1) is always_true, which says more. Only one finding."""
    assert pysource.weak_assertions("def test_x(self):\n    self.assertTrue(1)\n") == ()


def test_one_strong_assertion_makes_the_weak_one_a_guard():
    source = (
        "def test_x():\n"
        "    result = compute(1)\n"
        "    assert result\n"
        "    assert result.total == 42\n"
    )
    assert why(source) == {}


def test_it_comes_through_suspect_tests():
    found = {r["kind"] for r in pysource.suspect_tests("def test_x():\n    assert f()\n")}
    assert "weak_assertion" in found


def test_the_line_points_at_the_test():
    source = "\n\ndef test_x():\n    assert f()\n"
    assert pysource.weak_assertions(source)[0]["line"] == 3


def test_empty_source_yields_nothing():
    assert pysource.weak_assertions("") == ()
    assert pysource.weak_assertions("def broken(:") == ()


# --- JavaScript ---

def js(body):
    records = jssource.weak_assertions(f"it('a', () => {{ {body} }})")
    return records[0]["why"] if records else None


@pytest.mark.parametrize("body,expected", [
    ("expect(load()).toBeTruthy()", "truthiness"),
    ("expect(load()).toBeFalsy()", "falsiness"),
    ("expect(load()).toBeDefined()", "defined"),
    ("expect(load()).not.toBeNull()", "not null"),
    ("expect(load()).not.toBeUndefined()", "defined"),
    ("expect(spy).toHaveBeenCalled()", "not with what"),
])
def test_a_weak_matcher_is_flagged(body, expected):
    assert expected in js(body)


@pytest.mark.parametrize("body", [
    "expect(load()).toBe(42)",
    "expect(load()).toEqual({a: 1})",
    "expect(load()).toContain('x')",
    "expect(spy).toHaveBeenCalledWith(1)",
    "await expect(page).toHaveTitle(/x/)",
    "expect(() => boom()).toThrow(TypeError)",
    "expect(load()).toMatchSnapshot()",
])
def test_a_strong_matcher_is_not_flagged(body):
    assert js(body) is None


def test_one_strong_matcher_covers_a_weak_one():
    source = (
        "it('a', () => {\n"
        "  const r = load()\n"
        "  expect(r).toBeTruthy()\n"
        "  expect(r.total).toBe(42)\n"
        "})"
    )
    assert jssource.weak_assertions(source) == ()


def test_a_matcher_in_a_comment_does_not_count():
    source = "it('a', () => {\n  // expect(x).toBeTruthy()\n  expect(x).toBe(1)\n})"
    assert jssource.weak_assertions(source) == ()


def test_js_weak_assertions_come_through_suspect_tests():
    source = "it('a', () => { expect(load()).toBeTruthy() })"
    assert "weak_assertion" in {r["kind"] for r in jssource.suspect_tests(source)}
