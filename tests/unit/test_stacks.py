"""Unit: test-kind classification, counting and runner detection."""
import pytest
import stacks


@pytest.mark.parametrize("path,kind", [
    ("tests/test_a.py", "unit"),
    ("tests/integration/test_api.py", "integration"),
    ("tests/functional/test_x.py", "integration"),
    ("tests/e2e/login.spec.ts", "e2e"),
    ("cypress/e2e/a.cy.ts", "e2e"),
    ("tests/playwright/checkout.spec.ts", "e2e"),
    ("tests/smoke/test_boot.py", "smoke"),
    ("tests/sanity/test_x.py", "smoke"),
    ("bench/test_speed.py", "performance"),
    ("tests/contract/test_pact.py", "contract"),
])
def test_kind_comes_from_the_path(path, kind):
    assert stacks.classify(path) == kind


def test_the_narrower_kind_wins_when_two_hints_match():
    assert stacks.classify("tests/integration/e2e/test_x.py") == "e2e"


@pytest.mark.parametrize("source,count", [
    ("def test_a():\n  pass\n", 1),
    ("async def test_a():\n  pass\n", 1),
    ("def test_a():\n  pass\ndef test_b():\n  pass\n", 2),
    ("def helper():\n  pass\n", 0),
    ("# def test_commented():\n", 0),
])
def test_python_tests_are_counted(source, count):
    assert stacks.count_tests(source, stacks.TEST_FILES[0][2]) == count


@pytest.mark.parametrize("source,count", [
    ("it('a', () => {})\n", 1),
    ("test('a', () => {})\n", 1),
    ("it.each([1])('a', () => {})\n", 1),
    ("describe('a', () => {})\n", 0),
])
def test_javascript_tests_are_counted(source, count):
    assert stacks.count_tests(source, stacks.TEST_FILES[1][2]) == count


def test_go_tests_are_counted():
    source = "func TestA(t *testing.T) {}\nfunc BenchmarkB(b *testing.B) {}\n"
    assert stacks.count_tests(source, stacks.TEST_FILES[2][2]) == 1


def repo(tmp_path, files):
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def test_inventory_counts_files_tests_and_kinds(tmp_path):
    root = repo(tmp_path, {
        "tests/unit/test_a.py": "def test_x():\n  pass\ndef test_y():\n  pass\n",
        "tests/e2e/a.spec.ts": "it('x', () => {})\n",
    })
    report = stacks.inventory(root)
    assert report["total_tests"] == 3
    assert report["kinds"]["unit"]["tests"] == 2
    assert report["kinds"]["e2e"]["files"] == 1


def test_an_absent_kind_is_only_a_gap_when_the_repo_wants_it(tmp_path):
    """A plain package has no server and no CLI, so e2e is not a gap in it."""
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='lib'\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    report = stacks.inventory(root)
    assert report["missing_kinds"] == []
    assert "e2e" in report["not_applicable"]
    assert report["kinds"]["e2e"]["state"] == "not applicable"


def test_a_web_app_without_e2e_tests_is_a_gap(tmp_path):
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='api'\ndependencies=['fastapi']\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    report = stacks.inventory(root)
    assert "e2e" in report["missing_kinds"]
    assert report["kinds"]["e2e"]["evidence"] == ["fastapi"]
    assert "wants them" in stacks.render(report)


def test_a_cli_without_smoke_tests_is_a_gap(tmp_path):
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='tool'\n[project.scripts]\nt='t:main'\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    report = stacks.inventory(root)
    assert "smoke" in report["missing_kinds"]
    assert "an installed entry point" in report["kinds"]["smoke"]["evidence"]


def test_an_api_spec_makes_contract_tests_expected(tmp_path):
    root = repo(tmp_path, {
        "openapi.yaml": "openapi: 3.0.0\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    report = stacks.inventory(root)
    assert "contract" in report["missing_kinds"]
    assert "openapi.yaml" in report["kinds"]["contract"]["evidence"]


def test_a_proto_directory_makes_contract_tests_expected(tmp_path):
    root = repo(tmp_path, {
        "proto/service.proto": "syntax = \"proto3\";\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    assert "contract" in stacks.inventory(root)["missing_kinds"]


def test_a_benchmark_tool_makes_performance_tests_expected(tmp_path):
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\ndependencies=['pytest-benchmark']\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    assert "performance" in stacks.inventory(root)["missing_kinds"]


def test_a_database_dependency_makes_integration_tests_expected(tmp_path):
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\ndependencies=['sqlalchemy']\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
    })
    assert "integration" in stacks.inventory(root)["missing_kinds"]


def test_a_present_kind_is_covered_even_when_not_expected(tmp_path):
    """Having more kinds than the evidence calls for is never a complaint."""
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='lib'\n",
        "tests/unit/test_a.py": "def test_x():\n  assert f() == 1\n",
        "tests/e2e/test_b.py": "def test_y():\n  assert g() == 2\n",
    })
    report = stacks.inventory(root)
    assert report["kinds"]["e2e"]["state"] == "covered"
    assert report["kinds"]["e2e"]["expected"] is False, "covered without being asked for"
    assert report["missing_kinds"] == []


@pytest.mark.parametrize("text,name,found", [
    ("pytest-benchmark", "pytest_benchmark", True),
    ("pytest_benchmark", "pytest-benchmark", True),
    ('"react": "^18.3.1"', "react", True),
    ("dependencies = ['fastapi>=0.1']", "fastapi", True),
    ("reactive-streams", "react", False),
    ("my_flask_helper", "flask", False),
    ("flasky", "flask", False),
])
def test_dependency_names_match_exactly_not_as_substrings(text, name, found):
    assert stacks._mentions(text, name) is found


def test_unit_tests_are_expected_of_any_repo(tmp_path):
    assert stacks.expectations(tmp_path)["unit"]["expected"] is True


def test_vendored_directories_are_not_counted(tmp_path):
    root = repo(tmp_path, {
        "node_modules/p/a.test.js": "it('x', () => {})\n",
        ".venv/lib/test_a.py": "def test_x():\n  pass\n",
        "tests/test_real.py": "def test_x():\n  pass\n",
    })
    report = stacks.inventory(root)
    assert report["total_files"] == 1
    assert report["files"][0]["path"] == "tests/test_real.py"


def test_runners_are_found_from_their_config_files(tmp_path):
    root = repo(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "playwright.config.ts": "export default {}\n",
        "vitest.config.ts": "export default {}\n",
    })
    found = {r["runner"] for r in stacks.inventory(root)["runners"]}
    assert found == {"pytest", "playwright", "vitest"}


def test_runners_needing_extra_wiring_say_what_it_is(tmp_path):
    """Playwright and cypress can report coverage, but only once a reporter or
    instrumentation is configured. Saying 'yes' without that produces an empty
    report and a number that lies."""
    root = repo(tmp_path, {"playwright.config.ts": "export default {}\n"})
    report = stacks.inventory(root)
    assert report["coverage_runners"] == ["playwright"]
    found = report["runners"][0]
    assert found["coverage"] is True
    assert "monocart" in found["setup"]
    assert "needs setup:" in stacks.render(report)


def test_pytest_needs_no_extra_wiring(tmp_path):
    root = repo(tmp_path, {"pyproject.toml": "[project]\nname='x'\n"})
    assert stacks.inventory(root)["runners"][0]["setup"] == ""


def test_go_reports_real_coverage_now(tmp_path):
    root = repo(tmp_path, {"go.mod": "module x\n"})
    found = stacks.inventory(root)["runners"][0]
    assert found["coverage"] is True
    assert found["report"] == "coverage.out"
    assert "-coverprofile" in found["command"]


def test_a_nested_runner_config_is_still_found(tmp_path):
    root = repo(tmp_path, {"packages/web/vitest.config.ts": "export default {}\n"})
    assert [r["runner"] for r in stacks.inventory(root)["runners"]] == ["vitest"]


def test_an_empty_repo_only_misses_unit_tests(tmp_path):
    report = stacks.inventory(tmp_path)
    assert report["total_files"] == 0
    assert report["missing_kinds"] == ["unit"]
    assert set(report["not_applicable"]) == set(stacks.ALL_KINDS) - {"unit"}
    assert "No test runner config found" in stacks.render(report)


def test_every_runner_entry_has_the_fields_the_skill_reads():
    for runner in stacks.RUNNERS:
        for field in ("runner", "language", "evidence", "command", "coverage",
                      "install", "setup"):
            assert field in runner, runner["runner"]
        assert isinstance(runner["coverage"], bool)
