"""Concrete data types for the SSSF ADW system.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationInfo, field_validator

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    # Agent phases: gate-failure corrections in the SAME session. One by default,
    # so every agent gets told what it got wrong once before the phase fails.
    retries: int = 1

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class GraderEdit(BaseModel):
    """One existing grader file (a test, or test/build config) the build must change."""

    path: str                       # exact repo-relative path; no globs
    reason: str                     # why the requested behaviour change needs it

    @field_validator("path")
    @classmethod
    def _exact_path(cls, value: str) -> str:
        value = value.strip().removeprefix("./")
        if not value or any(c in value for c in "*?[") or value.startswith("/"):
            raise ValueError(f"may_modify path {value!r} must be one exact repo-relative "
                             f"file path — no globs, no absolute paths")
        return value


class PlanOutput(EnvelopeBase):
    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""
    # Existing grader files the build is allowed to modify. Declared BEFORE any
    # code is written or any test has run, so it cannot be tuned to make a red
    # suite green. Empty (the normal case) = every existing test is locked.
    may_modify: list[GraderEdit] = Field(default_factory=list)


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)   # what must change before approval


class DocumentOutput(EnvelopeBase):
    """Where the write-up of a completed change landed."""

    document_path: str = ""         # the doc in the repo, e.g. app_docs/<adw_id>_<slug>.md
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]


# ── Config ───────────────────────────────────────────────────────────────────

class PromptEngineering(BaseModel):
    system: str                     # path to system.md
    user: str                       # path to user.md


class AgentConfig(BaseModel):
    name: str
    coding_agent: Literal["claude_code"] = "claude_code"
    model: str = "sonnet"           # alias (fable|opus|sonnet|haiku) or a full claude-* id
    effort: str = "medium"          # low | medium | high | xhigh | max
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    # Paths to MCP config JSON files, passed as `--mcp-config` for this agent only.
    harness_engineering: list[str] = Field(default_factory=list)
    # Claude Code tool names (Read, Edit, Write, Bash, Grep, Glob, Agent, ...).
    # None = the default set. This is the allowlist the agent runs under.
    tools: Optional[list[str]] = None
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see adw_modules/permissions.py). `tools` cannot express this: `bash` runs
    # anything and `write` reaches any path, so an agent's capability list is a
    # statement of intent that nothing checks.
    #   None  -> unrestricted, except the roster-wide `protected_files` paths
    #   []    -> read-only: may modify nothing tracked
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None


class SandboxConfig(BaseModel):
    """bubblewrap isolation for agents and quality commands (Linux only).

    Inside the box the filesystem is read-only, $HOME is an empty tmpfs, /tmp is
    private, and the only writable places are the run's worktree and its session
    dir. Network stays on: claude itself needs the API.
    """

    enabled: bool = True
    # $HOME entries made visible inside the box. Everything else in $HOME —
    # ~/.ssh, ~/.aws, other projects and their .env files — does not exist there.
    home_readonly: list[str] = Field(default_factory=lambda: [
        ".local", ".bun", ".cargo", ".rustup", "go", ".nvm", ".pyenv", ".deno",
        ".gitconfig",
    ])
    # claude keeps its login and session files in ~/.claude and ~/.claude.json,
    # so both must be writable. Package caches make test runs practical.
    home_writable: list[str] = Field(default_factory=lambda: [
        ".claude", ".claude.json", ".cache", ".npm",
    ])


class ConfigDefaults(BaseModel):
    coding_agent: Literal["claude_code"] = "claude_code"
    model: str = "sonnet"
    effort: str = "medium"
    color: str = ""
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # roster-wide allowlist; None = the default tool set
    # Which Claude Code settings layers an agent loads. Empty (the default) loads
    # none: no project hooks or permission rules from the worktree, which an
    # earlier agent in the run could have edited. The repo's CLAUDE.md is still
    # given to every agent — read from the run's pinned baseline commit, not
    # from the worktree. Comma-separated if set: user,project,local.
    setting_sources: str = ""
    # Off-limits to every agent that has not named them in its own `writes`.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "adws/adw_modules/", "adws/adw_sssf_config/", "adws/adw_*.py", "adws/conftest.py",
        ".claude/", ".github/", ".env", "**/.env", ".env.*", "**/.env.*",
    ])
    # The grader: tests and the config that decides how they run. A file here
    # that existed when the run started may only be modified if the plan
    # declared it in `may_modify`, and may never be deleted. New ones are fine:
    # adding a test is what a builder should do.
    grader_files: list[str] = Field(default_factory=lambda: [
        "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "**/test/**",
        "**/__tests__/**", "**/*.test.*", "**/*.spec.*", "**/*_test.go",
        "**/pyproject.toml", "**/setup.cfg", "**/pytest.ini", "**/tox.ini",
        "**/package.json", "**/bunfig.toml", "**/vitest.config.*", "**/jest.config.*",
        "**/justfile", "**/Makefile", "**/Cargo.toml", "**/go.mod",
    ])
    data_dir: str = "adws/adw_data"
    # Where each run's git worktree is created, relative to the checkout. Kept
    # outside it so the operator's own test runs never collect a run's copy.
    worktree_dir: str = "../.sssf-worktrees"
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    # Watchdog for one claude process: killed after this long with no stream
    # event, or after this long in total. A hung agent fails its phase.
    agent_idle_timeout_seconds: int = 900
    agent_max_seconds: int = 3600
    # Ceiling for one run, in list-price-equivalent USD (on a subscription this
    # is a usage measure, not a charge). Passed to every send as
    # --max-budget-usd, and checked between sends.
    max_run_cost_usd: float = 25.0
    # When the subscription's usage window runs out mid-run, wait for the reset
    # and continue the same session. A reset further away than this fails the run.
    rate_limit_max_wait_seconds: int = 6 * 3600


class ObservabilityConfig(BaseModel):
    db: str = "adws/adw_data/sssf.db"
    poll_ms: int = 500


class SSSFConfig(BaseModel):
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Coding agent interface ───────────────────────────────────────────────────

class AgentRequest(BaseModel):
    """Everything one non-interactive `claude -p` send needs."""

    prompt: str
    system_prompt: str
    model: str                      # alias or full claude-* id
    effort: str = "medium"
    session_id: str                 # a UUID: created on the first send, resumed after
    resume: bool = False            # False -> --session-id (create), True -> --resume
    raw_output_path: str            # JSONL stream lands here
    tools: Optional[list[str]] = None
    mcp_configs: list[str] = Field(default_factory=list)
    setting_sources: str = ""
    cwd: str = "."                  # set from run.repo_root — the run's worktree
    sandbox: list[str] = Field(default_factory=list)   # argv prefix (bwrap ...), [] = none
    max_budget_usd: Optional[float] = None
    idle_timeout_seconds: int = 900
    max_seconds: int = 3600


class UsageBreakdown(BaseModel):
    """Tokens (and the cost) of one call, summed over a phase's sends.

    Mirrors the `usage` block of Claude Code's `result` event: `input` EXCLUDES
    cache reads, which are counted on their own — add them to learn the size of
    the prompt that was sent. Claude Code reports ONE cost per call, so only
    `total_cost` is filled; the per-component costs stay zero. On a
    subscription `total_cost` is the list-price equivalent, not a charge.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Thinking tokens. NOT a fifth component: the CLI reports them as
    # `output_tokens_details.thinking_tokens`, a share of `output_tokens`, so
    # the four components above are the whole total. Report it nested under
    # output, never added to it.
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    total_cost: float = 0.0

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        for field in type(self).model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class AgentResult(BaseModel):
    text: str = ""
    returncode: int = 0
    session_id: str = ""
    tokens: int = 0
    cost: float = 0.0               # list-price equivalent on a subscription
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    # Context occupancy after the LAST main-thread turn — not a sum. `tokens`
    # counts every turn; this is how full the window is right now, which is what
    # the visualizer's context bar measures against `context_window`.
    context_tokens: int = 0
    context_window: int = 0         # 0 when the CLI reported no ceiling
    is_error: bool = False          # the CLI's own verdict on the call
    subtype: str = ""               # result subtype, e.g. success | error_max_budget_usd
    errors: list[str] = Field(default_factory=list)
    # The model the alias actually resolved to (from the result's modelUsage),
    # so a run is comparable after `sonnet` starts meaning a newer model.
    resolved_model: str = ""
    # The last subscription rate-limit report the stream carried.
    rate_limit_status: str = ""     # allowed | allowed_warning | rejected
    rate_limit_resets_at: int = 0   # epoch seconds
