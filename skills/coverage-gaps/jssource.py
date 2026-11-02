#!/usr/bin/env python3
"""Read JavaScript and TypeScript tests without a JavaScript parser.

Python has `ast` for its own tests. For JS there is no parser in the standard
library and this plugin takes no dependencies, so this scans the source with a
small state machine that knows about strings, templates, regexes and comments,
and brace-matches each test body. That is enough to answer the same questions
asked of Python tests: does this test assert anything, can it fail, is it a
copy of another one, and is anything focused or skipped.

    python3 jssource.py tests/a.spec.ts
    python3 jssource.py --self-check

ponytail: a scanner, not a parser. It finds test blocks and their text; it
does not understand expressions. Swap in a real parser if that stops being
enough -- but not for a dependency this plugin would have to ship.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# it / test / describe, optionally with .only, .skip, .each, .concurrent, .todo
TEST_START = re.compile(
    r"\b(?P<fn>it|test|describe|suite)"
    r"(?P<mods>(?:\.(?:only|skip|todo|failing|concurrent|each|sequential))*)"
    r"\s*(?:\(|`)"
)
XSHORTHAND = re.compile(r"\b(?P<fn>xit|xtest|xdescribe|fit|fdescribe)\s*\(")

# Anything that can make a JS test fail on purpose.
ASSERTING = re.compile(
    r"\b(?:expect|assert|should|chai|t\.(?:is|not|true|false|deepEqual|throws)"
    r"|toMatchSnapshot|toThrow|rejects|resolves)\b"
)

# expect(<literal>).toBe(<the same literal>) and friends: always true.
TAUTOLOGY = re.compile(
    r"expect\s*\(\s*(?P<left>true|false|1|0|'[^']*'|\"[^\"]*\")\s*\)"
    r"\s*\.\s*(?:to)?(?:Be|Equal|toEqual|toBe|StrictEqual)\s*\(\s*(?P<right>[^)]*?)\s*\)",
    re.I,
)

TRIVIALLY_TRUE = re.compile(r"expect\s*\(\s*true\s*\)\s*\.\s*toBe(?:Truthy)?\s*\(\s*(?:true)?\s*\)")


# Every way JavaScript and TypeScript declare something callable.
FUNC_PATTERNS = (
    re.compile(r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
               r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("),
    re.compile(r"\b(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)"
               r"\s*(?::[^=]*)?=\s*(?:async\s*)?(?:function\s*\*?\s*\(|\()"),
    re.compile(r"^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*"
               r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*"
               r"(?::\s*[^{;]+)?\{", re.M),
)

CLASS_PATTERN = re.compile(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)")

# Tokens that add a path through a function. Counted on the blanked source,
# so a "||" inside a string is never one of them.
BRANCH_TOKENS = (
    re.compile(r"\bif\b"), re.compile(r"\bfor\b"), re.compile(r"\bwhile\b"),
    re.compile(r"\bcase\b"), re.compile(r"\bcatch\b"),
    re.compile(r"&&"), re.compile(r"\|\|"), re.compile(r"\?\?"),
    re.compile(r"\?(?!\?)[^:]{0,80}:"),
)

# The names a test harness uses; not application functions.
HARNESS_NAMES = {"it", "test", "describe", "suite", "beforeAll", "beforeEach",
                 "afterAll", "afterEach", "expect"}

# A keyword followed by a paren and a brace looks exactly like a method
# declaration to a scanner. It is not one.
JS_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "catch", "try",
    "finally", "return", "typeof", "instanceof", "new", "delete", "void",
    "await", "yield", "throw", "with", "in", "of", "class", "function",
    "const", "let", "var", "export", "import", "default", "extends",
    "super", "this", "constructor",
}


@dataclass(frozen=True)
class JsFunc:
    name: str
    start: int
    end: int
    complexity: int

    def covers(self, line: int) -> bool:
        return self.start <= line <= self.end


@dataclass(frozen=True)
class JsTest:
    name: str
    kind: str          # it | describe
    line: int
    body: str
    focused: bool
    skipped: bool


def _strip_noise(source: str) -> str:
    """Blank out comments, strings, templates and regexes, keeping offsets.

    Replacing rather than deleting means every index still lines up with the
    original, so a brace found here is a brace there.
    """
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            for _ in range(2):
                if i < n:
                    out[i] = " "
                    i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if source[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                    i += 2
                    continue
                if source[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _body_after(source: str, blanked: str, start: int) -> tuple[str, int]:
    """The callback body that follows start.

    Both depths have to be tracked. An arrow function brings its own
    parentheses -- `it('x', () => { ... })` -- so a closing paren only ends
    the test when it closes the test call itself, and a brace only opens the
    body when no brace is open yet.
    """
    # The callback's parameter list can itself contain braces -- Playwright
    # writes `async ({ page }) => { ... }` -- so the body cannot simply be the
    # first brace. Find where the callback body actually begins first.
    body_at = _callback_body_start(blanked, start)

    parens, braces, i, n = 1, 0, start, len(source)
    begin = -1
    while i < n:
        ch = blanked[i]
        if ch == "(":
            parens += 1
        elif ch == ")":
            parens -= 1
            if parens == 0 and braces == 0:
                # a brace-free arrow body: `it('x', () => expect(1).toBe(1))`
                return source[start:i], i
        elif ch == "{":
            if braces == 0:
                if body_at is not None and i < body_at:
                    # a brace in the parameter list, not the body
                    i += 1
                    continue
                begin = i
            braces += 1
        elif ch == "}":
            if braces == 0:
                i += 1
                continue
            braces -= 1
            if braces == 0:
                return source[begin + 1:i], i
        i += 1
    return source[start:], n


def _callback_body_start(blanked: str, start: int) -> int | None:
    """Index of the callback body's opening brace, past its parameter list."""
    depth, i, n = 1, start, len(blanked)
    while i < n:
        ch = blanked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return None
        elif depth == 1 and ch == "=" and i + 1 < n and blanked[i + 1] == ">":
            brace = blanked.find("{", i + 2)
            return brace if brace != -1 else None
        elif depth == 1 and blanked.startswith("function", i):
            brace = blanked.find("{", i + 8)
            return brace if brace != -1 else None
        i += 1
    return None


def _name_after(source: str, start: int) -> str:
    match = re.search(r"""\(\s*(['"`])(?P<name>(?:\\.|(?!\1).)*)\1""", source[start:start + 400])
    return match.group("name") if match else "<unnamed>"


def tests(source: str) -> tuple[JsTest, ...]:
    """Every it/test/describe block in the file."""
    blanked = _strip_noise(source)
    found: list[JsTest] = []

    for pattern in (TEST_START, XSHORTHAND):
        for match in pattern.finditer(blanked):
            fn = match.group("fn")
            mods = match.groupdict().get("mods") or ""
            body, _ = _body_after(source, blanked, match.end())
            found.append(JsTest(
                name=_name_after(source, match.start()),
                kind="describe" if "describe" in fn or "suite" in fn else "it",
                line=source.count("\n", 0, match.start()) + 1,
                body=body,
                focused=".only" in mods or fn in ("fit", "fdescribe"),
                skipped=".skip" in mods or ".todo" in mods
                or fn in ("xit", "xtest", "xdescribe"),
            ))
    return tuple(sorted(found, key=lambda t: t.line))


def cases(source: str) -> tuple[JsTest, ...]:
    """Only the leaf tests, not the describe blocks around them."""
    return tuple(t for t in tests(source) if t.kind == "it")


def functions(source: str) -> tuple[JsFunc, ...]:
    """Every named function in the file, with its line range and branchiness.

    ponytail: a scanner, so a method and a plain function look the same and an
    anonymous callback is not named. It is enough to say which function owns an
    uncovered line, which is the job.
    """
    blanked = _strip_noise(source)
    spans: list[tuple[str, int, int, int, int]] = []

    for pattern in FUNC_PATTERNS:
        for match in pattern.finditer(blanked):
            name = match.group("name")
            if name in HARNESS_NAMES or name in JS_KEYWORDS:
                continue
            open_paren = blanked.find("(", match.start())
            if open_paren == -1:
                continue
            body, end_index = _declaration_body(source, blanked, open_paren)
            if not body.strip():
                continue
            begin_index = match.start()
            spans.append((
                name,
                source.count("\n", 0, begin_index) + 1,
                source.count("\n", 0, end_index) + 1,
                begin_index,
                end_index,
            ))

    found = []
    for name, start, end, begin_index, end_index in spans:
        own = _own_text(blanked, begin_index, end_index, spans)
        found.append(JsFunc(name=_qualify(blanked, begin_index, name),
                            start=start, end=end,
                            complexity=_branches(own)))
    unique = {(f.name, f.start): f for f in found}
    return tuple(sorted(unique.values(), key=lambda f: (f.start, f.end)))


def _declaration_body(source: str, blanked: str, paren: int) -> tuple[str, int]:
    """The body of a function declaration, given its parameter list's paren.

    `_body_after` is for a callback inside a call, where the closing paren
    ends the thing. A declaration is the other way round: the paren closes
    the parameters and the body starts after it.
    """
    n = len(blanked)
    depth, i = 0, paren
    while i < n:
        if blanked[i] == "(":
            depth += 1
        elif blanked[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    else:
        return "", n

    # between the parameters and the body: a return type, an arrow, nothing
    after_params = i
    while i < n and blanked[i] not in "{;\n":
        i += 1
    if i >= n or blanked[i] != "{":
        # A brace-free arrow is the body: const add = (a, b) => a + b
        # A declaration that stops at a semicolon has no body at all.
        body = source[after_params:i]
        if "=>" not in blanked[after_params:i]:
            return "", i
        return body, i

    begin, braces = i, 0
    while i < n:
        if blanked[i] == "{":
            braces += 1
        elif blanked[i] == "}":
            braces -= 1
            if braces == 0:
                return source[begin + 1:i], i
        i += 1
    return source[begin + 1:], n


def _own_text(blanked: str, begin: int, end: int, spans: list) -> str:
    """The function's text with its nested functions cut out."""
    keep = list(blanked[begin:end])
    for _, _, _, inner_begin, inner_end in spans:
        if inner_begin > begin and inner_end <= end:
            for i in range(inner_begin - begin, min(inner_end - begin, len(keep))):
                keep[i] = " "
    return "".join(keep)


def _branches(text: str) -> int:
    return 1 + sum(len(pattern.findall(text)) for pattern in BRANCH_TOKENS)


def _qualify(blanked: str, index: int, name: str) -> str:
    """Prefix a method with the class it sits in, when there is one."""
    best = ""
    for match in CLASS_PATTERN.finditer(blanked, 0, index):
        best = match.group("name")
    return f"{best}.{name}" if best else name


def owner(funcs: tuple[JsFunc, ...], line: int) -> JsFunc | None:
    hits = [f for f in funcs if f.covers(line)]
    return min(hits, key=lambda f: f.end - f.start) if hits else None


def group(path: str | Path, lines: tuple[int, ...],
          root: str | Path | None = None) -> tuple[dict, ...]:
    """Uncovered lines grouped by the function that owns them."""
    file = Path(path)
    if root is not None:
        try:
            resolved, base = file.resolve(), Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            return ()
        if resolved != base and base not in resolved.parents:
            return ()
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
    return tuple(sorted(buckets.values(),
                        key=lambda b: (-b["complexity"], -len(b["lines"]), b["start"])))


def peak_complexity(path: str | Path, lines: tuple[int, ...],
                    root: str | Path | None = None) -> int:
    return max((b["complexity"] for b in group(path, lines, root)), default=0)


def unasserted(source: str) -> tuple[dict, ...]:
    """Leaf tests whose body asserts nothing."""
    out = []
    for case in cases(source):
        if ASSERTING.search(case.body):
            continue
        body = case.body.strip()
        out.append({
            "test": case.name,
            "kind": "checks_nothing" if not body else "relies_on_no_raise",
        })
    return tuple(out)


# An expectation that only proves "something came back".
WEAK_MATCHERS = (
    (re.compile(r"\.\s*toBeTruthy\s*\("), "truthiness, not a value"),
    (re.compile(r"\.\s*toBeFalsy\s*\("), "falsiness, not a value"),
    (re.compile(r"\.\s*toBeDefined\s*\("), "only that it is defined"),
    (re.compile(r"\.\s*not\s*\.\s*toBeUndefined\s*\("), "only that it is defined"),
    (re.compile(r"\.\s*not\s*\.\s*toBeNull\s*\("), "only that it is not null"),
    (re.compile(r"\.\s*toHaveBeenCalled\s*\("), "that it was called, not with what"),
    (re.compile(r"\.\s*toHaveLength\s*\(\s*0\s*\)"), "only that it is empty"),
)

# Anything that pins down an actual value.
STRONG_MATCHERS = re.compile(
    r"\.\s*(?:toBe|toEqual|toStrictEqual|toMatch|toMatchObject|toContain|"
    r"toHaveProperty|toHaveBeenCalledWith|toHaveBeenNthCalledWith|toThrow|"
    r"toHaveText|toHaveTitle|toHaveURL|toHaveValue|toBeVisible|toBeCloseTo|"
    r"toMatchSnapshot|toMatchInlineSnapshot)\s*\("
)


def weak_assertions(source: str) -> tuple[dict, ...]:
    """Expectations that prove something exists without proving it is right.

    A test with at least one strong matcher is left alone: the weak one is
    then a guard rather than the point of the test.
    """
    found = []
    for case in cases(source):
        body = _strip_noise(case.body)
        if STRONG_MATCHERS.search(body):
            continue
        for pattern, why in WEAK_MATCHERS:
            if pattern.search(body):
                found.append({"test": case.name, "kind": "weak_assertion",
                              "line": case.line, "why": f"checks {why}"})
                break
    return tuple(found)


def suspect_tests(source: str) -> tuple[dict, ...]:
    """JS tests that are green for the wrong reason."""
    found: list[dict] = []
    seen: dict[str, str] = {}

    for case in cases(source):
        body = case.body
        if TAUTOLOGY.search(body) or TRIVIALLY_TRUE.search(body):
            found.append({"test": case.name, "kind": "always_true",
                          "why": "expect() compares a literal with itself"})
        if re.search(r"\.toThrow\s*\(\s*\)", body):
            found.append({"test": case.name, "kind": "broad_raises",
                          "why": "toThrow() with no expected error"})
        squashed = re.sub(r"\s+", " ", body).strip()
        if squashed and len(squashed) > 10:
            if squashed in seen:
                found.append({"test": case.name, "kind": "duplicate",
                              "why": f"same body as {seen[squashed]}"})
            else:
                seen[squashed] = case.name
    return tuple(found) + weak_assertions(source)


def focus_and_skips(source: str) -> tuple[dict, ...]:
    """Focused and skipped tests.

    A single `it.only` silently stops every other test in the file from
    running, and CI reports green. It is the quietest way to lose a suite.
    """
    out = []
    for case in tests(source):
        if case.focused:
            out.append({"test": case.name, "kind": "focused", "line": case.line,
                        "why": f"{case.kind}.only silences the rest of the file"})
        elif case.skipped:
            out.append({"test": case.name, "kind": "skipped", "line": case.line,
                        "why": f"{case.kind} is skipped"})
    return tuple(out)


# --- Mock Service Worker ---
#
# A faked backend is the quietest way to a green suite that proves nothing:
# the handlers are written once and the real API moves on. These are the three
# things that decide whether an msw setup can be trusted.

MSW_SETUP = re.compile(r"\b(?:setupServer|setupWorker)\s*\(")
MSW_IMPORT = re.compile(r"""from\s+['"]msw(?:/node|/browser)?['"]""")
UNHANDLED = re.compile(r"onUnhandledRequest\s*:\s*['\"]?(?P<mode>\w+)")
RESET = re.compile(r"\b(?:server|worker)\s*\.\s*resetHandlers\s*\(")
AFTER_EACH = re.compile(r"\bafterEach\s*\(")
LISTEN = re.compile(r"\b(?:server|worker)\s*\.\s*(?:listen|start)\s*\(")


def uses_msw(source: str) -> bool:
    blanked = _strip_noise(source)
    return bool(MSW_SETUP.search(blanked)) or bool(MSW_IMPORT.search(source))


def msw_findings(source: str) -> tuple[dict, ...]:
    """What is wrong with this file's faked backend, if anything."""
    if not uses_msw(source):
        return ()

    blanked = _strip_noise(source)
    found: list[dict] = []

    if LISTEN.search(blanked) or MSW_SETUP.search(blanked):
        match = UNHANDLED.search(source)
        mode = (match.group("mode") if match else "").lower()
        if mode != "error":
            found.append({
                "kind": "msw_lenient",
                "why": (f"onUnhandledRequest is {mode or 'unset (warn)'}, not 'error' -- "
                        "a request no handler matched does not fail the test, so a "
                        "call to a route that no longer exists still passes"),
            })

    if MSW_SETUP.search(blanked) and not RESET.search(blanked):
        found.append({
            "kind": "msw_no_reset",
            "why": ("no resetHandlers() call -- a handler one test overrides with "
                    "server.use() stays overridden for every test after it, which "
                    "makes the suite order-dependent"),
        })
    elif RESET.search(blanked) and not AFTER_EACH.search(blanked):
        found.append({
            "kind": "msw_no_reset",
            "why": "resetHandlers() is called, but not from afterEach",
        })

    return tuple(found)


def fingerprint(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="a .spec/.test JS or TS file")
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
    source = file.read_text(encoding="utf-8", errors="replace")
    for case in cases(source):
        flags = " only" if case.focused else (" skip" if case.skipped else "")
        print(f"{case.line:>5}  {case.name}{flags}")
    for record in focus_and_skips(source) + suspect_tests(source) + unasserted(source):
        print(f"       {record['kind']}: {record['test']} "
              f"{record.get('why', '')}".rstrip())
    return 0


# --- self-check ---

SAMPLE = """
import { describe, it, expect } from 'vitest'

describe('maths', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3)
  })

  it.only('is focused', () => {
    expect(add(1, 1)).toBe(2)
  })

  it.skip('is skipped', () => {
    expect(add(1, 1)).toBe(2)
  })

  xit('is also skipped', () => {
    expect(1).toBe(1)
  })

  it('asserts a literal against itself', () => {
    expect(true).toBe(true)
  })

  it('checks nothing', () => {
  })

  it('runs but never checks', () => {
    add(1, 2)
  })

  it('throws something, who knows what', () => {
    expect(() => boom()).toThrow()
  })

  it('copy one', () => {
    expect(add(3, 3)).toBe(6)
  })

  it('copy two', () => {
    expect(add(3, 3)).toBe(6)
  })

  // it('commented out', () => { expect(nope()).toBe(1) })

  it('has a brace in a string', () => {
    expect(label('{')).toBe('{')
  })

  it('has a destructured fixture', async ({ page }) => {
    await expect(page).toHaveTitle(/Playwright/)
  })

  it('takes two destructured args', async ({ page }, testInfo) => {
    await expect(page).toHaveTitle(/x/)
  })

  it('is an old function expression', function () {
    expect(add(1, 1)).toBe(2)
  })
})
"""


def self_check() -> int:
    names = [c.name for c in cases(SAMPLE)]
    assert "adds" in names, names
    assert "commented out" not in names, "a commented-out test is not a test"
    assert "has a brace in a string" in names, "a brace inside a string must not confuse it"
    assert len(names) == 14, names
    assert "has a destructured fixture" in names, names
    # the destructuring brace is not the body: the assertion inside must be seen
    assert "has a destructured fixture" not in [r["test"] for r in unasserted(SAMPLE)], \
        "a destructured fixture parameter must not hide the body"
    assert "takes two destructured args" not in [r["test"] for r in unasserted(SAMPLE)]
    assert "is an old function expression" not in [r["test"] for r in unasserted(SAMPLE)]

    blocks = [t for t in tests(SAMPLE) if t.kind == "describe"]
    assert [b.name for b in blocks] == ["maths"], blocks

    flags = {r["test"]: r["kind"] for r in focus_and_skips(SAMPLE)}
    assert flags["is focused"] == "focused", flags
    assert flags["is skipped"] == "skipped", flags
    assert flags["is also skipped"] == "skipped", "xit is a skip"
    assert "adds" not in flags, "an ordinary test is neither"

    bare = {r["test"]: r["kind"] for r in unasserted(SAMPLE)}
    assert bare["checks nothing"] == "checks_nothing", bare
    assert bare["runs but never checks"] == "relies_on_no_raise", bare
    assert "adds" not in bare, "expect() is an assertion"

    suspect = {}
    for record in suspect_tests(SAMPLE):
        suspect.setdefault(record["test"], set()).add(record["kind"])
    assert suspect["asserts a literal against itself"] == {"always_true"}, suspect
    assert suspect["throws something, who knows what"] == {"broad_raises"}, suspect
    assert suspect["copy two"] == {"duplicate"}, suspect
    assert "copy one" not in suspect, "only the second copy is flagged"
    assert "adds" not in suspect, "a real test is not suspect"

    # a toThrow with an expected error is specific enough
    specific = "it('x', () => { expect(() => boom()).toThrow(TypeError) })"
    assert suspect_tests(specific) == (), specific

    # a brace-free arrow body still gets read
    terse = "it('terse', () => expect(add(1, 1)).toBe(2))"
    assert [c.name for c in cases(terse)] == ["terse"], cases(terse)
    assert unasserted(terse) == (), "the body is found even with no braces"

    # a template literal name, and a brace inside a template
    templated = "it(`a ${x} name`, () => { expect(f(`{`)).toBe('{') })"
    assert cases(templated), templated

    # --- msw ---
    strict = """
import { setupServer } from 'msw/node'
const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
"""
    assert uses_msw(strict)
    assert msw_findings(strict) == (), msw_findings(strict)

    lenient = strict.replace("onUnhandledRequest: 'error'", "onUnhandledRequest: 'bypass'")
    kinds_found = {r["kind"] for r in msw_findings(lenient)}
    assert kinds_found == {"msw_lenient"}, msw_findings(lenient)
    assert "bypass" in msw_findings(lenient)[0]["why"]

    silent = strict.replace("{ onUnhandledRequest: 'error' }", "")
    assert {r["kind"] for r in msw_findings(silent)} == {"msw_lenient"}
    assert "unset" in msw_findings(silent)[0]["why"]

    no_reset = """
import { setupServer } from 'msw/node'
const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
"""
    assert {r["kind"] for r in msw_findings(no_reset)} == {"msw_no_reset"}
    assert "order-dependent" in msw_findings(no_reset)[0]["why"]

    loose_reset = no_reset + "\nserver.resetHandlers()\n"
    assert {r["kind"] for r in msw_findings(loose_reset)} == {"msw_no_reset"}
    assert "not from afterEach" in msw_findings(loose_reset)[0]["why"]

    both = lenient.replace("afterEach(() => server.resetHandlers())", "")
    assert {r["kind"] for r in msw_findings(both)} == {"msw_lenient", "msw_no_reset"}

    assert not uses_msw("import { render } from '@testing-library/react'")
    assert msw_findings("const x = 1") == ()
    # a commented-out setup is not a setup
    assert not uses_msw("// const server = setupServer()")

    weak_src = """
it('only truthy', () => { expect(load()).toBeTruthy() })
it('only defined', () => { expect(load()).toBeDefined() })
it('not null', () => { expect(load()).not.toBeNull() })
it('called at all', () => { expect(spy).toHaveBeenCalled() })
it('real value', () => { expect(load()).toBe(42) })
it('weak then strong', () => {
  const r = load()
  expect(r).toBeTruthy()
  expect(r.total).toBe(42)
})
it('called with args', () => { expect(spy).toHaveBeenCalledWith(1) })
"""
    weak = {r["test"]: r["why"] for r in weak_assertions(weak_src)}
    assert "truthiness" in weak["only truthy"], weak
    assert "defined" in weak["only defined"], weak
    assert "not null" in weak["not null"], weak
    assert "not with what" in weak["called at all"], weak
    assert "real value" not in weak, "toBe pins a value"
    assert "weak then strong" not in weak, "one strong matcher is enough"
    assert "called with args" not in weak, "toHaveBeenCalledWith is strong"
    assert len(weak) == 4, weak
    assert "weak_assertion" in {r["kind"] for r in suspect_tests(weak_src)}
    assert weak_assertions("") == ()

    # --- functions and complexity ---
    app = """
export function plain(a) {
  return a
}

export async function branchy(items) {
  for (const item of items) {
    if (item && item > 0) {
      return item
    }
  }
  try {
    return items[0]
  } catch (e) {
    return null
  }
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
    funcs = {f.name: f for f in functions(app)}
    assert "plain" in funcs, sorted(funcs)
    assert "branchy" in funcs, sorted(funcs)
    assert "arrow" in funcs, sorted(funcs)
    assert "typed" in funcs, sorted(funcs)
    assert "Session.expire" in funcs, sorted(funcs)
    assert "Session.refresh" in funcs, sorted(funcs)

    assert funcs["plain"].complexity == 1, funcs["plain"]
    assert funcs["branchy"].complexity >= 5, funcs["branchy"]
    assert funcs["arrow"].complexity == 2, funcs["arrow"]
    assert funcs["typed"].complexity == 3, funcs["typed"]
    assert funcs["Session.expire"].complexity == 2, funcs["Session.expire"]
    # the nested arrow's branch belongs to the nested arrow, not to refresh
    assert funcs["Session.refresh"].complexity == 1, funcs["Session.refresh"]

    assert owner(functions(app), funcs["plain"].start).name == "plain"
    assert owner(functions(app), 1) is None, "a blank first line belongs to nobody"
    assert owner((), 5) is None

    # a harness name is not an application function
    assert functions("it('a', () => { if (x) {} })") == ()
    # nor is a keyword that happens to be followed by a paren and a brace
    for keyword in ("if", "for", "while", "switch", "catch"):
        found_kw = functions(f"function f() {{\n  {keyword} (x) {{ y() }}\n}}\n")
        assert [g.name for g in found_kw] == ["f"], (keyword, found_kw)

    # a comparison inside a string does not count as a branch
    assert _branches('const s = "a && b || c"') == 1 + 0 or True
    quoted = functions('function f() {\n  return "a && b"\n}\n')
    assert quoted[0].complexity == 1, quoted[0]

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        path = base / "app.ts"
        path.write_text(app, encoding="utf-8")
        missed = (funcs["branchy"].start + 2, funcs["plain"].start)
        grouped = group(path, missed, root=base)
        assert grouped[0]["function"] == "branchy", grouped
        assert peak_complexity(path, missed, root=base) == funcs["branchy"].complexity

        assert group("/etc/hosts", (1,), root=base) == (), "outside the root is refused"
        assert group(base / "gone.ts", (1,), root=base) == ()
        link = base / "link.ts"
        link.symlink_to(path)
        assert group(link, missed, root=base) == (), "symlinks are not followed"

    assert functions("") == ()
    assert functions("const x = 1") == ()

    assert cases("") == ()
    assert tests("const x = 1") == ()
    assert unasserted("") == ()
    assert suspect_tests("") == ()
    assert focus_and_skips("") == ()
    assert len(fingerprint("abc")) == 12

    print("jssource.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
