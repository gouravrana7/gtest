"""Smoke: the plugin is installable and every script still runs.

If one of these fails the plugin is broken as a plugin, whatever the unit
tests say. They are deliberately shallow and fast.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "coverage-gaps"
SCRIPT_NAMES = ("rank.py", "flaky.py", "pysource.py", "jssource.py",
                "stacks.py", "diagram.py", "mutate.py")


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_every_script_passes_its_own_self_check(script):
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--self-check"],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "all assertions passed" in done.stdout


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_every_script_has_help(script):
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0
    assert "--self-check" in done.stdout


def test_plugin_manifest_is_valid_json_and_named():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "gtest"
    assert manifest["description"]


def test_marketplace_manifest_points_at_this_repo():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "gtest"
    names = [p["name"] for p in market["plugins"]]
    assert "gtest" in names
    assert market["plugins"][0]["source"] == "./"


@pytest.mark.parametrize("name", ["gtest.md", "testsweep.md", "sweep.md"])
def test_every_command_file_exists_with_frontmatter(name):
    text = (ROOT / "commands" / name).read_text()
    assert text.startswith("---")
    assert "description:" in text.split("---")[1]


def test_the_skill_declares_its_name_and_triggers():
    text = (ROOT / "skills" / "coverage-gaps" / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert "name: coverage-gaps" in front
    assert "description:" in front


def test_the_skill_keeps_the_confirmation_gate():
    text = (ROOT / "skills" / "coverage-gaps" / "SKILL.md").read_text().lower()
    assert "never run a test suite without" in text, \
        "the skill must still refuse to run a suite unasked"


def test_the_skill_scans_without_asking():
    text = (ROOT / "skills" / "coverage-gaps" / "SKILL.md").read_text().lower()
    assert "do not ask permission for it" in text, \
        "the read-only scan must not be gated behind a question"


def test_the_skill_triggers_when_a_test_is_written():
    front = (ROOT / "skills" / "coverage-gaps" / "SKILL.md").read_text().split("---")[1]
    assert "writes or changes a test" in front, \
        "the skill should offer itself when the user writes a test"


def test_the_repo_reports_on_itself():
    """gtest run against gtest: the shortest real end-to-end check."""
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "stacks.py"), str(ROOT), "--json"],
        capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["coverage_runners"] == ["pytest"]
    for kind in ("unit", "integration", "smoke"):
        assert report["kinds"][kind]["files"] >= 1, (kind, report["kinds"])
