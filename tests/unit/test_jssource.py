"""Unit: the JavaScript and TypeScript scanner.

There is no JS parser in the standard library and this plugin takes no
dependencies, so these pin down what the scanner must get right: comments and
strings must not be mistaken for code, braces inside strings must not break
the matching, and the same findings as Python must come out.
"""

import pytest
import jssource


def names(source):
    return [case.name for case in jssource.cases(source)]


def kinds(source, fn):
    return {record["test"]: record["kind"] for record in fn(source)}


@pytest.mark.parametrize("source,expected", [
    ("it('a', () => { expect(1).toBe(1) })", ["a"]),
    ('it("a", () => { expect(1).toBe(1) })', ["a"]),
    ("test('a', () => { expect(1).toBe(1) })", ["a"]),
    ("it.only('a', () => {})", ["a"]),
    ("it.skip('a', () => {})", ["a"]),
    ("it.each([1])('a', () => {})", ["a"]),
    ("xit('a', () => {})", ["a"]),
    ("fit('a', () => {})", ["a"]),
])
def test_every_way_of_declaring_a_test_is_found(source, expected):
    assert names(source) == expected


def test_a_describe_is_not_counted_as_a_case():
    source = "describe('group', () => { it('a', () => { expect(1).toBe(1) }) })"
    assert names(source) == ["a"]
    assert [t.kind for t in jssource.tests(source)] == ["describe", "it"]


def test_a_commented_out_test_is_not_a_test():
    assert names("// it('a', () => {})") == []
    assert names("/* it('a', () => {}) */") == []


def test_a_test_name_inside_a_comment_block_spanning_lines_is_ignored():
    source = "/*\nit('a', () => {})\n*/\nit('b', () => { expect(1).toBe(1) })"
    assert names(source) == ["b"]


def test_a_brace_inside_a_string_does_not_break_matching():
    source = "it('a', () => { expect(f('{')).toBe('}') })\nit('b', () => { expect(1).toBe(1) })"
    assert names(source) == ["a", "b"]


def test_a_template_literal_body_does_not_break_matching():
    source = "it('a', () => { expect(f(`{x}`)).toBe('x') })\nit('b', () => { expect(1).toBe(1) })"
    assert names(source) == ["a", "b"]


def test_a_brace_free_arrow_body_is_still_read():
    source = "it('a', () => expect(add(1, 1)).toBe(2))"
    assert names(source) == ["a"]
    assert jssource.unasserted(source) == (), "the assertion is inside the body"


@pytest.mark.parametrize("source", [
    "it('a', async ({ page }) => { await expect(page).toHaveTitle(/x/) })",
    "it('a', async ({ page }, info) => { await expect(page).toHaveTitle(/x/) })",
    "it('a', ({ a, b }) => { expect(a).toBe(b) })",
    "it('a', function () { expect(1).toBe(1) })",
    "it('a', async function () { expect(1).toBe(1) })",
])
def test_a_destructured_parameter_is_not_the_body(source):
    """Playwright writes `async ({ page }) => {...}`.

    Taking the first brace as the body made every Playwright test look like it
    asserted nothing. Found by running this on a real Playwright repo.
    """
    assert names(source) == ["a"]
    assert jssource.unasserted(source) == (), "the real body must be read"


def test_an_empty_test_checks_nothing():
    assert kinds("it('a', () => {})", jssource.unasserted) == {"a": "checks_nothing"}


def test_a_test_that_calls_without_asserting_relies_on_not_throwing():
    source = "it('a', () => { doTheThing() })"
    assert kinds(source, jssource.unasserted) == {"a": "relies_on_no_raise"}


@pytest.mark.parametrize("body", [
    "expect(x).toBe(1)",
    "assert.equal(x, 1)",
    "expect(x).toMatchSnapshot()",
    "await expect(p).rejects.toThrow(TypeError)",
])
def test_an_asserting_body_is_not_flagged(body):
    assert jssource.unasserted(f"it('a', () => {{ {body} }})") == ()


def test_expecting_a_literal_against_itself_cannot_fail():
    source = "it('a', () => { expect(true).toBe(true) })"
    assert kinds(source, jssource.suspect_tests) == {"a": "always_true"}


def test_a_bare_to_throw_catches_anything():
    source = "it('a', () => { expect(() => boom()).toThrow() })"
    assert kinds(source, jssource.suspect_tests) == {"a": "broad_raises"}


def test_a_named_error_in_to_throw_is_specific():
    source = "it('a', () => { expect(() => boom()).toThrow(TypeError) })"
    assert jssource.suspect_tests(source) == ()


def test_the_same_body_twice_is_flagged_once():
    source = (
        "it('a', () => { expect(add(3, 3)).toBe(6) })\n"
        "it('b', () => { expect(add(3, 3)).toBe(6) })"
    )
    found = jssource.suspect_tests(source)
    assert [r["test"] for r in found] == ["b"]
    assert "a" in found[0]["why"]


def test_different_bodies_are_different_tests():
    source = (
        "it('a', () => { expect(add(3, 3)).toBe(6) })\n"
        "it('b', () => { expect(add(4, 4)).toBe(8) })"
    )
    assert jssource.suspect_tests(source) == ()


def test_only_is_the_dangerous_one():
    source = "it.only('a', () => { expect(1).toBe(1) })"
    record = jssource.focus_and_skips(source)[0]
    assert record["kind"] == "focused"
    assert "silences the rest" in record["why"]


@pytest.mark.parametrize("source", [
    "it.skip('a', () => {})",
    "xit('a', () => {})",
    "it.todo('a')",
    "describe.skip('a', () => {})",
])
def test_every_way_of_skipping_is_found(source):
    assert jssource.focus_and_skips(source)[0]["kind"] == "skipped"


def test_a_plain_test_is_neither_focused_nor_skipped():
    assert jssource.focus_and_skips("it('a', () => { expect(1).toBe(1) })") == ()


def test_the_reported_line_is_the_real_one():
    source = "\n\n\nit.only('a', () => {})"
    assert jssource.focus_and_skips(source)[0]["line"] == 4


def test_an_empty_file_yields_nothing():
    for fn in (jssource.tests, jssource.cases, jssource.unasserted,
               jssource.suspect_tests, jssource.focus_and_skips):
        assert fn("") == ()


def test_source_that_is_not_tests_yields_nothing():
    assert jssource.tests("export const add = (a, b) => a + b") == ()


def test_the_cli_lists_cases_and_findings(tmp_path, capsys):
    path = tmp_path / "a.spec.ts"
    path.write_text("it.only('focused', () => { expect(true).toBe(true) })\n")
    assert jssource.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "focused" in out and "always_true" in out


def test_the_cli_rejects_a_missing_file(tmp_path, capsys):
    assert jssource.main([str(tmp_path / "gone.ts")]) == 2
    assert "no such file" in capsys.readouterr().err
