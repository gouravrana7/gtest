#!/usr/bin/env python3
"""Turn real gtest output into the terminal picture in the README.

The picture has to be the tool's actual output, not a mock-up of it, or it
stops being true the first time the output changes. So:

    python3 skills/coverage-gaps/stacks.py <a repo> --json > /tmp/s.json
    python3 skills/coverage-gaps/diagram.py --stacks /tmp/s.json > /tmp/out.txt
    python3 docs/make_demo.py /tmp/out.txt docs/demo.svg

Colours are assigned from the text, by the same words the terminal highlights.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

CHAR_W = 7.55          # IBM Plex Mono at 12.5px, measured against the render
LINE_H = 18
PAD_X, PAD_TOP, PAD_BOTTOM = 22, 46, 20

INK = "#d7dce4"
DIM = "#7c8798"
RED = "#e8776a"
GREEN = "#52c7a6"
AMBER = "#e0a44b"
HEAD = "#f2f5f9"

HEADINGS = re.compile(
    r"^(TEST KINDS|COVERED PER FILE|WORST GAPS|FLAKINESS|SLOWEST|QUARANTINE|"
    r"NEVER RUN|FAKED BACKEND|NOT REALLY TESTING|NO EXPLICIT ASSERTION)"
)


def colour(line: str) -> str:
    stripped = line.strip()
    if line.startswith("/gtest") or HEADINGS.match(stripped):
        return HEAD
    if "<- gap" in line or stripped.startswith("No ") and "wants them" in line:
        return RED
    if "focused --" in line or "cannot fail:" in line:
        return RED
    if "n/a" in line and "tests" in line:
        return DIM
    if "Not expected here" in line or stripped.startswith("evidence:"):
        return DIM
    if "%" in line and "█" in line:
        return GREEN
    if "█" in line or "·" in line:
        return AMBER if "<- gap" in line else INK
    if stripped.startswith(("└", "├", "->")):
        return DIM
    return INK


def build(text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    width = max((len(line) for line in lines), default = 40)
    px_w = int(PAD_X * 2 + width * CHAR_W)
    px_h = PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM

    rows = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        y = PAD_TOP + i * LINE_H
        rows.append(
            f'<text x="{PAD_X}" y="{y}" fill="{colour(line)}" '
            f'xml:space="preserve">{html.escape(line)}</text>'
        )

    lights = "".join(
        f'<circle cx="{x}" cy="22" r="5.5" fill="{c}"/>'
        for x, c in ((24, "#e8776a"), (44, "#e0a44b"), (64, "#52c7a6"))
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{px_w}" height="{px_h}" \
viewBox="0 0 {px_w} {px_h}" role="img" aria-label="gtest terminal output">
  <title>gtest output on a sample repository</title>
  <rect width="{px_w}" height="{px_h}" rx="8" fill="#141922"/>
  <rect width="{px_w}" height="38" rx="8" fill="#1d2430"/>
  <rect y="30" width="{px_w}" height="8" fill="#141922"/>
  {lights}
  <text x="86" y="26" fill="{DIM}" font-family="ui-monospace,Menlo,monospace" \
font-size="11.5">gtest</text>
  <g font-family="IBM Plex Mono,ui-monospace,SF Mono,Menlo,Consolas,monospace" \
font-size="12.5">
{chr(10).join("    " + r for r in rows)}
  </g>
</svg>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="a file holding real diagram.py output")
    parser.add_argument("out", help="the .svg to write")
    args = parser.parse_args(argv)

    text = Path(args.source).read_text(encoding="utf-8")
    if "/gtest" not in text:
        print("make_demo: that does not look like diagram.py output", file=sys.stderr)
        return 2
    Path(args.out).write_text(build(text), encoding="utf-8")
    print(f"{args.out}  ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
