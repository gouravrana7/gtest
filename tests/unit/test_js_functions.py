"""Unit: which JavaScript function owns a line, and how branchy it is.

Python gets this from `ast`. There is no JS parser in the standard library and
this plugin ships no dependencies, so it comes from the scanner — which means
these tests are the specification.
"""

import pytest
import jssource

APP = """
export function plain(a) {
  return a
}

export async function branchy(items) {
  for (const item of items) {
    if (item && item > 0) {
      return item
    }
  }
  return null
}

const arrow = (x) => {
  return x ? 1 : 2
}

const typed: Handler = async (req) => {
  if (req.ok || req.retry) { return 1 }
  return 0
}

class Session {
  expire(now) {
    if (now > this.until) { return true }
    return false
  }

  refresh() {
    const inner = () => {
      if (true) { return 2 }
    }
    return inner()
  }
}
"""


@pytest.fixture
def funcs():
    return {f.name: f for f in jssource.functions(APP)}


@pytest.mark.parametrize("name", [
    "plain", "branchy", "arrow", "typed", "Session.expire", "Session.refresh",
])
def test_every_declaration_style_is_found(name, funcs):
    assert name in funcs


def test_a_method_is_named_with_its_class(funcs):
    assert "Session.expire" in funcs
    assert "expire" not in funcs


def test_a_flat_function_has_complexity_one(funcs):
    assert funcs["plain"].complexity == 1


def test_branches_are_counted(funcs):
    assert funcs["branchy"].complexity >= 4


def test_a_ternary_counts(funcs):
    assert funcs["arrow"].complexity == 2


def test_a_logical_or_counts(funcs):
    assert funcs["typed"].complexity == 3


def test_a_nested_function_keeps_its_own_branches(funcs):
    """refresh contains an arrow with an if; that if belongs to the arrow."""
    assert funcs["Session.refresh"].complexity == 1


@pytest.mark.parametrize("keyword", ["if", "for", "while", "switch", "catch"])
def test_a_keyword_is_not_a_function(keyword):
    source = f"function f() {{\n  {keyword} (x) {{ y() }}\n}}\n"
    assert [f.name for f in jssource.functions(source)] == ["f"]


def test_a_test_harness_name_is_not_a_function():
    assert jssource.functions("it('a', () => { if (x) {} })") == ()
    assert jssource.functions("beforeEach(() => { setup() })") == ()


def test_a_branch_inside_a_string_does_not_count():
    source = 'function f() {\n  return "a && b || c"\n}\n'
    assert jssource.functions(source)[0].complexity == 1


def test_a_branch_inside_a_comment_does_not_count():
    source = "function f() {\n  // if (x) {}\n  return 1\n}\n"
    assert jssource.functions(source)[0].complexity == 1


def test_a_declaration_with_no_body_is_skipped():
    assert jssource.functions("declare function f(a: number): void;\n") == ()


def test_a_brace_free_arrow_is_found():
    found = jssource.functions("const add = (a, b) => a + b\n")
    assert [f.name for f in found] == ["add"]


def test_the_innermost_function_owns_an_overlapping_line(funcs):
    all_funcs = jssource.functions(APP)
    inner_line = funcs["Session.refresh"].start
    assert jssource.owner(all_funcs, inner_line).name == "Session.refresh"


def test_a_line_in_no_function_has_no_owner():
    assert jssource.owner(jssource.functions(APP), 1) is None
    assert jssource.owner((), 5) is None


def test_group_sorts_the_branchiest_first(tmp_path, funcs):
    path = tmp_path / "app.ts"
    path.write_text(APP)
    lines = (funcs["plain"].start, funcs["branchy"].start + 1)
    grouped = jssource.group(path, lines, root=tmp_path)
    assert grouped[0]["function"] == "branchy"


def test_group_refuses_a_path_outside_the_root(tmp_path):
    assert jssource.group("/etc/hosts", (1,), root=tmp_path) == ()


def test_group_does_not_follow_a_symlink(tmp_path):
    real = tmp_path / "a.ts"
    real.write_text(APP)
    link = tmp_path / "link.ts"
    link.symlink_to(real)
    assert jssource.group(link, (2,), root=tmp_path) == ()


def test_group_on_a_missing_file_is_empty(tmp_path):
    assert jssource.group(tmp_path / "gone.ts", (1,), root=tmp_path) == ()


def test_peak_complexity_reports_the_worst(tmp_path, funcs):
    path = tmp_path / "app.ts"
    path.write_text(APP)
    line = funcs["branchy"].start + 1
    assert jssource.peak_complexity(path, (line,), root=tmp_path) == \
        funcs["branchy"].complexity


def test_the_ranker_names_a_typescript_function(tmp_path):
    import rank
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.ts").write_text(
        "export function total(items) {\n"
        "  if (!items) { return 0 }\n"
        "  return items.length\n"
        "}\n"
    )
    rows = rank.rank((rank.Gap("src/cart.ts", (2, 3), 0, 4),), frozenset(), root=tmp_path)
    assert rows[0]["functions"][0]["function"] == "total"
    assert rows[0]["complexity"] == 2


def test_the_ranker_leaves_an_unreadable_language_alone(tmp_path):
    import rank
    rows = rank.rank((rank.Gap("pkg/a.go", (1,), 0, 4),), frozenset(), root=tmp_path)
    assert rows[0]["functions"] == []


def test_empty_source_yields_nothing():
    assert jssource.functions("") == ()
    assert jssource.functions("const x = 1") == ()
