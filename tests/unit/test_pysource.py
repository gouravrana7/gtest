"""Unit: line-to-function mapping and the complexity estimate."""
import pytest
import pysource

SRC = """
def flat():
    return 1

def nested(xs):
    for x in xs:
        if x:
            return x
    return None

class Box:
    def put(self, v):
        self.v = v
"""


def test_functions_are_named_with_their_class():
    names = [f.name for f in pysource.functions(SRC)]
    assert names == ["flat", "nested", "Box.put"]


def test_complexity_counts_branch_points():
    by_name = {f.name: f for f in pysource.functions(SRC)}
    assert by_name["flat"].complexity == 1
    assert by_name["nested"].complexity == 3  # for + if + implicit entry


def test_decorated_functions_are_found():
    funcs = pysource.functions("import functools\n\n@functools.cache\ndef cached():\n    return 1\n")
    assert [f.name for f in funcs] == ["cached"]


def test_syntax_errors_yield_no_functions_instead_of_raising():
    assert pysource.functions("def (:\n") == ()


def test_owner_picks_the_innermost_function():
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    funcs = pysource.functions(src)
    assert pysource.owner(funcs, 3).name == "outer.inner"
    assert pysource.owner(funcs, 4).name == "outer"


def test_module_level_lines_have_no_owner():
    assert pysource.owner(pysource.functions(SRC), 1) is None


def test_group_sorts_the_branchiest_function_first(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(SRC)
    funcs = {f.name: f for f in pysource.functions(SRC)}
    lines = (funcs["flat"].start, funcs["nested"].start + 1, funcs["nested"].start + 2)
    grouped = pysource.group(path, lines, root=tmp_path)
    assert grouped[0]["function"] == "nested"


def test_group_labels_module_level_lines(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("x = 1\ndef f():\n    return 1\n")
    grouped = pysource.group(path, (1,), root=tmp_path)
    assert grouped[0]["function"] == "<module level>"


def test_group_refuses_a_path_outside_the_root(tmp_path):
    assert pysource.group("/etc/hosts", (1,), root=tmp_path) == ()


def test_group_does_not_follow_a_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("def f():\n    return 1\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    assert pysource.group(link, (1,), root=tmp_path) == ()


def test_group_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert pysource.group(tmp_path / "gone.py", (1,), root=tmp_path) == ()


def test_peak_complexity_reports_the_worst_function(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(SRC)
    funcs = {f.name: f for f in pysource.functions(SRC)}
    assert pysource.peak_complexity(path, (funcs["nested"].start + 1,), root=tmp_path) == 3


@pytest.mark.parametrize("root,allowed", [(None, True), ("/", True)])
def test_inside_without_a_root_allows_everything(root, allowed):
    assert pysource.inside("/etc/hosts", root) is allowed
