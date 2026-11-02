#!/usr/bin/env python3
"""Say which function owns a line, and how branchy that function is.

A coverage report gives line numbers. A line number is not something anyone
can write a test against; a function name is. This maps one to the other
using the standard library's own parser, and scores each function's
branchiness so a tangled function outranks a long flat one.

    python3 pysource.py --self-check

Python only. Other languages get line numbers without function names, which
is still useful, just less so.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Nodes that add a path through a function. Counting these approximates
# cyclomatic complexity without pulling in a dependency.
# ponytail: node counting, not a real control-flow graph. Good enough to
# order functions; swap for a CFG if the ordering ever misleads.
BRANCHING = (
    ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
    ast.ExceptHandler, ast.Assert, ast.With, ast.AsyncWith,
    ast.BoolOp, ast.comprehension,
)

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Func:
    name: str
    start: int
    end: int
    complexity: int

    def covers(self, line: int) -> bool:
        return self.start <= line <= self.end


def functions(source: str) -> tuple[Func, ...]:
    """Every function and method in the source, outermost first.

    Returns () for source that will not parse — a file mid-edit should not
    take the whole report down with it.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()

    found: list[Func] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTIONS):
                name = f"{prefix}{child.name}"
                found.append(Func(
                    name=name,
                    start=child.lineno,
                    end=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    complexity=complexity(child),
                ))
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return tuple(sorted(found, key=lambda f: (f.start, f.end)))


def complexity(func: ast.AST) -> int:
    """1 plus the branch points in this function, nested functions excluded."""
    count = 1

    def walk(node: ast.AST) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTIONS):
                continue  # the nested function is scored on its own
            if isinstance(child, BRANCHING):
                count += 1
            walk(child)

    walk(func)
    return count


# Anything that can make a test fail on purpose. A test with none of these
# runs the code and then agrees with whatever happened.
ASSERTING_CALLS = (
    "assert", "raises", "warns", "approx", "fail", "expect",
    "check", "verify", "assert_called", "assert_any_call",
    "assert_has_calls", "assert_not_called", "assert_awaited",
)


def _asserts(node: ast.AST) -> bool:
    """True when this subtree can fail the test, nested functions excluded."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, FUNCTIONS):
            continue
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if _names_an_assertion(item.context_expr):
                    return True
        if isinstance(child, ast.Call) and _names_an_assertion(child):
            return True
        if _asserts(child):
            return True
    return False


def _names_an_assertion(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    name = ""
    if isinstance(target, ast.Attribute):
        name = target.attr
    elif isinstance(target, ast.Name):
        name = target.id
    lowered = name.lower()
    return any(lowered.startswith(token) or token in lowered for token in ASSERTING_CALLS)


def _only_bare_calls(node: ast.AST) -> bool:
    """True when the body is nothing but call statements.

    `validate_codec("h264", "out.mp4")` on its own is a deliberate test: it
    passes only if the call does not raise, and it usually sits next to a
    pytest.raises test for the negative case. That is weaker than an explicit
    assertion but it is not nothing, so it is reported apart from a test that
    genuinely cannot fail.
    """
    body = [
        stmt for stmt in getattr(node, "body", ())
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    return bool(body) and all(
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) for stmt in body
    )


def unasserted(source: str) -> tuple[dict, ...]:
    """Test functions with no explicit assertion, split by how weak they are.

    - relies_on_no_raise: the body is bare calls. It fails if one raises.
    - checks_nothing: it computes, prints or assigns, and can never fail.
    """
    found = []
    for func, node in _test_functions(source):
        if _asserts(node):
            continue
        found.append({
            "test": func.name,
            "kind": "relies_on_no_raise" if _only_bare_calls(node) else "checks_nothing",
        })
    return tuple(found)


def assertionless(source: str) -> tuple[str, ...]:
    """Every test with no explicit assertion, by name."""
    return tuple(record["test"] for record in unasserted(source))


# Exceptions so broad that a typo in the code under test satisfies the test:
# a misspelled name raises NameError, a wrong signature raises TypeError, and
# pytest.raises(Exception) accepts both.
BROAD_EXCEPTIONS = ("Exception", "BaseException")


def _is_truthy_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    return False


def _always_true(node: ast.Assert) -> str | None:
    """Why this assertion can never fail, or None if it can."""
    test = node.test
    if _is_truthy_literal(test):
        return "asserts a literal"
    if isinstance(test, ast.Compare) and len(test.comparators) == 1:
        if ast.dump(test.left) == ast.dump(test.comparators[0]):
            if isinstance(test.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)):
                return "both sides are the same expression"
    return None


def _tautological_call(node: ast.Call) -> str | None:
    """unittest-style assertions that compare a value with itself."""
    name = node.func.attr if isinstance(node.func, ast.Attribute) else (
        node.func.id if isinstance(node.func, ast.Name) else "")
    if name in ("assertEqual", "assertIs", "assertNotEqual") and len(node.args) == 2:
        if ast.dump(node.args[0]) == ast.dump(node.args[1]):
            return f"{name} on two identical expressions"
    if name in ("assertTrue", "assertFalse") and len(node.args) == 1:
        if isinstance(node.args[0], ast.Constant):
            return f"{name} on a literal"
    return None


def _checks_the_exception(func: ast.AST, bound: set[str]) -> bool:
    """True when the test asserts on an exception it captured.

        with pytest.raises(Exception) as info:
            boom()
        assert "ffmpeg" in str(info.value)

    That message check is the narrowing, so the broad class is not the
    whole story and flagging it would be wrong.
    """
    if not bound:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Assert):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in bound:
                return True
    return False


def _broad_raises(node: ast.AST) -> str | None:
    """pytest.raises(Exception) with no match= accepts a typo as a pass."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else "")
    if name not in ("raises", "assertRaises"):
        return None
    if not node.args:
        return None
    first = node.args[0]
    caught = first.id if isinstance(first, ast.Name) else (
        first.attr if isinstance(first, ast.Attribute) else "")
    if caught not in BROAD_EXCEPTIONS:
        return None
    if any(kw.arg == "match" for kw in node.keywords):
        return None
    return f"{name}({caught}) with no match="


def _has_a_subject(node: ast.AST) -> bool:
    """True when the test touches something outside itself.

    A call is the usual way, but an attribute reference counts too:

        assert ".gif" not in interactive.ENCODE_EXTS

    calls nothing and is still a real test of a real invariant. Only a test
    that references neither is checking Python rather than the project.
    """
    for child in ast.walk(node):
        if isinstance(child, (ast.Call, ast.Attribute, ast.Subscript)):
            return True
    return False


def _body_fingerprint(node: ast.AST) -> str:
    """The body with names and positions kept but literals intact.

    Two tests with the same fingerprint are the same test twice, which
    usually means a copy-paste where the second one was never adjusted.
    """
    body = [
        stmt for stmt in getattr(node, "body", ())
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    return "|".join(ast.dump(stmt) for stmt in body)


# An assertion that only proves "something came back". The call ran, a value
# exists, and nothing says it is the right value.
WEAK_CALLS = {
    "assertIsNotNone": "only that it is not None",
    "assertIsNone": "only that it is None",
    "assertTrue": "truthiness, not a value",
    "assertFalse": "falsiness, not a value",
    "assert_called": "that it was called, not with what",
    "assert_called_once": "that it was called once, not with what",
    "assert_any_call": "that some call matched, not this one",
}

WEAK_ATTRS = {
    "called": "that it was called, not with what",
    "call_count": "how many times, not with what",
}

SKIP_MARKERS = ("skip", "skipif", "xfail")


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _weakness(node: ast.Assert) -> str | None:
    """Why this assertion proves less than it looks like it does."""
    test = node.test

    # assert x  -- a bare name or attribute: truthy, nothing more
    if isinstance(test, ast.Name):
        return "truthiness of a value, not the value"
    if isinstance(test, ast.Attribute):
        if test.attr in WEAK_ATTRS:
            return WEAK_ATTRS[test.attr]
        return "truthiness of an attribute, not its value"

    # assert x is not None / assert x != None
    if isinstance(test, ast.Compare) and len(test.comparators) == 1:
        right = test.comparators[0]
        op = test.ops[0]
        if isinstance(right, ast.Constant) and right.value is None:
            if isinstance(op, (ast.IsNot, ast.NotEq)):
                return "only that it is not None"
        # assert len(x) > 0 / != 0 / >= 1
        if (isinstance(test.left, ast.Call)
                and _call_name(test.left).rsplit(".", 1)[-1] == "len"
                and isinstance(right, ast.Constant)
                and right.value in (0, 1)
                and isinstance(op, (ast.Gt, ast.NotEq, ast.GtE))):
            return "only that it is not empty, not what is in it"

    # assert f(x)  -- the call's own truthiness
    if isinstance(test, ast.Call):
        name = _call_name(test).rsplit(".", 1)[-1]
        if name in WEAK_CALLS:
            return WEAK_CALLS[name]
        return "truthiness of a call's result, not the result"

    return None


def weak_assertions(source: str) -> tuple[dict, ...]:
    """Assertions that prove something exists without proving it is right.

    `assert result` passes for 1, for "oops" and for a half-built object.
    `assert result == 42` does not. The difference is the whole test.

    A test with at least one strong assertion is left alone: the weak one is
    then a guard, not the point of the test.
    """
    found = []
    for func, node in _test_functions(source):
        weak: list[str] = []
        strong = 0
        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                why = _weakness(child)
                if why:
                    weak.append(why)
                else:
                    strong += 1
            elif isinstance(child, ast.Call):
                name = _call_name(child).rsplit(".", 1)[-1]
                if name in WEAK_CALLS:
                    # assertTrue(1) is a tautology, which always_true already
                    # says more precisely. Only a real subject is weak.
                    first = child.args[0] if child.args else None
                    if not isinstance(first, ast.Constant):
                        weak.append(WEAK_CALLS[name])
                elif name.startswith("assert") and name not in ("assert_called_with",):
                    strong += 1
                elif name in ("assert_called_with", "assert_called_once_with",
                              "assert_has_calls", "assert_awaited_with"):
                    strong += 1
        if weak and not strong:
            found.append({
                "test": func.name,
                "kind": "weak_assertion",
                "line": func.start,
                "why": f"checks {weak[0]}",
            })
    return tuple(found)


def focus_and_skips(source: str) -> tuple[dict, ...]:
    """Tests that never run.

    A permanently skipped test reads as a test in every count and catches
    nothing. Whether it is waiting on a fix or simply forgotten, the suite
    should say so out loud rather than quietly carry it.
    """
    out = []
    for func, node in _test_functions(source):
        for decorator in getattr(node, "decorator_list", ()):
            name = _decorator_name(decorator)
            last = name.rsplit(".", 1)[-1]
            if last not in SKIP_MARKERS:
                continue
            reason = ""
            if isinstance(decorator, ast.Call):
                for kw in decorator.keywords:
                    if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                        reason = str(kw.value.value)
            conditional = last == "skipif"
            out.append({
                "test": func.name,
                "kind": "skipped",
                "line": func.start,
                "why": (f"{name}"
                        + (f": {reason}" if reason else "")
                        + ("" if reason or conditional else ", with no reason given")
                        + (" (conditional)" if conditional else "")),
            })
            break
        else:
            body = getattr(node, "body", ())
            first = body[0] if body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                    and _decorator_name(first.value).rsplit(".", 1)[-1] == "skip"):
                out.append({
                    "test": func.name, "kind": "skipped", "line": func.start,
                    "why": "skips itself on the first line",
                })
    return tuple(out)


# --- faked backends and faked objects ---
#
# A fake that cannot fail is worse than no test: it reports green while the
# real thing has moved. These are the settings that decide whether a Python
# fake can fail at all.

SPEC_KEYWORDS = ("autospec", "spec", "spec_set", "new_callable", "new")

ASSERT_CALLED = re.compile(r"\bassert_(?:any_)?called(?:_once)?(?:_with)?\b")

LOOSE_RECORD_MODES = ("all", "new_episodes")


def _call_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _keyword(node: ast.Call, name: str):
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _assigned_names(stmt: ast.AST) -> list[str]:
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    names = []
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.append(node.id)
    return names


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def unused_results(source: str) -> tuple[dict, ...]:
    """Tests that capture a call's result and never look at it.

        def test_x():
            result = compute(1)

    The assertion belongs on that line and is not there. This is narrower
    than "asserts nothing": the test went to the trouble of asking for the
    answer, then dropped it.
    """
    found = []
    for func, node in _test_functions(source):
        used = _loaded_names(node)
        dropped = []
        for stmt in ast.walk(node):
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            value = getattr(stmt, "value", None)
            if not isinstance(value, ast.Call):
                continue
            names = _assigned_names(stmt)
            # A leading underscore is the documented way to say "ignore this".
            # Unpacking and using only one value still means the call was
            # looked at, so the whole statement passes or none of it does.
            candidates = [n for n in names if not n.startswith("_")]
            if candidates and not any(n in used for n in candidates):
                dropped.extend(candidates)
        if dropped:
            found.append({
                "test": func.name,
                "kind": "unused_result",
                "why": (f"captures {', '.join(sorted(set(dropped)))} from a call and "
                        "never looks at it -- the assertion belongs there"),
            })
    return tuple(found)


def dead_fixtures(source: str, is_conftest: bool = False) -> tuple[dict, ...]:
    """Fixtures in a test file that no test in that file asks for.

    A conftest shares its fixtures with every file beside it, so it is never
    checked. An autouse fixture is requested by definition. Anything left is
    test code nothing runs.
    """
    if is_conftest:
        return ()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()

    fixtures: dict[str, int] = {}
    requested: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, FUNCTIONS):
            continue
        is_fixture = False
        autouse = False
        for decorator in node.decorator_list:
            name = _decorator_name(decorator)
            if name.rsplit(".", 1)[-1] != "fixture":
                continue
            is_fixture = True
            if isinstance(decorator, ast.Call):
                value = _keyword(decorator, "autouse")
                if isinstance(value, ast.Constant) and value.value:
                    autouse = True
        if is_fixture:
            if not autouse:
                fixtures[node.name] = node.lineno
            continue
        if node.name.startswith("test"):
            requested.update(arg.arg for arg in node.args.args)
            requested.update(arg.arg for arg in node.args.kwonlyargs)

    # a fixture may ask for another fixture, and usefixtures names them as text
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS) and node.name in fixtures:
            requested.update(arg.arg for arg in node.args.args)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            requested.add(node.value)

    return tuple(
        {"test": name, "kind": "dead_fixture", "line": line,
         "why": "a fixture no test in this file asks for"}
        for name, line in sorted(fixtures.items(), key=lambda pair: pair[1])
        if name not in requested
    )


def mock_findings(source: str) -> tuple[dict, ...]:
    """What is wrong with this file's fakes, if anything."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()

    found: list[dict] = []
    spec_less_patches = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        last = name.rsplit(".", 1)[-1]

        if last == "patch" or name.endswith("patch.object"):
            if not any(_keyword(node, kw) is not None for kw in SPEC_KEYWORDS):
                spec_less_patches += 1

        if last == "RequestsMock" or name.endswith("responses.mock"):
            fired = _keyword(node, "assert_all_requests_are_fired")
            if fired is None or (isinstance(fired, ast.Constant) and not fired.value):
                found.append({
                    "kind": "mock_unfired",
                    "why": ("responses with assert_all_requests_are_fired off -- a "
                            "registered response that is never requested does not "
                            "fail the test, so a call that stopped happening passes"),
                })

        if name.endswith("respx.mock") or last == "MockRouter":
            for kw_name, what in (("assert_all_called", "a mock nobody called"),
                                  ("assert_all_mocked", "a request nobody mocked")):
                value = _keyword(node, kw_name)
                if isinstance(value, ast.Constant) and not value.value:
                    found.append({
                        "kind": "mock_unfired",
                        "why": f"respx with {kw_name}=False -- {what} passes silently",
                    })

        if last in ("VCR", "use_cassette") or name.endswith("vcr.use_cassette"):
            mode = _keyword(node, "record_mode")
            if isinstance(mode, ast.Constant) and mode.value in LOOSE_RECORD_MODES:
                found.append({
                    "kind": "mock_rerecords",
                    "why": (f"vcr record_mode is {mode.value!r} -- the cassette is "
                            "rewritten from whatever the API says today, so a "
                            "breaking change is recorded instead of caught"),
                })

    if spec_less_patches and ASSERT_CALLED.search(source):
        found.append({
            "kind": "mock_no_spec",
            "why": (f"{spec_less_patches} patch(es) with no autospec or spec, and the "
                    "file asserts on how they were called -- a mock with no spec "
                    "accepts any signature, so renaming an argument of the real "
                    "function leaves the assertion passing"),
        })

    return tuple(found)


def suspect_tests(source: str) -> tuple[dict, ...]:
    """Tests that look like tests but do not test what they appear to.

    Four things, each one a reason a green test can be meaningless:

    - always_true   an assertion that cannot fail: a literal, or both sides
                    of a comparison being the same expression
    - broad_raises  pytest.raises(Exception) with no match=, which a typo in
                    the name under test satisfies just as well as the bug
    - no_subject    asserts, but calls nothing at all, so it only checks
                    Python itself
    - duplicate     the same body as another test in the file
    """
    found: list[dict] = []
    fingerprints: dict[str, str] = {}

    for func, node in _test_functions(source):
        reasons: list[tuple[str, str]] = []
        bound: set[str] = set()
        broad: list[str] = []

        for child in ast.walk(node):
            if isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None and isinstance(
                        item.optional_vars, ast.Name
                    ):
                        bound.add(item.optional_vars.id)
            if isinstance(child, ast.Assert):
                why = _always_true(child)
                if why:
                    reasons.append(("always_true", why))
            elif isinstance(child, ast.Call):
                why = _tautological_call(child)
                if why:
                    reasons.append(("always_true", why))
                why = _broad_raises(child)
                if why:
                    broad.append(why)

        if broad and not _checks_the_exception(node, bound):
            reasons.append(("broad_raises", broad[0]))

        asserts_something = any(
            isinstance(child, ast.Assert) for child in ast.walk(node)
        )
        if asserts_something and not _has_a_subject(node):
            reasons.append(("no_subject", "asserts without touching the project"))

        print_ = _body_fingerprint(node)
        if print_ and print_ in fingerprints:
            reasons.append(("duplicate", f"same body as {fingerprints[print_]}"))
        elif print_:
            fingerprints[print_] = func.name

        for kind, why in reasons:
            found.append({"test": func.name, "kind": kind, "why": why})

    return tuple(found) + unused_results(source) + weak_assertions(source)


def _test_functions(source: str) -> tuple[tuple[Func, ast.AST], ...]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()

    found = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTIONS):
                name = f"{prefix}{child.name}"
                if child.name.startswith("test"):
                    found.append((
                        Func(name, child.lineno,
                             getattr(child, "end_lineno", child.lineno) or child.lineno,
                             complexity(child)),
                        child,
                    ))
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return tuple(found)


def owner(funcs: tuple[Func, ...], line: int) -> Func | None:
    """The innermost function containing the line, or None at module level."""
    hits = [f for f in funcs if f.covers(line)]
    return min(hits, key=lambda f: f.end - f.start) if hits else None


def inside(path: str | Path, root: str | Path | None) -> bool:
    """True when path resolves inside root. Paths come from a coverage report,
    which is just a file on disk: it may name ../../etc/passwd or a symlink
    pointing anywhere, so the caller confines reads to the repo it asked about."""
    if root is None:
        return True
    try:
        resolved, base = Path(path).resolve(), Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError covers an embedded null byte, which a crafted coverage
        # report can carry. Anything unresolvable is treated as outside.
        return False
    return resolved == base or base in resolved.parents


def group(path: str | Path, lines: tuple[int, ...],
          root: str | Path | None = None) -> tuple[dict, ...]:
    """Uncovered lines grouped by the function that owns them."""
    if not inside(path, root):
        return ()
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        return ()
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()

    funcs = functions(source)
    if not funcs:
        return ()

    buckets: dict[str, dict] = {}
    for line in lines:
        found = owner(funcs, line)
        key = found.name if found else "<module level>"
        bucket = buckets.setdefault(key, {
            "function": key,
            "lines": [],
            "complexity": found.complexity if found else 0,
            "start": found.start if found else 0,
        })
        bucket["lines"].append(line)

    ordered = sorted(
        buckets.values(),
        key=lambda b: (-b["complexity"], -len(b["lines"]), b["start"]),
    )
    return tuple(ordered)


def peak_complexity(path: str | Path, lines: tuple[int, ...],
                    root: str | Path | None = None) -> int:
    """Complexity of the branchiest function with uncovered lines in it."""
    return max((b["complexity"] for b in group(path, lines, root)), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="Python file to list functions for")
    parser.add_argument("--assertionless", action="store_true",
                        help="list test functions in the file that assert nothing")
    parser.add_argument("--suspect", action="store_true",
                        help="list tests that cannot fail, catch too much, or repeat")
    parser.add_argument("--skips", action="store_true",
                        help="list tests that never run")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.path:
        parser.error("pass a path (or --self-check)")

    file = Path(args.path)
    if not file.is_file():
        print(f"gtest: no such file: {file}", file=sys.stderr)
        return 2
    source = file.read_text(encoding="utf-8")
    if args.assertionless:
        bare = unasserted(source)
        if not bare:
            print("every test in this file asserts something")
        for record in bare:
            note = ("relies on the call not raising"
                    if record["kind"] == "relies_on_no_raise" else "checks nothing at all")
            print(f"no explicit assertion: {record['test']}  ({note})")
        return 0
    if args.skips:
        skips = focus_and_skips(source)
        if not skips:
            print("every test in this file runs")
        for record in skips:
            print(f"{record['kind']:<9} {record['test']}: {record['why']}")
        return 0
    if args.suspect:
        bad = suspect_tests(source)
        if not bad:
            print("no suspect tests in this file")
        for record in bad:
            print(f"{record['kind']:<13} {record['test']}: {record['why']}")
        return 0
    for func in functions(source):
        print(f"{func.start:>5}-{func.end:<5} c={func.complexity:<3} {func.name}")
    return 0


# --- self-check ---

SAMPLE = '''
import os


def plain():
    return 1


def branchy(items):
    total = 0
    for item in items:
        if item and item > 0:
            total += item
        else:
            total -= 1
    try:
        return total / len(items)
    except ZeroDivisionError:
        return 0


class Session:
    def expire(self, now):
        if now > self.until:
            return True
        return False

    async def refresh(self):
        def inner():
            if True:
                return 2
        return inner()
'''


def self_check() -> int:
    funcs = functions(SAMPLE)
    names = [f.name for f in funcs]
    assert names == [
        "plain", "branchy",
        "Session.expire", "Session.refresh", "Session.refresh.inner",
    ], names

    by_name = {f.name: f for f in funcs}
    assert by_name["plain"].complexity == 1, by_name["plain"]
    # for + (if + and) + else-arm counted via If + try handler
    assert by_name["branchy"].complexity >= 5, by_name["branchy"]
    assert by_name["Session.expire"].complexity == 2, by_name["Session.expire"]
    # a nested function's branches belong to the nested function, not the parent
    assert by_name["Session.refresh"].complexity == 1, by_name["Session.refresh"]
    assert by_name["Session.refresh.inner"].complexity == 2, by_name["Session.refresh.inner"]

    # the innermost function wins an overlapping line
    inner = by_name["Session.refresh.inner"]
    assert owner(funcs, inner.start).name == "Session.refresh.inner"
    assert owner(funcs, by_name["plain"].start).name == "plain"
    assert owner(funcs, 2) is None, "an import line belongs to no function"

    assert functions("def broken(:\n  pass") == (), "unparseable source yields no functions"
    assert functions("") == ()
    assert owner((), 5) is None

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.py"
        path.write_text(SAMPLE, encoding="utf-8")
        missed = tuple(f.start for f in funcs) + (2,)
        grouped = group(path, missed)
        by_func = {g["function"]: g for g in grouped}
        assert "<module level>" in by_func, by_func
        assert by_func["<module level>"]["lines"] == [2], by_func["<module level>"]
        assert grouped[0]["function"] == "branchy", grouped
        assert peak_complexity(path, missed) == by_name["branchy"].complexity

        assert group(Path(tmp) / "missing.py", (1,)) == ()
        assert peak_complexity(Path(tmp) / "missing.py", (1,)) == 0

        # a path outside the given root is refused, however it is spelled
        root = Path(tmp)
        assert group(path, missed, root=root) != (), "a path inside root is allowed"
        assert group("/etc/hosts", (1,), root=root) == (), "absolute escape refused"
        assert group(root / ".." / "escape.py", (1,), root=root) == (), "../ escape refused"
        assert peak_complexity("/etc/hosts", (1,), root=root) == 0
        assert inside(path, root) and not inside("/etc/hosts", root)
        assert inside("/etc/hosts", None), "no root means no confinement"

        link = root / "link.py"
        link.symlink_to(path)
        assert group(link, missed, root=root) == (), "symlinks are not followed"

        empty = Path(tmp) / "empty.py"
        empty.write_text("x = 1\n", encoding="utf-8")
        assert group(empty, (1,)) == (), "a file with no functions groups to nothing"

    tests = """
def test_asserts():
    assert 1 == 1

def test_unittest_style(self):
    self.assertEqual(1, 1)

def test_raises_block():
    with pytest.raises(ValueError):
        boom()

def test_mock_called(m):
    m.assert_called_once()

def test_approx():
    assert_that(x).is_equal_to(1)

def test_nothing():
    compute(1, 2)

def test_only_prints():
    print(compute(1, 2))

def test_explicit_raise():
    if not ok():
        raise AssertionError("no")

def helper_not_a_test():
    compute(1)

class TestThing:
    def test_method_nothing(self):
        self.subject.run()
"""
    bare = assertionless(tests)
    assert "test_nothing" in bare, bare
    assert "test_only_prints" in bare, bare
    assert "TestThing.test_method_nothing" in bare, bare
    assert "test_asserts" not in bare, "a plain assert counts"
    assert "test_unittest_style" not in bare, "self.assertEqual counts"
    assert "test_raises_block" not in bare, "pytest.raises counts"
    assert "test_mock_called" not in bare, "a mock assertion counts"
    assert "test_approx" not in bare, "a fluent assert_that counts"
    assert "test_explicit_raise" not in bare, "an explicit raise can fail the test"
    assert not any("helper" in name for name in bare), "non-test functions are ignored"
    assert len(bare) == 3, bare

    # a nested helper's assertion does not cover the test that calls nothing
    nested = """
def test_outer():
    def inner():
        assert False
"""
    assert assertionless(nested) == ("test_outer",), assertionless(nested)

    # a bare call is a "does not raise" test, not a test that cannot fail
    kinds = {r["test"]: r["kind"] for r in unasserted(tests)}
    assert kinds["test_nothing"] == "relies_on_no_raise", kinds
    assert kinds["test_only_prints"] == "relies_on_no_raise", kinds
    assert kinds["TestThing.test_method_nothing"] == "relies_on_no_raise", kinds
    assigns = unasserted("def test_x():\n    y = 1\n")
    assert assigns[0]["kind"] == "checks_nothing", assigns
    empty_body = unasserted("def test_x():\n    pass\n")
    assert empty_body[0]["kind"] == "checks_nothing", empty_body
    docstring_only = unasserted('def test_x():\n    """why"""\n    run()\n')
    assert docstring_only[0]["kind"] == "relies_on_no_raise", "a docstring is not a statement"
    assert assertionless("def broken(:\n") == ()
    assert assertionless("") == ()

    suspects = """
def test_asserts_a_literal():
    run()
    assert True

def test_asserts_a_nonempty_list():
    run()
    assert [1, 2]

def test_compares_a_value_with_itself(x):
    run()
    assert x == x

def test_unittest_identical(self):
    self.assertEqual(compute(1), compute(1))

def test_assert_true_on_literal(self):
    self.assertTrue(1)

def test_catches_everything():
    with pytest.raises(Exception):
        boom()

def test_catches_everything_but_checks_the_message():
    with pytest.raises(Exception) as info:
        boom()
    assert "ffmpeg" in str(info.value)

def test_asserts_on_a_module_constant():
    assert ".gif" not in interactive.ENCODE_EXTS

def test_catches_everything_but_checks_the_message():
    with pytest.raises(Exception, match="boom"):
        boom()

def test_catches_the_right_thing():
    with pytest.raises(ValueError):
        boom()

def test_no_subject():
    assert 1 + 1 == 2

def test_real_one():
    assert compute(2) == 4

def test_copy_a():
    assert compute(3) == 9

def test_copy_b():
    assert compute(3) == 9
"""
    by_test: dict[str, set[str]] = {}
    for record in suspect_tests(suspects):
        by_test.setdefault(record["test"], set()).add(record["kind"])

    assert by_test["test_asserts_a_literal"] == {"always_true"}, by_test
    assert by_test["test_asserts_a_nonempty_list"] == {"always_true"}, by_test
    assert by_test["test_compares_a_value_with_itself"] == {"always_true"}, by_test
    assert by_test["test_unittest_identical"] == {"always_true"}, by_test
    assert by_test["test_assert_true_on_literal"] == {"always_true"}, by_test
    assert by_test["test_catches_everything"] == {"broad_raises"}, by_test
    assert "test_catches_everything_but_checks_the_message" not in by_test, \
        "an assertion on the captured exception is the narrowing"
    assert "test_asserts_on_a_module_constant" not in by_test, \
        "an attribute reference is a real subject, even with no call"
    assert "test_catches_the_right_thing" not in by_test, "a named exception is fine"
    assert "test_catches_the_right_thing" not in by_test, "a named exception is fine"
    assert by_test["test_no_subject"] == {"no_subject"}, by_test
    assert "test_real_one" not in by_test, "a real assertion on a real call is fine"
    assert by_test["test_copy_b"] == {"duplicate"}, by_test
    assert "test_copy_a" not in by_test, "only the second copy is flagged"

    # assert False is a deliberate failure, not a tautology
    assert suspect_tests("def test_x():\n    run()\n    assert False\n") == ()
    # an empty collection is falsy, so asserting it is a real (failing) check
    assert suspect_tests("def test_x():\n    run()\n    assert []\n") == ()
    assert suspect_tests("") == ()
    assert suspect_tests("def test_x(:\n") == ()

    skipped_src = """
import pytest

@pytest.mark.skip(reason="waiting on the api")
def test_with_a_reason():
    assert f() == 1

@pytest.mark.skip
def test_bare_skip():
    assert f() == 1

@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
def test_conditional():
    assert f() == 1

@pytest.mark.xfail
def test_expected_to_fail():
    assert f() == 1

def test_skips_itself():
    pytest.skip("not ready")
    assert f() == 1

@pytest.mark.parametrize("x", [1])
def test_runs(x):
    assert f(x) == 1
"""
    skips = {r["test"]: r["why"] for r in focus_and_skips(skipped_src)}
    assert "waiting on the api" in skips["test_with_a_reason"], skips
    assert "no reason given" in skips["test_bare_skip"], skips
    assert "conditional" in skips["test_conditional"], skips
    assert "xfail" in skips["test_expected_to_fail"], skips
    assert "first line" in skips["test_skips_itself"], skips
    assert "test_runs" not in skips, "parametrize is not a skip"
    assert len(skips) == 5, skips
    assert focus_and_skips("") == ()

    def kinds_of(src):
        return {r["kind"] for r in mock_findings(src)}

    assert kinds_of("responses.RequestsMock(assert_all_requests_are_fired=True)") == set()
    assert kinds_of("responses.RequestsMock(assert_all_requests_are_fired=False)") == \
        {"mock_unfired"}
    assert kinds_of("responses.RequestsMock()") == {"mock_unfired"}, "the default is off"

    assert kinds_of("respx.mock(assert_all_called=False)") == {"mock_unfired"}
    assert kinds_of("respx.mock(assert_all_mocked=False)") == {"mock_unfired"}
    assert kinds_of("respx.mock()") == set(), "respx defaults to strict"

    assert kinds_of("vcr.use_cassette('a.yaml', record_mode='none')") == set()
    assert kinds_of("vcr.use_cassette('a.yaml', record_mode='all')") == {"mock_rerecords"}
    assert kinds_of("vcr.VCR(record_mode='new_episodes')") == {"mock_rerecords"}
    assert kinds_of("vcr.use_cassette('a.yaml')") == set(), "the default records once"

    asserted = """
from unittest.mock import patch

def test_x():
    with patch("app.send") as send:
        run()
    send.assert_called_once_with(1, 2)
"""
    assert kinds_of(asserted) == {"mock_no_spec"}, mock_findings(asserted)
    assert "renaming an argument" in mock_findings(asserted)[0]["why"]

    specced = asserted.replace('patch("app.send")', 'patch("app.send", autospec=True)')
    assert kinds_of(specced) == set(), "autospec makes the assertion meaningful"

    unasserted_patch = """
from unittest.mock import patch

def test_x():
    with patch("time.sleep"):
        assert run() == 1
"""
    assert kinds_of(unasserted_patch) == set(), \
        "a patch nobody asserts on is not the problem"

    dropped = """
def test_drops_the_answer():
    result = compute(1)

def test_keeps_it():
    result = compute(1)
    assert result == 1

def test_says_it_does_not_care():
    _ = compute(1)

def test_unpacks_and_drops():
    first, second = pair()

def test_unpacks_and_uses_one():
    first, second = pair()
    assert first == 1

def test_no_assignment_at_all():
    compute(1)
"""
    by_test = {r["test"]: r["why"] for r in unused_results(dropped)}
    assert "result" in by_test["test_drops_the_answer"], by_test
    assert "test_keeps_it" not in by_test
    assert "test_says_it_does_not_care" not in by_test, "a leading _ means ignore it"
    assert "first, second" in by_test["test_unpacks_and_drops"].replace(" and", ","), by_test
    assert "test_unpacks_and_uses_one" not in by_test, \
        "one name used is enough to show the call was looked at"
    assert "test_no_assignment_at_all" not in by_test, "that is a different finding"
    assert unused_results("") == ()

    # it comes through suspect_tests too, which is what the report reads
    assert "unused_result" in {r["kind"] for r in suspect_tests(dropped)}

    fixtures_src = """
import pytest

@pytest.fixture
def used_one():
    return 1

@pytest.fixture
def never_used():
    return 2

@pytest.fixture(autouse=True)
def always_on():
    return 3

@pytest.fixture
def asked_by_another(used_one):
    return used_one

@pytest.fixture
def chained(asked_by_another):
    return asked_by_another

@pytest.mark.usefixtures("named_as_text")
def test_x(used_one, chained):
    assert used_one == 1

@pytest.fixture
def named_as_text():
    return 4
"""
    dead = {r["test"] for r in dead_fixtures(fixtures_src)}
    assert dead == {"never_used"}, dead
    assert dead_fixtures(fixtures_src, is_conftest=True) == (), \
        "a conftest shares its fixtures with every file beside it"
    assert dead_fixtures("") == ()
    assert dead_fixtures("def broken(:") == ()

    weak_src = """
def test_only_truthy():
    assert compute(1)

def test_bare_name():
    result = compute(1)
    assert result

def test_not_none():
    assert compute(1) is not None

def test_not_empty():
    assert len(items()) > 0

def test_mock_called_only(m):
    run()
    assert m.called

def test_assert_is_not_none(self):
    self.assertIsNotNone(compute(1))

def test_assert_true_on_a_call(self):
    self.assertTrue(compute(1))

def test_real_value():
    assert compute(1) == 42

def test_weak_then_strong():
    result = compute(1)
    assert result
    assert result.total == 42

def test_mock_with_args(m):
    run()
    m.assert_called_once_with(1, 2)
"""
    weak = {r["test"]: r["why"] for r in weak_assertions(weak_src)}
    assert "truthiness of a call" in weak["test_only_truthy"], weak
    assert "truthiness of a value" in weak["test_bare_name"], weak
    assert "not None" in weak["test_not_none"], weak
    assert "not empty" in weak["test_not_empty"], weak
    assert "not with what" in weak["test_mock_called_only"], weak
    assert "not None" in weak["test_assert_is_not_none"], weak
    assert "not a value" in weak["test_assert_true_on_a_call"], weak
    # assertTrue(1) is a tautology, reported as always_true, not as weak
    assert weak_assertions("def test_x(self):\n    self.assertTrue(1)\n") == ()
    assert "test_real_value" not in weak, "a value comparison is strong"
    assert "test_weak_then_strong" not in weak, \
        "one strong assertion makes the weak one a guard, not the point"
    assert "test_mock_with_args" not in weak, "asserting the arguments is strong"
    assert len(weak) == 7, weak

    assert "weak_assertion" in {r["kind"] for r in suspect_tests(weak_src)}
    assert weak_assertions("") == ()

    assert mock_findings("x = 1") == ()
    assert mock_findings("def broken(:") == ()

    print("pysource.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
