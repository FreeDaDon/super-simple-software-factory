"""Claude Code interface — runs every agent through the operator's own `claude` CLI.

Each agent call is one non-interactive `claude -p --output-format stream-json
--verbose` process whose JSONL stdout is tailed line by line and forwarded to a
callback WHILE the agent works. That is what lets the tracer insert tool calls
into the db live.

Authentication is the operator's Claude subscription (`claude auth login`), not
an API key. `claude_env()` removes ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from
the child's environment, because Claude Code prefers them over the subscription
login and a key left in `.env` would silently turn a subscription run into a
metered API run. `SSSF_ALLOW_API_KEY=1` opts back in.

Sessions: `--session-id <uuid>` CREATES a session and errors if it already
exists; `--resume <uuid>` continues it. A first send creates, every later send
resumes — same session id, same context window. That is the whole mechanism
behind "correction, not cold restart".

Permissions: the agent runs with `--permission-mode dontAsk` plus an explicit
`--allowedTools` list, so anything not on the agent's `tools` list is denied
instead of prompting (there is no one to answer a prompt). `bypassPermissions`
is deliberately not used: it is refused under root, and an allowlist is the
stricter contract anyway. What an agent may CHANGE in the repo is a separate
question, enforced after the fact by permissions.py; what it can REACH on the
machine is bounded by the bubblewrap box in sandbox.py, which wraps the process.

Limits: every send carries `--max-budget-usd` (what is left of the run's
budget; the CLI checks it between turns), and a watchdog kills a process that
goes quiet for `idle_timeout_seconds` or runs past `max_seconds`.

Rate limits: the stream reports the subscription window in `rate_limit_event`.
A call that dies on a rejected window raises RateLimited with the reset time,
so the caller can wait and resume the same session.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from typing import Callable, Optional

from .data_types import AgentRequest, AgentResult, UsageBreakdown
from .utils import now_iso, operator_env, redact

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "Bash: <command>" shown as the event name
STDERR_TAIL_CHARS = 800

# What `--effort` accepts. Replaces the old `thinking` ladder: `off` and
# `minimal` do not exist on Claude Code.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Model aliases Claude Code resolves to the newest model of that family, plus
# full ids. There is no `--list-models`, so a model is validated by SHAPE here
# and proven by the first call. An id that has the right shape but does not
# exist fails that call with the CLI's own error, which is shown verbatim.
MODEL_ALIAS = re.compile(r"^(fable|opus|sonnet|haiku)(\[1m\])?$")
MODEL_FULL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*(\[1m\])?$")

# Claude Code's built-in tool names (`Agent` is the subagent tool, formerly
# `Task`). A name may carry a permission rule — `Bash(git *)` — and an MCP tool
# is `mcp__<server>__<tool>`; both validate on the part before the parenthesis.
BUILTIN_TOOLS = ("Read", "Edit", "Write", "Bash", "Grep", "Glob", "Agent", "Task",
                 "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite")

# What an agent gets when its config sets no `tools` at all.
DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]

# The tool whose permission rule can ride inside the name, e.g. `Bash(git *)`.
_RULE = re.compile(r"^([A-Za-z_][\w]*)(\(.*\))?$")


class RateLimited(RuntimeError):
    """The subscription's usage window is exhausted until `resets_at` (epoch s)."""

    def __init__(self, message: str, resets_at: int):
        super().__init__(message)
        self.resets_at = resets_at


class BudgetExhausted(RuntimeError):
    """The CLI stopped the call at its --max-budget-usd."""


class AgentTimeout(RuntimeError):
    """The watchdog killed a claude process that hung or ran too long."""


def validate_model(pattern: str) -> str:
    """Return the model unchanged when it is a Claude alias or full id."""
    if MODEL_ALIAS.match(pattern) or MODEL_FULL_ID.match(pattern):
        return pattern
    raise ValueError(
        f"model {pattern!r} is not a Claude Code model — use an alias "
        f"(fable, opus, sonnet, haiku) or a full id like claude-sonnet-5-5")


def validate_effort(effort: str) -> str:
    if effort not in EFFORTS:
        raise ValueError(f"effort {effort!r} is not one of {', '.join(EFFORTS)}")
    return effort


def validate_tool(tool: str) -> str:
    match = _RULE.match(tool.strip())
    name = match.group(1) if match else ""
    if name in BUILTIN_TOOLS or name.startswith("mcp__"):
        return tool
    raise ValueError(
        f"tool {tool!r} is not a Claude Code tool — built-ins are "
        f"{', '.join(BUILTIN_TOOLS)} (names are case-sensitive: "
        f"`Read`, not `read`; there is no `ls` or `find`, use `Glob`)")


def claude_env() -> dict[str, str]:
    """The operator's environment, minus anything that would bypass the subscription."""
    env = operator_env()
    if os.environ.get("SSSF_ALLOW_API_KEY") != "1":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
    return env


# ── stream parsing ───────────────────────────────────────────────────────────

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _text_of(content) -> str:
    """Join the text of a message's content, or of a tool_result's content.

    Claude Code shapes both as either a plain string or a list of blocks.
    """
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or []
                   if isinstance(part, dict) and part.get("type") == "text")


# The arg that identifies a call at a glance, in the order tools tend to use.
PRIMARY_ARGS = ("command", "file_path", "path", "pattern", "description",
                "query", "url", "prompt")


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `Bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


class ToolCallTracker:
    """Folds Claude Code's message stream into ONE normalized record per tool call.

    A call is announced by a `tool_use` block inside an `assistant` message and
    completed by a `tool_result` block inside the following `user` message. The
    record is emitted at completion — one trace event per real call, with the
    arguments from the announcement and the result from the completion.

    Messages arrive whole (no partial-message streaming), so `started_at` is
    when the announcing message arrived. That is accurate to the model turn, not
    to the tool's own execution; durations include that turn's overhead. A call
    made by a subagent carries `subagent_of`, the tool_use id of the Agent call
    that spawned it.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> list[dict]:
        """Records for the tool calls this event finished (usually zero or one)."""
        etype = event.get("type", "")
        blocks = (event.get("message") or {}).get("content") or []
        if not isinstance(blocks, list):
            return []
        if etype == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("input"), event.get("parent_tool_use_id"))
            return []
        if etype != "user":
            return []

        records = []
        for block in blocks:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            call_id = str(block.get("tool_use_id") or "")
            opened = self._open.pop(call_id, {})
            tool = str(opened.get("tool") or "tool")
            args = opened.get("args") or {}
            record = {
                "tool": tool,
                "tool_call_id": call_id,
                "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                         for key, value in args.items()},
                "ok": not block.get("is_error", False),
                "label": _label(tool, args),
            }
            result_text = _text_of(block.get("content"))
            if result_text:
                record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
            record["ended_at"] = now_iso()
            if opened.get("clock"):
                record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
            if opened.get("started_at"):
                record["started_at"] = opened["started_at"]
            if opened.get("subagent_of"):
                record["subagent_of"] = opened["subagent_of"]
            records.append(record)
        return records

    def _announce(self, call_id, tool, args, parent) -> None:
        if not call_id:
            return
        self._open[str(call_id)] = {
            "tool": tool or "",
            "args": args if isinstance(args, dict) else {},
            "started_at": now_iso(),          # wall clock, for the row
            "clock": time.monotonic(),        # monotonic, for duration
            "subagent_of": parent or "",
        }


def _usage_breakdown(usage: dict, cost: float) -> UsageBreakdown:
    """One call's spend from the `result` event.

    Claude Code reports cost once per call, not per turn, so component costs
    stay at zero and only `total_cost` is filled. On a subscription that figure
    is the list-price EQUIVALENT of the tokens used — what the same call would
    cost on the API — not a charge. Token counts are exact. `total_tokens` is
    the sum of the four components (cache reads included), the same convention
    the rest of the trace uses.
    """
    breakdown = UsageBreakdown(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        reasoning_tokens=int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
        total_cost=float(cost or 0.0),
    )
    breakdown.total_tokens = (breakdown.input_tokens + breakdown.output_tokens
                              + breakdown.cache_read_tokens + breakdown.cache_write_tokens)
    return breakdown


def _turn_context(usage: dict) -> int:
    """Window occupancy after one assistant turn: the whole prompt plus its reply."""
    return int(sum(usage.get(part) or 0 for part in
                   ("input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens")))


def build_command(request: AgentRequest) -> list[str]:
    """The `claude` argv for one send. The prompt travels on stdin, not here."""
    tools = request.tools or DEFAULT_TOOLS
    cmd = [
        *request.sandbox,                 # bwrap ... -- (empty when unsandboxed)
        CLAUDE_PATH, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", request.model,
        "--effort", request.effort,
        # Create on the first send, continue on every later one.
        *(["--resume", request.session_id] if request.resume
          else ["--session-id", request.session_id]),
        "--append-system-prompt", request.system_prompt,
        "--tools", ",".join(tools),
        "--allowedTools", ",".join(tools),
        "--permission-mode", "dontAsk",
        # "" (the default) loads no settings layer at all: no user hooks or
        # plugins, and no project hooks or permission rules from a worktree an
        # earlier agent could have edited. The repo's CLAUDE.md arrives through
        # the system prompt instead, read from the run's pinned baseline.
        "--setting-sources", request.setting_sources,
        # No MCP servers unless the agent's config names an MCP file.
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]
    if request.max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{request.max_budget_usd:.4f}"]
    for mcp_config in request.mcp_configs:
        cmd += ["--mcp-config", mcp_config]
    return cmd


def run(request: AgentRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> AgentResult:
    """Run one non-interactive Claude Code turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung agent is otherwise a pid you have to hunt
    for in `ps` while the run sits there.
    """
    cmd = build_command(request)
    raw_path = os.path.abspath(request.raw_output_path)
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    stderr_path = raw_path + ".stderr"

    result = AgentResult(session_id=request.session_id)
    stray: list[str] = []        # stdout lines that were not JSON — usually a CLI error
    killed = ""                  # why the watchdog stopped the process, if it did

    # stderr goes to a file, not a pipe: nothing reads a pipe until stdout is
    # exhausted, so a chatty child could fill it and deadlock the whole run.
    with open(stderr_path, "a") as stderr_file:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr_file, text=True, bufsize=1,
                                   cwd=request.cwd, env=claude_env())
        if on_spawn:
            on_spawn(process.pid)
        # The prompt goes on stdin: an argv entry is capped near 128 KB on
        # Linux and a previous_envelope can be large.
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(request.prompt)
        process.stdin.close()

        # A reader thread feeds a queue so the loop below can wait with a
        # timeout: a blocking readline would hang with the agent.
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True).start()
        started = time.monotonic()
        with open(raw_path, "a") as raw:
            while True:
                elapsed = time.monotonic() - started
                wait = min(request.idle_timeout_seconds, request.max_seconds - elapsed)
                try:
                    line = lines.get(timeout=max(wait, 0.01))
                except queue.Empty:
                    killed = (f"no output for {request.idle_timeout_seconds}s"
                              if elapsed + wait < request.max_seconds
                              else f"still running after {request.max_seconds}s")
                    process.kill()
                    break
                if line is None:                 # EOF
                    break
                raw.write(redact(line))
                raw.flush()                      # events land on disk as they happen
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stray.append(line)
                    continue
                _fold(event, result)
                if on_event:
                    on_event(event)

        result.returncode = process.wait()
    if on_exit:
        on_exit(process.pid)

    if killed:
        raise AgentTimeout(f"claude was killed by the watchdog: {killed}")
    detail = _failure_detail(result, stray, stderr_path)
    if result.subtype == "error_max_budget_usd":
        raise BudgetExhausted(f"claude stopped at its budget: {detail}")
    if result.is_error or (result.returncode != 0 and not result.text):
        if result.rate_limit_status == "rejected" or _looks_rate_limited(detail):
            raise RateLimited(f"subscription usage limit reached: {detail}",
                              result.rate_limit_resets_at)
        raise RuntimeError(f"claude failed (exit {result.returncode}): {detail}")
    return result


def _pump(stream, lines: queue.Queue) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


_LIMIT_TEXT = re.compile(r"(usage limit|rate limit|hit your limit|limit reached|"
                         r"resets? (at|in)|5-hour limit)", re.IGNORECASE)


def _looks_rate_limited(detail: str) -> bool:
    return bool(_LIMIT_TEXT.search(detail))


def _fold(event: dict, result: AgentResult) -> None:
    """Fold one stream event into the running result."""
    etype = event.get("type")
    if etype == "assistant":
        message = event.get("message") or {}
        text = _text_of(message.get("content"))
        # Subagent turns (parent_tool_use_id set) are not this agent's window or
        # its final answer — only the main thread counts for either.
        if event.get("parent_tool_use_id"):
            return
        if text:
            result.text = text                   # last assistant message wins
        turn = _turn_context(message.get("usage") or {})
        if turn and message.get("stop_reason") not in ("error",):
            result.context_tokens = turn
    elif etype == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        result.rate_limit_status = str(info.get("status") or "")
        result.rate_limit_resets_at = int(info.get("resetsAt") or 0)
    elif etype == "result":
        result.is_error = bool(event.get("is_error"))
        result.subtype = str(event.get("subtype") or "")
        result.errors = [str(e) for e in event.get("errors") or []]
        final = event.get("result")
        if isinstance(final, str) and final.strip():
            result.text = final                  # the CLI's own statement of the final answer
        result.session_id = event.get("session_id") or result.session_id
        result.cost = float(event.get("total_cost_usd") or 0.0)
        result.usage = _usage_breakdown(event.get("usage") or {}, result.cost)
        result.tokens = result.usage.total_tokens
        models = event.get("modelUsage") or {}
        windows = [int(m.get("contextWindow") or 0) for m in models.values()]
        result.context_window = max(windows, default=0)
        # Several models can appear (subagents); the main one spent the most.
        if models:
            result.resolved_model = max(models, key=lambda m: float(
                (models[m] or {}).get("costUSD") or 0))


def _failure_detail(result: AgentResult, stray: list[str], stderr_path: str) -> str:
    parts = []
    if result.subtype and result.subtype != "success":
        parts.append(result.subtype)
    parts += result.errors
    if result.is_error and result.text:
        parts.append(result.text)
    parts += stray[-3:]
    try:
        with open(stderr_path) as f:
            tail = f.read()[-STDERR_TAIL_CHARS:].strip()
        if tail:
            parts.append(tail)
    except OSError:
        pass
    return " | ".join(p for p in parts if p)[-STDERR_TAIL_CHARS * 2:] or "no output"
