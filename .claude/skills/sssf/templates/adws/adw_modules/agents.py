"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import agent_cc, permissions, prompts
from .utils import redact
from .data_types import (AgentCall, AgentConfig, AgentRequest, AgentResult,
                         EnvelopeBase, EventRecord, GateCheck, GateReport, Phase,
                         SSSFConfig, UsageBreakdown)

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    # Pydantic ignores unknown keys, so a stale `thinking:` would be dropped
    # without a word and the agent would quietly run at the default effort.
    stale = [where for where, block in
             [("defaults", defaults), *((f"agent {a.get('name')!r}", a)
                                        for a in raw.get("agents", []) or [])]
             if "thinking" in block]
    if stale:
        raise SystemExit(f"config: `thinking` was renamed `effort` (low | medium | high | "
                         f"xhigh | max) — update {', '.join(stale)}")
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "effort", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    return SSSFConfig(**raw)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        for check, value in ((agent_cc.validate_model, agent.model),
                             (agent_cc.validate_effort, agent.effort),
                             *((agent_cc.validate_tool, t) for t in agent.tools or [])):
            try:
                check(value)
            except ValueError as e:
                problems.append(f"agent {name!r}: {e}")
        for mcp in agent.harness_engineering:
            if not Path(mcp).is_file():
                problems.append(f"agent {name!r}: MCP config not found: {mcp}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> claude run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    system_text += _repo_instructions(run)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    session_id, session_exists = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "effort": agent.effort,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME claude session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: AgentResult | None = None
    spent = UsageBreakdown()
    # `--session-id` creates a session and refuses an id that exists; every send
    # after the first (here, or in an earlier phase of a joined run) must resume.
    created = session_exists

    mcp_configs = [str(Path(p).resolve()) for p in agent.harness_engineering]
    defaults = run.cfg.defaults

    def send_once(prompt_text: str) -> AgentResult:
        nonlocal created
        request = AgentRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            effort=agent.effort,
            session_id=session_id,
            resume=created,
            # absolute: read by the claude subprocess, which runs in repo_root
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            mcp_configs=mcp_configs,
            setting_sources=defaults.setting_sources,
            cwd=str(run.repo_root),
            sandbox=run.box(readable=mcp_configs),
            max_budget_usd=run.remaining_budget(),
            idle_timeout_seconds=defaults.agent_idle_timeout_seconds,
            max_seconds=defaults.agent_max_seconds,
        )
        try:
            return agent_cc.run(
                request,
                on_event=_event_forwarder(run, phase, agent.name),
                on_spawn=lambda pid: run.tracer.process_start(
                    run.adw_id, "agent", agent.name, pid,
                    f"claude {agent.name} {agent.model}"),
                on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        finally:
            # The CLI creates the session as soon as it starts, success or not.
            # Record it now, so a retry or a rejoined run resumes this context
            # instead of orphaning it and starting cold.
            if not created:
                created = True
                run.save_agent_map(agent.name, {"session_id": session_id,
                                                "model": agent.model,
                                                "coding_agent": agent.coding_agent})

    def send(prompt_text: str) -> AgentResult:
        nonlocal latest, created
        text = prompt_text
        for _ in range(4):      # a session-mode flip and a rate-limit wait, at most twice each
            resumed = created                       # the mode this attempt uses
            try:
                result = send_once(text)
                break
            except agent_cc.RateLimited as limited:
                _wait_for_reset(run, phase, agent.name, limited)
                text = RESUME_NOTE + prompt_text
            except RuntimeError as error:
                message = str(error)
                if "already in use" in message and not resumed:
                    created = True                  # it exists: resume it
                elif "No conversation found" in message and resumed:
                    created = False                 # it never started: create it
                else:
                    raise
        else:
            raise RuntimeError(f"{agent.name}: gave up after repeated session/rate-limit retries")
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    run.record_touched(touched)
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "resolved_model": context.resolved_model,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "resolved_model": context.resolved_model,
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

RESUME_NOTE = ("(This session was paused by a subscription usage limit and has now "
               "resumed. Continue the task from where you stopped.)\n\n")


def _wait_for_reset(run, phase: Phase, agent_name: str, limited) -> None:
    """Sleep until the usage window resets, or fail if that is too far away."""
    now = time.time()
    resets_at = limited.resets_at or 0
    wait = (resets_at - now + 60) if resets_at > now else 15 * 60   # unknown: check again in 15 min
    ceiling = run.cfg.defaults.rate_limit_max_wait_seconds
    until = datetime.fromtimestamp(now + wait).strftime("%Y-%m-%d %H:%M")
    if wait > ceiling:
        raise RuntimeError(f"{agent_name}: subscription usage limit reached; it resets at "
                           f"{until}, beyond rate_limit_max_wait_seconds ({ceiling}s). "
                           f"Rejoin later with --adw-id {run.adw_id}.") from limited
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="log", name="rate_limited",
                                 payload={"agent": agent_name, "waiting_until": until,
                                          "wait_seconds": int(wait), "detail": str(limited)}))
    run.console.note(f"{agent_name}: subscription usage limit reached — waiting until "
                     f"{until} for the reset, then resuming the same session")
    time.sleep(wait)


def _repo_instructions(run) -> str:
    """The repo's CLAUDE.md, as of the run's pinned baseline commit.

    Settings layers are off by default (`setting_sources: ""`), which also stops
    Claude Code loading CLAUDE.md. The file is still the repo's standing
    instructions, so it is passed in here — from the baseline, never from the
    worktree, where an earlier agent in the same run could have rewritten it.
    """
    ws = run.workspace
    if "project" in run.cfg.defaults.setting_sources or not ws.baseline:
        return ""
    shown = subprocess.run(["git", "show", f"{ws.baseline}:CLAUDE.md"], cwd=ws.home_root,
                           capture_output=True, text=True)
    if shown.returncode != 0 or not shown.stdout.strip():
        return ""
    return ("\n\n# Repository instructions (CLAUDE.md at the run's baseline commit)\n\n"
            + shown.stdout)

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> tuple[str, bool]:
    """(session id, whether that session already exists and must be resumed)."""
    entry = run.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model:
        return entry["session_id"], True     # rejoin the existing context window
    return str(uuid.uuid4()), False          # claude requires a UUID


def _event_forwarder(run, phase: Phase, agent_name: str):
    """One tool_call event per real tool call, with its exact args and result."""
    tracker = agent_cc.ToolCallTracker()

    def forward(event: dict) -> None:
        for record in tracker.observe(event):
            # The call's span rides the columns; duration_ms stays in the payload.
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="tool_call", name=record.pop("label"),
                                         started_at=record.pop("started_at", None),
                                         ended_at=record.pop("ended_at", None),
                                         payload={**record, "agent": agent_name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(
            redact(json.dumps(record, indent=2)))
