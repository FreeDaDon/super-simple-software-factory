"""Quality blocks fail closed, run with credentials scrubbed, inside the sandbox; the trace is redacted."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from adw_modules import agents, quality, sandbox, session
from adw_modules.data_types import PhaseParams, QualityCheckSpec, SandboxConfig

CONFIG = "adws/adw_sssf_config/sssf.config.yaml"


@pytest.fixture
def run(stamped):
    return session.ensure(agents.load_config(CONFIG), "qrun0001")


def code_phase(run):
    return run.phase(PhaseParams(name="check", kind="code", owner="quality",
                                 description="Run one quality command under test"))


def test_placeholder_fails_closed(run):
    with code_phase(run):
        check = quality.test(run)
    assert not check.passed and check.returncode == 1
    assert "no test command is configured" in check.output_tail


def test_require_names_every_placeholder():
    with pytest.raises(SystemExit, match="test, lint"):
        quality.require("test", "lint")


def test_quality_command_does_not_see_credentials(run, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "leakme")
    monkeypatch.setenv("GITHUB_TOKEN", "leakme2")
    monkeypatch.setenv("HARMLESS", "fine")
    spec = QualityCheckSpec(name="env", area="backend", operation="build", argv=["env"])
    with code_phase(run):
        check = quality._run(spec, run)
    assert "HARMLESS=fine" in check.output_tail
    assert "leakme" not in check.output_tail


def test_tracer_redacts_secrets_in_db_and_jsonl(run):
    from adw_modules.data_types import EventRecord
    run.tracer.event(EventRecord(adw_id=run.adw_id, type="tool_call", name="Read: .env",
                                 payload={"result_snippet": "ANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzzzzzz\n"
                                                            "AWS=AKIAABCDEFGHIJKLMNOP"}))
    stored = run.tracer.conn.execute(
        "SELECT payload_json FROM events WHERE name='Read: .env'").fetchone()[0]
    jsonl = run.tracer.events_jsonl.read_text()
    for text in (stored, jsonl):
        assert "zzzzzzzz" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    json.loads(stored)


needs_bwrap = pytest.mark.skipif(not sandbox.available(), reason="bubblewrap not available")


@needs_bwrap
def test_sandbox_hides_home_and_freezes_the_filesystem(tmp_path):
    home_root = Path.home() / "projects"            # stands in for an operator checkout
    work = tmp_path / "wt"
    work.mkdir()
    box = sandbox.prefix(SandboxConfig(), workdir=work, home_root=home_root)
    script = ("ls ~/.ssh >/dev/null 2>&1 && echo SSH_VISIBLE; "
              "touch /etc/sssf_probe 2>/dev/null && echo ETC_WRITABLE; "
              "touch inside.txt && echo INSIDE_OK; "
              f"ls {home_root} >/dev/null 2>&1 && ls {home_root} | head -1 | grep -q . && echo CHECKOUT_VISIBLE; "
              "command -v claude >/dev/null && echo CLAUDE_FOUND")
    out = subprocess.run([*box, "sh", "-c", script], capture_output=True, text=True,
                         env={**os.environ}).stdout
    assert "INSIDE_OK" in out and (work / "inside.txt").exists()
    assert "SSH_VISIBLE" not in out and "ETC_WRITABLE" not in out
    assert "CHECKOUT_VISIBLE" not in out
    assert not Path("/etc/sssf_probe").exists()


@needs_bwrap
def test_quality_runs_inside_the_box(stamped):
    cfg = agents.load_config(CONFIG)
    cfg.defaults.sandbox.enabled = True
    run = session.ensure(cfg, "qbox0001")
    spec = QualityCheckSpec(name="probe", area="backend", operation="build",
                            argv=["sh", "-c", f"touch {stamped}/escaped.txt; touch here.txt"])
    with code_phase(run):
        quality._run(spec, run)
    assert not (stamped / "escaped.txt").exists()          # the checkout is not writable
    assert (run.repo_root / "here.txt").exists()             # the worktree is


def test_sandbox_off_is_empty():
    assert sandbox.prefix(SandboxConfig(enabled=False), workdir=Path("."), home_root=Path(".")) == []
