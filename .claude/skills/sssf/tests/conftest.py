"""Shared fixtures. Tests import the TEMPLATE modules — what install.py stamps."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL / "templates"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
sys.path.insert(0, str(TEMPLATES / "adws"))


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fresh repo with one commit holding a source file and an existing test."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "tester")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (root / ".gitignore").write_text(".env\n__pycache__/\nadws/adw_data/sessions/\nadws/adw_data/runs/\nadws/adw_data/sssf.db*\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def stamped(repo):
    """`repo` with the factory stamped in and committed, sandbox off (tests run unboxed)."""
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=repo,
                   check=True, capture_output=True)
    cfg = repo / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    text = cfg.read_text().replace("  sandbox:\n    enabled: true", "  sandbox:\n    enabled: false")
    cfg.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "stamp sssf")
    return repo


def fixture_events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]
