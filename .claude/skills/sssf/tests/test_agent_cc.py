"""The claude adapter: stream folding, argv, env, watchdog, limits, redaction."""

import sys
import textwrap

import pytest
from conftest import FAKE_CLAUDE, fixture_events

from adw_modules import agent_cc
from adw_modules.data_types import AgentRequest, AgentResult


def fold(name):
    result = AgentResult()
    for event in fixture_events(name):
        agent_cc._fold(event, result)
    return result


def test_fold_real_scout_stream():
    result = fold("scout_simple_bash_tools.jsonl")
    assert result.text and not result.is_error
    assert result.context_window > 0 and result.context_tokens > 0
    assert result.resolved_model.startswith("claude-")
    assert result.rate_limit_status == "allowed" and result.rate_limit_resets_at > 0
    assert result.usage.total_tokens == (result.usage.input_tokens + result.usage.output_tokens
                                         + result.usage.cache_read_tokens
                                         + result.usage.cache_write_tokens)


def test_result_without_model_usage_has_no_window():
    result = AgentResult()
    agent_cc._fold({"type": "result", "result": "x", "usage": {}}, result)
    assert result.context_window == 0 and result.resolved_model == ""


def test_subagent_turns_do_not_become_the_answer():
    events = fixture_events("planner_agent_subagent.jsonl")
    assert any(e.get("parent_tool_use_id") for e in events)
    result = AgentResult()
    for event in events:
        if event.get("type") == "result":
            continue
        agent_cc._fold(event, result)
    last_main = [e for e in events if e.get("type") == "assistant"
                 and not e.get("parent_tool_use_id")
                 and agent_cc._text_of(e["message"].get("content"))][-1]
    assert result.text == agent_cc._text_of(last_main["message"]["content"])


def test_tool_call_tracker_pairs_calls_and_marks_subagents():
    tracker = agent_cc.ToolCallTracker()
    records = [r for e in fixture_events("planner_agent_subagent.jsonl") for r in tracker.observe(e)]
    assert records, "no tool calls folded"
    assert all(r["tool_call_id"] and r["label"] for r in records)
    assert any(r.get("subagent_of") for r in records)
    assert any(r["tool"] == "Agent" for r in records)


def request(tmp_path, **kw):
    base = dict(prompt="hi", system_prompt="sys", model="haiku", effort="low",
                session_id="11111111-1111-1111-1111-111111111111",
                raw_output_path=str(tmp_path / "raw.jsonl"), cwd=str(tmp_path))
    return AgentRequest(**{**base, **kw})


def test_build_command_create_then_resume(tmp_path):
    first = agent_cc.build_command(request(tmp_path))
    assert "--session-id" in first and "--resume" not in first
    later = agent_cc.build_command(request(tmp_path, resume=True))
    assert "--resume" in later and "--session-id" not in later
    assert first[first.index("--setting-sources") + 1] == ""
    assert first[first.index("--permission-mode") + 1] == "dontAsk"


def test_build_command_sandbox_and_budget(tmp_path):
    cmd = agent_cc.build_command(request(tmp_path, sandbox=["bwrap", "--", ],
                                         max_budget_usd=1.5, tools=["Read"]))
    assert cmd[:2] == ["bwrap", "--"] and cmd[2] == agent_cc.CLAUDE_PATH
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5000"
    assert cmd[cmd.index("--tools") + 1] == "Read"


@pytest.mark.parametrize("model", ["opus", "sonnet[1m]", "haiku", "claude-sonnet-5-5"])
def test_model_shapes_accepted(model):
    assert agent_cc.validate_model(model) == model


@pytest.mark.parametrize("bad", ["gpt-5", "openrouter/x", "Opus", "pi:sonnet"])
def test_model_shapes_rejected(bad):
    with pytest.raises(ValueError):
        agent_cc.validate_model(bad)


def test_tools_and_effort_validation():
    assert agent_cc.validate_tool("Bash(git *)")
    assert agent_cc.validate_tool("mcp__x__y")
    with pytest.raises(ValueError):
        agent_cc.validate_tool("read")
    with pytest.raises(ValueError):
        agent_cc.validate_effort("minimal")


def test_claude_env_strips_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bogus")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bogus")
    monkeypatch.delenv("SSSF_ALLOW_API_KEY", raising=False)
    env = agent_cc.claude_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    monkeypatch.setenv("SSSF_ALLOW_API_KEY", "1")
    assert agent_cc.claude_env()["ANTHROPIC_API_KEY"] == "sk-ant-bogus"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))


def test_run_round_trip_with_fake_cli(tmp_path, fake):
    result = agent_cc.run(request(tmp_path))
    assert '"status": "success"' in result.text and result.cost == 0.001
    assert result.resolved_model == "claude-haiku-5-5"


def test_watchdog_kills_a_hung_agent(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    with pytest.raises(agent_cc.AgentTimeout, match="no output for 1s"):
        agent_cc.run(request(tmp_path, idle_timeout_seconds=1))


def test_wall_clock_cap(tmp_path, monkeypatch):
    chatty = tmp_path / "chatty.py"
    chatty.write_text(textwrap.dedent("""\
        import json, sys, time
        sys.stdin.read()
        while True:
            print(json.dumps({"type": "system"}), flush=True)
            time.sleep(0.2)
        """))
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", sys.executable)
    req = request(tmp_path, idle_timeout_seconds=5, max_seconds=1)
    original = agent_cc.build_command
    monkeypatch.setattr(agent_cc, "build_command", lambda r: [sys.executable, str(chatty)])
    with pytest.raises(agent_cc.AgentTimeout, match="still running after 1s"):
        agent_cc.run(req)
    monkeypatch.setattr(agent_cc, "build_command", original)


def test_rejected_window_raises_rate_limited(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ratelimit")
    with pytest.raises(agent_cc.RateLimited) as info:
        agent_cc.run(request(tmp_path))
    assert info.value.resets_at > 0


def test_budget_stop_is_distinct(tmp_path, monkeypatch):
    script = tmp_path / "budget.py"
    script.write_text(
        "import json,sys\nsys.stdin.read()\n"
        "print(json.dumps({'type':'result','subtype':'error_max_budget_usd','is_error':True,"
        "'errors':['Reached maximum budget ($0.01)'],'usage':{}}))\n")
    monkeypatch.setattr(agent_cc, "build_command", lambda r: [sys.executable, str(script)])
    with pytest.raises(agent_cc.BudgetExhausted, match="maximum budget"):
        agent_cc.run(request(tmp_path))


def test_raw_output_is_redacted(tmp_path, monkeypatch):
    script = tmp_path / "leak.py"
    script.write_text(
        "import json,sys\nsys.stdin.read()\n"
        "print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result',"
        "'tool_use_id':'t1','content':'ANTHROPIC_API_KEY=sk-ant-api03-secretsecretsecret'}]}}))\n"
        "print(json.dumps({'type':'result','result':'{}','usage':{}}))\n")
    monkeypatch.setattr(agent_cc, "build_command", lambda r: [sys.executable, str(script)])
    agent_cc.run(request(tmp_path))
    raw = (tmp_path / "raw.jsonl").read_text()
    assert "secretsecret" not in raw and "[REDACTED]" in raw
