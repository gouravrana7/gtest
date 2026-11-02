#!/usr/bin/env python3
"""Rank uncovered code by risk.

Reads a coverage report (coverage.py JSON or istanbul coverage-final.json),
drops files nobody should be testing, scores what is left, worst first.

    python3 rank.py --coverage coverage.json --top 5
    python3 rank.py --coverage coverage.json --diff main --json
    python3 rank.py --self-check

Never writes files and never generates code. Judgment about which test to
write belongs in SKILL.md, not here.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pysource
except ImportError:  # pragma: no cover - only if the file is moved away
    pysource = None  # the function map is a bonus, not a requirement
try:
    import jssource
except ImportError:  # pragma: no cover - only if the file is moved away
    jssource = None

# which reader can name the functions in a file, by extension
READERS = {
    ".py": "pysource",
    ".js": "jssource", ".jsx": "jssource",
    ".ts": "jssource", ".tsx": "jssource", ".mjs": "jssource",
}

# ponytail: naive additive scoring. Swap for churn x complexity if the
# ordering stops matching what a reviewer would actually pick up first.
WEIGHTS = {
    "recent": 3,      # file touched in the last RECENT_COMMITS commits
    "keyword": 2,     # path contains a risky word
    "branch": 2,      # file has uncovered branches, not just lines
    "ratio": 3,       # maximum points, scaled by share of statements missed
    "complexity": 3,  # maximum points, scaled by the branchiest untested function
}

RECENT_COMMITS = 20

# Complexity at or above this gets the full weight. Twelve branches in one
# function is already well past what a reader holds in their head.
COMPLEXITY_CEILING = 12

RISKY = (
    "auth", "login", "session", "permission", "admin",
    "pay", "billing", "invoice", "money", "price", "refund",
    "token", "secret", "crypt", "password",
    "parse", "serial", "migrat", "upload",
)

SKIP = re.compile(
    r"(^|/)(tests?|__tests__)/"
    r"|(^|/)test_[^/]*$"
    r"|_test\.(py|js|ts|tsx|jsx|go|rb)$"
    r"|(^|/)conftest\.py$"
    r"|(^|/)__init__\.py$"
    r"|(^|/)migrations?/"
    r"|_pb2(_grpc)?\.py$"
    r"|\.(spec|test)\.(js|ts|tsx|jsx)$"
    r"|(^|/)node_modules/"
    r"|(^|/)(dist|build|coverage|\.venv|venv)/"
)


@dataclass(frozen=True)
class Gap:
    """One file's uncovered code. Immutable: adapters build these, nothing edits them."""

    path: str
    missed: tuple[int, ...]
    branches: int
    statements: int


# --- adapters: every report shape converts into Gap, so callers see one shape ---

def detect(data: object) -> str:
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return "coverage.py"
    if isinstance(data, dict) and data:
        first = next(iter(data.values()))
        if isinstance(first, dict) and "statementMap" in first:
            return "istanbul"
    raise ValueError(
        "unrecognised coverage report: expected coverage.py JSON (an object "
        "with 'files') or istanbul coverage-final.json"
    )


def from_coverage_py(data: dict) -> tuple[Gap, ...]:
    gaps = []
    for path, f in sorted(data["files"].items()):
        if not isinstance(f, dict):
            continue
        summary = f.get("summary") or {}
        gaps.append(Gap(
            path=_relative(path),
            missed=tuple(sorted(int(n) for n in (f.get("missing_lines") or ()))),
            branches=len(f.get("missing_branches") or ()),
            statements=int(summary.get("num_statements") or 0),
        ))
    return tuple(gaps)


def from_istanbul(data: dict) -> tuple[Gap, ...]:
    gaps = []
    for key, f in sorted(data.items()):
        if not isinstance(f, dict):
            continue
        stmts = f.get("statementMap") or {}
        hits = f.get("s") or {}
        missed = {
            int(stmts[sid]["start"]["line"])
            for sid, count in hits.items()
            if not count and isinstance(stmts.get(sid), dict)
            and isinstance(stmts[sid].get("start"), dict)
        }
        branches = sum(1 for arm in (f.get("b") or {}).values() if 0 in (arm or []))
        gaps.append(Gap(
            path=_relative(f.get("path") or key),
            missed=tuple(sorted(missed)),
            branches=branches,
            statements=len(stmts),
        ))
    return tuple(gaps)


# Go writes a text profile, not JSON:
#   github.com/me/app/pkg/x.go:10.13,12.2 1 0
#   file:startLine.col,endLine.col numStatements timesRun
GO_BLOCK = re.compile(r"^(?P<file>.+):(?P<start>\d+)\.\d+,(?P<end>\d+)\.\d+ (?P<n>\d+) (?P<hits>\d+)$")


def is_go_profile(text: str) -> bool:
    return text.lstrip().startswith("mode:")


def go_path(path: str, root: Path | None = None) -> str:
    """Turn an import path from a coverprofile into a repo-relative path.

    A profile names files by import path, so github.com/me/app/pkg/x.go has to
    become pkg/x.go. Resolve it against the tree when the file is there.
    """
    parts = path.split("/")
    base = Path(root) if root is not None else Path.cwd()
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if (base / candidate).is_file():
            return candidate
    # ponytail: assume the usual host/owner/repo prefix when the file is not
    # on disk. Good enough to name the file; resolve properly if it misreads.
    if len(parts) > 3 and "." in parts[0]:
        return "/".join(parts[3:])
    return path


def from_go_coverprofile(text: str, root: Path | None = None) -> tuple[Gap, ...]:
    found: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("mode:"):
            continue
        match = GO_BLOCK.match(line)
        if not match:
            continue
        name = go_path(match["file"], root)
        record = found.setdefault(name, {"missed": set(), "statements": 0})
        record["statements"] += int(match["n"])
        if int(match["hits"]) == 0:
            record["missed"].update(range(int(match["start"]), int(match["end"]) + 1))
    return tuple(
        # A coverprofile carries no branch data, so branches stays 0.
        Gap(name, tuple(sorted(record["missed"])), 0, record["statements"])
        for name, record in sorted(found.items())
    )


def load(data: object) -> tuple[Gap, ...]:
    shape = detect(data)
    gaps = from_coverage_py(data) if shape == "coverage.py" else from_istanbul(data)
    return tuple(g for g in gaps if not SKIP.search(g.path))


def load_report(text: str, root: Path | None = None) -> tuple[Gap, ...]:
    """Read any supported report: coverage.py JSON, istanbul JSON, Go profile."""
    if is_go_profile(text):
        gaps = from_go_coverprofile(text, root)
        return tuple(g for g in gaps if not SKIP.search(g.path))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"neither JSON nor a Go coverprofile: {exc}") from exc
    return load(data)


def _relative(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        return path.lstrip("./")


# --- scoring ---

def recent_files(commits: int = RECENT_COMMITS) -> frozenset[str]:
    """Files touched recently. Empty set when git is unavailable — scoring still works."""
    try:
        out = subprocess.run(
            ["git", "log", f"-{commits}", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


def score(gap: Gap, recent: frozenset[str], peak: int = 0) -> tuple[int, tuple[str, ...]]:
    points, reasons = 0, []
    if gap.path in recent:
        points += WEIGHTS["recent"]
        reasons.append("recently changed")
    word = next((w for w in RISKY if w in gap.path.lower()), None)
    if word:
        points += WEIGHTS["keyword"]
        reasons.append(f"risky path ({word})")
    if gap.branches:
        points += WEIGHTS["branch"]
        reasons.append(f"{gap.branches} uncovered branches")
    if gap.statements:
        share = len(gap.missed) / gap.statements
        bump = round(WEIGHTS["ratio"] * min(share, 1.0))
        if bump:
            points += bump
            reasons.append(f"{share:.0%} of statements missed")
    if peak > 1:
        bump = round(WEIGHTS["complexity"] * min(peak, COMPLEXITY_CEILING) / COMPLEXITY_CEILING)
        if bump:
            points += bump
            reasons.append(f"complexity {peak}")
    return points, tuple(reasons)


def owning_functions(gap: Gap, root: Path | None) -> tuple[dict, ...]:
    """Which functions hold the uncovered lines, for the languages we can read."""
    reader_name = READERS.get(Path(gap.path).suffix)
    reader = {"pysource": pysource, "jssource": jssource}.get(reader_name)
    if reader is None:
        return ()
    base = Path(root) if root is not None else Path.cwd()
    return reader.group(base / gap.path, gap.missed, root=base)


def rank(gaps: tuple[Gap, ...], recent: frozenset[str],
         root: Path | None = None) -> tuple[dict, ...]:
    scored = []
    for gap in gaps:
        if not gap.missed:
            continue
        funcs = owning_functions(gap, root)
        peak = max((f["complexity"] for f in funcs), default=0)
        points, reasons = score(gap, recent, peak)
        scored.append({
            "file": gap.path,
            "score": points,
            "reasons": list(reasons),
            "missed_lines": list(gap.missed),
            "ranges": ranges(gap.missed),
            "branches": gap.branches,
            "statements": gap.statements,
            "complexity": peak,
            "functions": [dict(f) for f in funcs],
        })
    return tuple(sorted(scored, key=lambda r: (-r["score"], -len(r["missed_lines"]), r["file"])))


def ranges(lines: tuple[int, ...]) -> str:
    """(1,2,3,7) -> '1-3,7'"""
    if not lines:
        return ""
    out, start, prev = [], lines[0], lines[0]
    for n in lines[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def totals(gaps: tuple[Gap, ...]) -> dict:
    statements = sum(g.statements for g in gaps)
    missed = sum(len(g.missed) for g in gaps)
    return {
        "files": len(gaps),
        "statements": statements,
        "missed": missed,
        "covered_pct": round(100 * (statements - missed) / statements, 1) if statements else 0.0,
        "clean_files": sum(1 for g in gaps if not g.missed),
    }


# --- --diff ---

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# A base ref reaches git as its own argv entry, so there is no shell to
# inject into -- but git itself reads a leading dash as an option, and
# `--upload-pack=...` runs a command. Allow only what a ref can contain.
REF_OK = re.compile(r"^[A-Za-z0-9._/@^~-]{1,200}$")


def parse_diff(text: str) -> dict[str, frozenset[int]]:
    """Added/changed line numbers per file, from `git diff --unified=0` output."""
    changed: dict[str, set[int]] = {}
    path = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            continue
        match = HUNK.match(line)
        if match and path:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            changed.setdefault(path, set()).update(range(start, start + count))
    return {p: frozenset(v) for p, v in changed.items()}


def changed_lines(base: str | None) -> dict[str, frozenset[int]]:
    if base is not None:
        if base.startswith("-") or not REF_OK.match(base):
            raise RuntimeError(f"refusing {base!r} as a git ref")
    cmd = ["git", "diff", "--unified=0"]
    cmd.append(f"{base}...HEAD" if base else "HEAD")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"git diff failed: {exc}") from exc
    return parse_diff(out)


def restrict(gaps: tuple[Gap, ...], changed: dict[str, frozenset[int]]) -> tuple[Gap, ...]:
    """Keep only missed lines the diff actually touched."""
    out = []
    for gap in gaps:
        touched = changed.get(gap.path)
        if not touched:
            continue
        kept = tuple(n for n in gap.missed if n in touched)
        if kept:
            out.append(Gap(gap.path, kept, gap.branches, gap.statements))
    return tuple(out)


# --- output ---

def render(rows: tuple[dict, ...], summary: dict, top: int) -> str:
    head = (
        f"coverage {summary['covered_pct']}%  |  {summary['files']} files  |  "
        f"{summary['missed']} of {summary['statements']} statements missed  |  "
        f"{summary['clean_files']} files fully covered"
    )
    if not rows:
        return head + "\n\nNo gaps worth reporting."
    lines = [head, ""]
    for i, row in enumerate(rows[:top], 1):
        lines.append(f"{i:>2}. [{row['score']:>2}] {row['file']}:{row['ranges']}")
        lines.append(f"       {' | '.join(row['reasons']) or 'no risk signals'}")
        for func in row["functions"][:3]:
            where = ranges(tuple(func["lines"]))
            lines.append(
                f"       -> {func['function']}  (complexity {func['complexity']}, lines {where})"
            )
    if len(rows) > top:
        lines.append(f"\n... and {len(rows) - top} more files with gaps.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coverage", help="coverage.json, coverage-final.json or a Go coverprofile")
    parser.add_argument("--diff", nargs="?", const="", default=None,
                        help="restrict to changed lines; optional base ref (default: uncommitted)")
    parser.add_argument("--top", type=int, default=5, help="how many files to show (default 5)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--self-check", action="store_true", help="run the built-in checks")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    if not args.coverage:
        parser.error("--coverage is required (or pass --self-check)")

    path = Path(args.coverage)
    if not path.is_file():
        print(f"gtest: no coverage report at {path}", file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"gtest: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        gaps = load_report(text, Path.cwd())
    except ValueError as exc:
        print(f"gtest: {exc}", file=sys.stderr)
        return 2

    if not gaps:
        print("gtest: the report has no measurable source files (all skipped or empty).")
        return 0

    summary = totals(gaps)
    scope = gaps
    if args.diff is not None:
        try:
            scope = restrict(gaps, changed_lines(args.diff or None))
        except RuntimeError as exc:
            print(f"gtest: {exc}", file=sys.stderr)
            return 2

    rows = rank(scope, recent_files(), root=Path.cwd())
    if args.json:
        print(json.dumps({"summary": summary, "gaps": list(rows)}, indent=2))
    else:
        print(render(rows, summary, args.top))
    return 0


# --- self-check: the smallest thing that fails if the logic breaks ---

def self_check() -> int:
    cov_py = {
        "meta": {"version": "7.0"},
        "files": {
            "src/auth/session.py": {
                "summary": {"num_statements": 10},
                "missing_lines": [4, 5, 6, 9],
                "missing_branches": [[3, 4], [7, 8]],
            },
            "src/util/strings.py": {
                "summary": {"num_statements": 20},
                "missing_lines": [12],
                "missing_branches": [],
            },
            "tests/test_auth.py": {
                "summary": {"num_statements": 30},
                "missing_lines": [1, 2, 3],
            },
            "src/models/__init__.py": {"summary": {"num_statements": 2}, "missing_lines": [1]},
            "src/clean.py": {"summary": {"num_statements": 5}, "missing_lines": []},
        },
    }

    assert detect(cov_py) == "coverage.py"
    gaps = load(cov_py)
    paths = [g.path for g in gaps]
    assert "tests/test_auth.py" not in paths, "test files must be skipped"
    assert "src/models/__init__.py" not in paths, "__init__.py must be skipped"
    assert paths == ["src/auth/session.py", "src/clean.py", "src/util/strings.py"], paths

    rows = rank(gaps, frozenset({"src/util/strings.py"}))
    assert [r["file"] for r in rows] == ["src/auth/session.py", "src/util/strings.py"], rows
    assert "src/clean.py" not in [r["file"] for r in rows], "fully covered files are not gaps"
    top = rows[0]
    assert top["ranges"] == "4-6,9", top["ranges"]
    assert any("risky path (auth)" == r for r in top["reasons"]), top["reasons"]
    assert any("uncovered branches" in r for r in top["reasons"]), top["reasons"]
    assert any("recently changed" == r for r in rows[1]["reasons"]), rows[1]["reasons"]

    # recency breaks a tie between two otherwise identical gaps
    plain = Gap("src/zzz.py", (1, 2), 0, 10)
    touched = Gap("src/one.py", (1, 2), 0, 10)
    assert rank((plain, touched), frozenset({"src/one.py"}))[0]["file"] == "src/one.py"
    # a bigger gap still wins a tie on points, which is the intended order
    wide = Gap("src/wide.py", tuple(range(1, 10)), 0, 10)
    thin = Gap("src/thin.py", (1,), 0, 10)
    assert rank((thin, wide), frozenset({"src/thin.py"}))[0]["file"] == "src/wide.py"

    summary = totals(gaps)
    assert summary["files"] == 3, summary
    assert summary["missed"] == 5, summary
    assert summary["clean_files"] == 1, summary
    assert summary["covered_pct"] == 85.7, summary

    # empty and degenerate reports must not raise
    assert load({"files": {}}) == ()
    assert totals(())["covered_pct"] == 0.0
    assert rank((), frozenset()) == ()
    assert "No gaps worth reporting" in render((), totals(()), 5)

    istanbul = {
        "/repo/src/pay/checkout.js": {
            "path": "/repo/src/pay/checkout.js",
            "statementMap": {
                "0": {"start": {"line": 3}}, "1": {"start": {"line": 4}},
                "2": {"start": {"line": 9}},
            },
            "s": {"0": 0, "1": 0, "2": 5},
            "b": {"0": [1, 0], "1": [2, 3]},
        }
    }
    assert detect(istanbul) == "istanbul"
    js = from_istanbul(istanbul)
    assert len(js) == 1, js
    assert js[0].missed == (3, 4), js[0]
    assert js[0].branches == 1, js[0]
    assert js[0].statements == 3, js[0]

    try:
        detect({"nonsense": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown report shapes must raise")

    diff = (
        "diff --git a/src/auth/session.py b/src/auth/session.py\n"
        "--- a/src/auth/session.py\n"
        "+++ b/src/auth/session.py\n"
        "@@ -4,0 +5,2 @@\n"
        "+    pass\n"
        "+    pass\n"
        "@@ -20 +22 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/gone.py b/dev/null\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
    )
    changed = parse_diff(diff)
    assert changed["src/auth/session.py"] == frozenset({5, 6, 22}), changed
    assert "/dev/null" not in changed and "gone.py" not in changed, changed

    narrowed = restrict(gaps, {"src/auth/session.py": frozenset({5, 6, 99})})
    assert len(narrowed) == 1, narrowed
    assert narrowed[0].missed == (5, 6), narrowed[0]
    assert restrict(gaps, {}) == (), "a diff touching nothing yields no gaps"

    assert ranges(()) == ""
    assert ranges((7,)) == "7"
    assert ranges((1, 2, 3, 7, 9, 10)) == "1-3,7,9-10"

    for bad in ("--upload-pack=evil", "-x", "a b", "a;b", "$(touch /tmp/x)", "a" * 300):
        try:
            changed_lines(bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be refused as a git ref")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "src").mkdir()
        (base / "src" / "auth.py").write_text(
            "def expire(now):\n"
            "    if now:\n"
            "        return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        rows = rank((Gap("src/auth.py", (2, 3), 0, 4),), frozenset(), root=base)
        assert rows[0]["functions"], rows[0]
        assert rows[0]["functions"][0]["function"] == "expire", rows[0]["functions"]
        assert rows[0]["complexity"] == 2, rows[0]
        assert not any(r.startswith("complexity") for r in rows[0]["reasons"]), \
            "complexity 2 is too low to be worth a point"
        assert "-> expire" in render(rows, totals((Gap("src/auth.py", (2, 3), 0, 4),)), 5)

        # a report naming a path outside the repo must not be read
        # a TypeScript file is mapped the same way
        (base / "src" / "cart.ts").write_text(
            "export function total(items) {\n"
            "  if (!items) { return 0 }\n"
            "  return items.length\n"
            "}\n",
            encoding="utf-8",
        )
        ts_rows = rank((Gap("src/cart.ts", (2, 3), 0, 4),), frozenset(), root=base)
        assert ts_rows[0]["functions"], ts_rows[0]
        assert ts_rows[0]["functions"][0]["function"] == "total", ts_rows[0]["functions"]
        assert ts_rows[0]["complexity"] == 2, ts_rows[0]

        # a language with no reader still ranks, just without function names
        go_rows = rank((Gap("pkg/a.go", (1,), 0, 4),), frozenset(), root=base)
        assert go_rows[0]["functions"] == [], go_rows[0]

        escape = rank((Gap("../outside.py", (1,), 0, 2),), frozenset(), root=base)
        assert escape[0]["functions"] == [], escape
        assert escape[0]["complexity"] == 0, escape

    # a branchy untested function outranks a bigger gap in a flat one
    flat, _ = score(Gap("a.py", (1, 2, 3), 0, 10), frozenset(), peak=0)
    tangled, why = score(Gap("b.py", (1, 2), 0, 10), frozenset(), peak=12)
    assert tangled > flat, (tangled, flat)
    assert any("complexity 12" == r for r in why), why
    # the weight is capped, so a monster function cannot swamp every other signal
    capped, _ = score(Gap("b.py", (1, 2), 0, 10), frozenset(), peak=500)
    assert capped == tangled, (capped, tangled)

    profile = (
        "mode: set\n"
        "github.com/me/app/pkg/auth.go:10.13,12.2 2 0\n"
        "github.com/me/app/pkg/auth.go:14.2,15.3 1 4\n"
        "github.com/me/app/pkg/auth_test.go:5.1,6.2 1 0\n"
        "garbage line that is not a block\n"
    )
    assert is_go_profile(profile) and not is_go_profile("{}")
    go_gaps = from_go_coverprofile(profile, Path("/nowhere"))
    assert len(go_gaps) == 2, go_gaps
    auth = next(g for g in go_gaps if g.path.endswith("auth.go") and "_test" not in g.path)
    assert auth.path == "pkg/auth.go", auth.path
    assert auth.missed == (10, 11, 12), auth.missed
    assert auth.statements == 3, auth.statements
    assert auth.branches == 0, "a coverprofile carries no branch data"
    # the Go test file is dropped by the same skip list as every other language
    assert [g.path for g in load_report(profile, Path("/nowhere"))] == ["pkg/auth.go"]

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "pkg").mkdir()
        (base / "pkg" / "auth.go").write_text("package pkg\n", encoding="utf-8")
        assert go_path("github.com/me/app/pkg/auth.go", base) == "pkg/auth.go"
        assert go_path("weird/auth.go", base) == "weird/auth.go", "unresolvable stays as given"

    try:
        load_report("not json and not a profile")
    except ValueError:
        pass
    else:
        raise AssertionError("an unreadable report must raise")

    print("rank.py self-check: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
