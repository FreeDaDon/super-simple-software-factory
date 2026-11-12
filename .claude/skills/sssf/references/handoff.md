# Handoff Reference

The envelope schema, the two-channel output contract, and the session directory layout — how context transfers in code, not in conversation.

## Two output channels, exactly

An agent may produce output in two ways and no others:

1. **Reference files** written into `context_handoff/` — plans, notes, artifacts for the agents that follow.
2. **A final valid-JSON response** — the envelope, its direct response and nothing else.

Code does the rest: parse the response against the output type the call declared, persist it as `envelope.json`, and inject it into the next agent's user prompt.

## Envelope schema

Every output type extends `EnvelopeBase`:

```python
class EnvelopeBase(BaseModel):
    status: Literal["success", "fail"]  # the only required field
    summary: str = ""                   # one sentence: what happened
    artifacts: list[str] = []           # paths written, usually inside context_handoff/
    notes_for_next_agent: str = ""      # what the next agent must know
```

`status` is load-bearing: an envelope that parses but reports `status="fail"` raises, failing the phase. An agent declaring its own failure is not a successful phase.

The starter types in `adw_modules/data_types.py`:

```python
class GenericOutput(EnvelopeBase):
    """Fallback for an agent with no sharper contract yet."""

class PlanOutput(EnvelopeBase):
    commit_message: str = ""            # imperative git subject for the PLAN FILE itself
    may_modify: list[GraderEdit] = []   # GraderEdit: {path, reason} — existing tests the build may change

class BuildOutput(EnvelopeBase):
    changed_files: list[str] = []
    commit_message: str = ""            # consumed by the git commit phase

class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = []   # ScoutFinding: {file: str, note: str}

class ReviewOutput(EnvelopeBase):
    approved: bool = False              # the verdict; status is only "did the review run"
    findings: list[ReviewFinding] = []  # ReviewFinding: {requirement, met: bool, evidence}
    blocking: list[str] = []            # what must change before approval

class DocumentOutput(EnvelopeBase):
    document_path: str = ""             # the write-up's home in the repo
    documented_files: list[str] = []
    commit_message: str = ""
```

`commit_message` defaults to empty, so a git phase consuming it always needs a fallback — see `cookbooks/create_adw.md`.

**Each `commit_message` describes its own agent's work product, never the next one's**: `PlanOutput`'s covers the spec file, `BuildOutput`'s the code, `DocumentOutput`'s the write-up. A chain that commits once can use whichever fits; a chain that commits per step (`adw_simple_sdlc.py`) needs all three, and reusing one agent's sentence for another's diff is how a commit log starts lying.

`may_modify` is the plan's declaration of which **existing** grader files (tests, test and build config; `defaults.grader_files`) the build is allowed to change, each with the reason the requested behaviour needs it. Paths are exact and repo-relative; a glob or an absolute path fails validation. It is declared before any code exists, so it cannot be tuned to turn a red suite green, and it is usually empty. The ADW pins it with `run.declare_grader_edits(plan)` into `{data_dir}/runs/{adw_id}.json`, outside anything an agent can write; a joined `adw_build.py` reads it back from there. See `references/config.md`, "The grader lock".

There is no test output type: running the suite is a `kind="code"` phase, and its `QualityResult` reaches the next agent through `quality.as_envelope`.

Two of these are adapters rather than agent reports — code shaped as an envelope so an agent can be handed a deterministic result through the same door: `VerifyOutput` (a lint/test block's result) and `ChangesOutput` (a captured `git diff`, from `changes.as_envelope`). The consuming agent cannot tell the difference, which is the point.

The envelope is a **manifest of claims**. Gates verify those claims after the fact — declared artifacts exist and are non-empty, declared changes appear in the diff, declared tests actually pass. See `cookbooks/update_modules.md`.

## The typed-output rule

**Every agent call passes a concrete output type**, and the agent's final JSON is parsed against exactly that type. No untyped handoffs.

```python
plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                         gates=[gates.artifacts_exist]))
```

The user prompt asks for the shape; the type enforces it. They always travel as a pair, which is what lets one agent serve many calls — same system prompt, different user prompt + output type per call site. Output types live in code, never in `sssf.config.yaml`.

**Parse failure is not a restart.** If the response doesn't parse or doesn't validate, the harness re-prompts the **same session** with a correction naming the required fields — bounded by `JSON_FIX_ATTEMPTS` in `agents.py` (2). Gate violations use the identical mechanism, bounded instead by the phase's `retries` (default 1). A cold restart would throw away the context that produced the near-miss.

There is no separate continue call to make: `agents.execute()` tracks whether the session already exists, so `agent_cc.run()` passes `--session-id` on the first send (which creates the session) and `--resume` on every later one. Running an agent and continuing it are the same call with the same id, and `--session-id` on an existing id is refused by the CLI, which is why the two flags are never mixed. If the recorded state is wrong anyway, the send corrects itself: "already in use" flips it to `--resume`, "No conversation found" flips it back to `--session-id`. A send that hits the subscription usage limit waits for the reset and resumes the same session with a short note, so it is not a restart either. Before parsing, the harness also tolerates a fenced `json` code block or prose wrapped around the object — but the prompt still asks for bare JSON, and every failed attempt is persisted as an invalid envelope row.

## Injecting the previous envelope

`prompts.py` renders the agent's `user.md`, substituting:

| Placeholder | Value |
|---|---|
| `{{prompt}}` | the engineer's ask (or the ADW's per-call prompt) |
| `{{previous_envelope}}` | the upstream envelope JSON, from `AgentCall(previous=...)` |
| `{{context_handoff_dir}}` | absolute path to this session's `context_handoff/` |

A `user.md` declares one h3 per incoming datum, then the task, then the output contract:

````markdown
# Scout Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Find what `prompt` asks about. Write findings into `context_handoff_dir`, then emit your `Report` JSON.

## Report

Respond with ONLY valid JSON matching `ScoutOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence on what you found>",
  "findings": [
    { "file": "src/server.ts", "note": "<why this file matters>" }
  ],
  "artifacts": ["<context_handoff_dir>/scout_findings.md"]
}
```
````

The `## Report` section shows the exact JSON shape of the declared output type — that is the agent's output contract, and it lives in `user.md` because the shape belongs to the *use*, not the identity. The matching `system.md` stays static: Purpose + Instructions only.

## Session directory layout

```
adws/adw_data/runs/{adw_id}.json   the run's pinned state: worktree, branch, baseline commit, may_modify, touched paths
adws/adw_data/sessions/{adw_id}/
├── agent_map.json          agent name → coding-agent session_id + model (+ resolved_model)
├── context_handoff/        the ONE place agents write files for the agents that follow
└── {agent_name}/
    ├── prompts/            exact prompts sent (system.md + user.md), saved before execution
    ├── raw_output.jsonl    full JSONL stream from the coding agent, appended live, secrets redacted
    ├── raw_output.jsonl.stderr   the CLI's stderr, kept out of the stream
    └── envelope.json       the final valid-JSON response — captured, validated, persisted by code
```

`session.ensure(cfg, adw_id)` mints or joins the id, creates (or re-enters) the run's git worktree, and creates these dirs. They live in the operator's checkout, not in the worktree, at absolute paths; the session dir is the one place besides the worktree that the sandbox leaves writable. One `context_handoff/` per session, shared by every agent — the single location for cross-agent files.

## agent_map.json and resuming

```json
{
  "planner": {"session_id": "3f6c0a1e-8b7d-4c52-9a1f-2d5e7b9c4a10",
              "model": "opus", "coding_agent": "claude_code"},
  "builder": {"session_id": "a1659e36-a30c-43dc-92c0-86a0599b5dad",
              "model": "sonnet", "resolved_model": "claude-sonnet-5-5",
              "coding_agent": "claude_code"}
}
```

This map is the key that lets a later ADW rejoin each agent's **existing context window**. Run `adw_build.py --adw-id a1b2c3d4` after `adw_plan.py` and the builder resumes its own session rather than starting cold.

An agent's entry is written as soon as its first send runs, success or not, because the CLI creates the session as soon as it starts: a retry or a rejoined run resumes that context instead of orphaning it. `resolved_model` (the concrete id the alias resolved to) is added once the phase finishes. The map records the model each session was created with. If config drift changes an agent's model, that agent starts a **fresh** session and the map is updated — never a bad resume. `agent_sessions` in `sssf.db` is the queryable mirror of this file.

**Files are the raw record; the db is the queryable mirror.** Losing `sssf.db` loses nothing that can't be rebuilt from `raw_output.jsonl`, `envelope.json`, and `agent_map.json`.
