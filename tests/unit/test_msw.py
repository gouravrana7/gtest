"""Unit: a faked backend.

msw exists to answer HTTP without a server. That is useful and it is also the
quietest way to a green suite that proves nothing: the handlers were written
once, the real API moved on, and no test noticed. Three things decide whether
the setup can be trusted at all.
"""

import pytest
import jssource
import stacks

STRICT = """
import { setupServer } from 'msw/node'
const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
"""


def found(source):
    return {record["kind"] for record in jssource.msw_findings(source)}


def test_a_strict_setup_has_nothing_wrong_with_it():
    assert jssource.msw_findings(STRICT) == ()


@pytest.mark.parametrize("source,used", [
    ("import { setupServer } from 'msw/node'", True),
    ("import { setupWorker } from 'msw/browser'", True),
    ("const server = setupServer()", True),
    ("import { render } from '@testing-library/react'", False),
    ("// const server = setupServer()", False),
    ("", False),
])
def test_an_msw_setup_is_recognised(source, used):
    assert jssource.uses_msw(source) is used


@pytest.mark.parametrize("mode", ["bypass", "warn"])
def test_anything_but_error_lets_an_unmatched_request_through(mode):
    source = STRICT.replace("'error'", f"'{mode}'")
    assert found(source) == {"msw_lenient"}
    assert mode in jssource.msw_findings(source)[0]["why"]


def test_leaving_it_unset_is_the_same_problem():
    source = STRICT.replace("{ onUnhandledRequest: 'error' }", "")
    assert found(source) == {"msw_lenient"}
    assert "unset" in jssource.msw_findings(source)[0]["why"]


def test_the_reason_explains_what_actually_goes_wrong():
    why = jssource.msw_findings(STRICT.replace("'error'", "'bypass'"))[0]["why"]
    assert "no longer exists" in why, "say what breaks, not just what is set"


def test_no_reset_makes_the_suite_order_dependent():
    source = STRICT.replace("afterEach(() => server.resetHandlers())", "")
    assert found(source) == {"msw_no_reset"}
    assert "order-dependent" in jssource.msw_findings(source)[0]["why"]


def test_a_reset_outside_after_each_is_still_called_out():
    source = STRICT.replace(
        "afterEach(() => server.resetHandlers())", "server.resetHandlers()"
    )
    assert found(source) == {"msw_no_reset"}
    assert "not from afterEach" in jssource.msw_findings(source)[0]["why"]


def test_both_problems_at_once():
    source = STRICT.replace("'error'", "'bypass'").replace(
        "afterEach(() => server.resetHandlers())", ""
    )
    assert found(source) == {"msw_lenient", "msw_no_reset"}


def test_a_worker_setup_is_checked_the_same_way():
    source = STRICT.replace("setupServer", "setupWorker").replace("msw/node", "msw/browser")
    assert jssource.msw_findings(source) == ()


def test_a_file_with_no_msw_is_not_commented_on():
    assert jssource.msw_findings("export const add = (a, b) => a + b") == ()


def write(root, files):
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_the_setup_is_found_outside_the_test_files(tmp_path):
    """It lives in src/mocks/server.ts, which is not a test file."""
    root = write(tmp_path, {
        "src/mocks/server.ts": STRICT.replace("'error'", "'bypass'"),
        "tests/a.spec.ts": "it('x', () => { expect(1).toBe(1) })",
    })
    records = stacks.mock_setups(root)
    assert [r["kind"] for r in records] == ["msw_lenient"]
    assert records[0]["path"] == "src/mocks/server.ts"


def test_a_setup_test_file_is_found_too(tmp_path):
    root = write(tmp_path, {"setupTests.ts": STRICT.replace("'error'", "'warn'")})
    assert [r["kind"] for r in stacks.mock_setups(root)] == ["msw_lenient"]


def test_vendored_directories_are_not_searched(tmp_path):
    root = write(tmp_path, {
        "node_modules/msw/setup.ts": STRICT.replace("'error'", "'bypass'"),
    })
    assert stacks.mock_setups(root) == ()


def test_a_repo_without_mocking_says_nothing_about_it(tmp_path):
    root = write(tmp_path, {"tests/a.spec.ts": "it('x', () => { expect(1).toBe(1) })"})
    assert stacks.mock_setups(root) == ()
    assert "faked backend" not in stacks.render(stacks.inventory(root))


def test_the_inventory_reports_the_problems(tmp_path):
    root = write(tmp_path, {
        "package.json": '{"devDependencies": {"msw": "^2.0.0"}}',
        "src/mocks/server.ts": STRICT.replace("'error'", "'bypass'"),
        "tests/a.spec.ts": "it('x', () => { expect(1).toBe(1) })",
    })
    report = stacks.inventory(root)
    assert report["mock_setup_count"] == 1
    assert "faked backend" in stacks.render(report)


def test_msw_makes_contract_tests_expected(tmp_path):
    """A handwritten fake drifts from the real API; only a contract test knows."""
    root = write(tmp_path, {
        "package.json": '{"devDependencies": {"msw": "^2.0.0"}}',
        "tests/a.spec.ts": "it('x', () => { expect(1).toBe(1) })",
    })
    report = stacks.inventory(root)
    assert "contract" in report["missing_kinds"]
    assert "msw" in report["kinds"]["contract"]["evidence"]


@pytest.mark.parametrize("dep", ["nock", "responses", "respx", "vcrpy", "wiremock"])
def test_other_http_fakes_count_the_same(dep, tmp_path):
    root = write(tmp_path, {
        "pyproject.toml": f"[project]\nname='x'\ndependencies=['{dep}']\n",
        "tests/test_a.py": "def test_x():\n    assert f() == 1\n",
    })
    assert "contract" in stacks.inventory(root)["missing_kinds"]
