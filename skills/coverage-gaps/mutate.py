#!/usr/bin/env python3
"""Propose mutants: small wrongnesses your tests should notice.

Coverage says a line ran. It cannot say the line was checked. Mutation
testing answers that: break the code on purpose, run the suite, and see
whether anything fails. A mutant that survives is a line your tests execute
and do not verify.

    python3 mutate.py --file src/auth.py            # propose, change nothing
    python3 mutate.py --file src/auth.py --apply 0  # write mutant 0 to disk
    python3 mutate.py --file src/auth.py --restore  # put the file back
    python3 mutate.py --self-check

This script never runs a test suite -- that stays the caller's decision, and
each mutant costs one full run, which is why the default is two.

Operators are found with `tokenize`, so a `>` inside a string or a comment is
never touched.
"""
from __future__ import annotations

import argparse
import ast
import io
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT = 2

# Each swap is a mistake a person actually makes. An off-by-one in a
# comparison and an inverted boolean are the two that slip through review.
OP_SWAPS: dict[str, tuple[str, str]] = {
    ">=": (">", "an off-by-one at the boundary"),
    "<=": ("<", "an off-by-one at the boundary"),
    ">": (">=", "an off-by-one at the boundary"),
    "<": ("<=", "an off-by-one at the boundary"),
    "==": ("!=", "an inverted equality"),
    "!=": ("==", "an inverted equality"),
    "+": ("-", "the wrong arithmetic"),
    "-": ("+", "the wrong arithmetic"),
}

NAME_SWAPS: dict[str, tuple[str, str]] = {
    "and": ("or", "a loosened condition"),
    "or": ("and", "a tightened condition"),
    "True": ("False", "an inverted constant"),
    "False": ("True", "an inverted constant"),
}

# Lines like these are not worth mutating: the change is not a behaviour
# change a test could reasonably catch.
SKIP_LINE_HINTS = ("import ", "from ", "__version__", "# pragma")


@dataclass(frozen=True)
class Mutant:
    index: int
    line: int
    column: int
    was: str
    now: str
    describes: str
    source_line: str

    def summary(self) -> str:
        return (f"[{self.index}] line {self.line}: {self.was} -> {self.now}"
                f"   ({self.describes})")


def _swap(token: tokenize.TokenInfo) -> tuple[str, str] | None:
    if token.type == tokenize.OP:
        return OP_SWAPS.get(token.string)
    if token.type == tokenize.NAME:
        return NAME_SWAPS.get(token.string)
    return None


def mutants(source: str) -> tuple[Mutant, ...]:
    """Every valid single-token mutation of this source.

    A candidate is kept only if the result still parses, so a mutant can
    never turn a test failure into a syntax error.
    """
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ()

    found: list[Mutant] = []
    for token in tokens:
        swap = _swap(token)
        if swap is None:
            continue
        row, col = token.start
        if row - 1 >= len(lines):
            continue
        text = lines[row - 1]
        if any(hint in text for hint in SKIP_LINE_HINTS):
            continue
        replacement, describes = swap
        mutated = _rewrite(lines, row, col, token.string, replacement)
        if mutated is None:
            continue
        found.append(Mutant(
            index=len(found),
            line=row,
            column=col,
            was=token.string,
            now=replacement,
            describes=describes,
            source_line=text.rstrip("\n"),
        ))
    return tuple(found)


def _rewrite(lines: list[str], row: int, col: int, was: str, now: str) -> str | None:
    """The whole source with one token swapped, or None if it stops parsing."""
    text = lines[row - 1]
    if text[col:col + len(was)] != was:
        return None
    changed = list(lines)
    changed[row - 1] = text[:col] + now + text[col + len(was):]
    candidate = "".join(changed)
    try:
        ast.parse(candidate)
    except (SyntaxError, ValueError):
        return None
    return candidate


def apply(path: str | Path, index: int) -> str:
    """Write one mutant to disk. Returns the mutant's summary.

    Refuses unless the file is tracked by git and unmodified, so `restore`
    can always put it back exactly.
    """
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"no such file: {file}")
    state = git_state(file)
    if state != "clean":
        raise ValueError(
            f"refusing to mutate {file}: it is {state}. Mutating edits the file "
            "in place and restore uses git, so commit or stash first."
        )

    source = file.read_text(encoding="utf-8")
    options = mutants(source)
    if not options:
        raise ValueError(f"no mutants available in {file}")
    if not 0 <= index < len(options):
        raise ValueError(f"mutant {index} does not exist; there are {len(options)}")

    chosen = options[index]
    lines = source.splitlines(keepends=True)
    mutated = _rewrite(lines, chosen.line, chosen.column, chosen.was, chosen.now)
    if mutated is None:  # pragma: no cover - mutants() already validated it
        raise ValueError("the mutant no longer applies to this file")
    file.write_text(mutated, encoding="utf-8")

    # Verify what landed. If more than the one intended line differs, put the
    # file back at once rather than leaving it in a state nobody asked for.
    written = file.read_text(encoding="utf-8")
    if not _only_one_line_differs(source, written):
        file.write_text(source, encoding="utf-8")
        raise ValueError(
            f"the write to {file} changed more than one line; it has been put back"
        )
    return chosen.summary()


def _only_one_line_differs(before: str, after: str) -> bool:
    old_lines, new_lines = before.splitlines(), after.splitlines()
    if len(old_lines) != len(new_lines):
        return False
    differing = sum(1 for a, b in zip(old_lines, new_lines) if a != b)
    return differing == 1


def git_state(path: str | Path) -> str:
    """clean, modified, untracked, or unavailable."""
    file = Path(path)
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain", "--", str(file)],
            capture_output=True, text=True, timeout=30, cwd=file.parent,
        )
    except (subprocess.SubprocessError, OSError):
        return "unavailable"
    if done.returncode != 0:
        return "unavailable"
    out = done.stdout.strip()
    if not out:
        return "clean"
    return "untracked" if out.startswith("??") else "modified"


def restore(path: str | Path) -> None:
    """Put the file back to its committed contents."""
    file = Path(path)
    done = subprocess.run(
        ["git", "checkout", "--", str(file)],
        capture_output=True, text=True, timeout=30, cwd=file.parent,
    )
    if done.returncode != 0:
        raise ValueError(f"could not restore {file}: {done.stderr.strip()}")


def render(found: tuple[Mutant, ...], limit: int) -> str:
    if not found:
        return "No mutants available here."
    lines = [
        f"{len(found)} mutant(s) available; the first {min(limit, len(found))} "
        f"would cost one test run each.",
        "",
        "A mutant that survives is a line your tests run and do not check.",
        "",
    ]
    for mutant in found[:limit]:
        lines.append(f" {mutant.summary()}")
        lines.append(f"      {mutant.source_line.strip()[:80]}")
    if len(found) > limit:
        lines.append("")
        lines.append(f" ... {len(found) - limit} more; raise --limit to see them.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", help="the source file to mutate")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"how many mutants to propose (default {DEFAULT_LIMIT})")
    parser.add_argument("--apply", type=int, metavar="N",
                        help="write mutant N to the file, in place")
    parser.add_argument("--restore", action="store_true",
                        help="put the file back to its committed contents")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.file:
        parser.error("--file is required (or pass --self-check)")

    file = Path(args.file)
    try:
        if args.restore:
            restore(file)
            print(f"restored {file}")
            return 0
        if args.apply is not None:
            print(apply(file, args.apply))
            print(f"run the suite now, then: mutate.py --file {file} --restore")
            return 0
    except ValueError as exc:
        print(f"gtest: {exc}", file=sys.stderr)
        return 2

    if not file.is_file():
        print(f"gtest: no such file: {file}", file=sys.stderr)
        return 2
    found = mutants(file.read_text(encoding="utf-8", errors="replace"))
    if args.json:
        import json
        print(json.dumps({
            "file": str(file),
            "available": len(found),
            "limit": args.limit,
            "mutants": [vars(m) for m in found[:args.limit]],
        }, indent=2))
    else:
        print(render(found, args.limit))
    return 0


# --- self-check ---

SAMPLE = '''import os

MAX = 10


def allowed(age, member):
    if age >= 18 and member:
        return True
    return False


def label(kind):
    # a > b in a comment must not be mutated
    note = "a >= b in a string must not be mutated either"
    return note if kind == "note" else "other"
'''


def self_check() -> int:
    found = mutants(SAMPLE)
    assert found, "there is plenty to mutate here"

    swaps = {(m.was, m.now) for m in found}
    assert (">=", ">") in swaps, swaps
    assert ("and", "or") in swaps, swaps
    assert ("True", "False") in swaps, swaps
    assert ("False", "True") in swaps, swaps
    assert ("==", "!=") in swaps, swaps

    # a comparison inside a comment or a string is not code
    comment_line = next(
        (m for m in found if "comment" in m.source_line), None
    )
    assert comment_line is None, comment_line
    string_line = next((m for m in found if "in a string" in m.source_line), None)
    assert string_line is None, string_line

    # the import line is skipped
    assert all("import" not in m.source_line for m in found), found

    # every mutant still parses, and changes exactly one token
    lines = SAMPLE.splitlines(keepends=True)
    for mutant in found:
        mutated = _rewrite(lines, mutant.line, mutant.column, mutant.was, mutant.now)
        assert mutated is not None, mutant
        ast.parse(mutated)
        assert mutated != SAMPLE
        assert len(mutated.splitlines()) == len(SAMPLE.splitlines())

    assert [m.index for m in found] == list(range(len(found))), "indexes are stable"

    text = render(found, 2)
    assert "would cost one test run each" in text
    assert "run and do not check" in text
    assert text.count("[0]") == 1 and text.count("[1]") == 1
    assert "more; raise --limit" in text, "the rest are offered, not hidden"
    assert render((), 2) == "No mutants available here."
    assert "[2]" not in render(found, 2), "the limit is honoured"

    assert _only_one_line_differs("a\nb\n", "a\nc\n")
    assert not _only_one_line_differs("a\nb\n", "x\ny\n"), "two lines is too many"
    assert not _only_one_line_differs("a\nb\n", "a\nb\nc\n"), "a new line is too many"
    assert not _only_one_line_differs("a\nb\n", "a\nb\n"), "no change is not a mutant"

    assert mutants("") == ()
    assert mutants("def broken(:\n") == (), "unparseable source yields no mutants"

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.py"
        path.write_text(SAMPLE, encoding="utf-8")

        # outside a repository, mutating is refused rather than risked
        assert git_state(path) in ("unavailable", "untracked"), git_state(path)
        try:
            apply(path, 0)
        except ValueError as exc:
            assert "refusing to mutate" in str(exc), exc
        else:
            raise AssertionError("a file git cannot restore must not be mutated")
        assert path.read_text(encoding="utf-8") == SAMPLE, "and it must be untouched"

        try:
            apply(Path(tmp) / "missing.py", 0)
        except ValueError as exc:
            assert "no such file" in str(exc), exc
        else:
            raise AssertionError("a missing file must raise")

    print("mutate.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
