"""Deterministic lint, typecheck, build, and test blocks.

A known command is not a judgement call. Anything whose invocation you can write
down belongs here as code — it runs in milliseconds, costs nothing, and returns
the same answer every time. Agents are for the parts that need reading and
deciding.

╔══════════════════════════════════════════════════════════════════════════════╗
║  REPLACE THE PLACEHOLDER COMMANDS BELOW.                                     ║
║                                                                              ║
║  Every block ships as a placeholder that FAILS (exit 1) and says so. A       ║
║  stamped repo cannot guess your test runner, and a gate that passes without  ║
║  testing anything is worse than no gate. Any ADW that runs a placeholder     ║
║  block refuses to start (see `require`), before it spends anything.          ║
║                                                                              ║
║  For each block you want: swap `_placeholder(...)` for the real argv, e.g.    ║
║      argv=["bun", "test", "apps/web/server.test.ts"]                         ║
║      argv=["uv", "run", "pytest", "-q"]                                      ║
║      argv=["npm", "run", "lint"]                                             ║
║  Delete the blocks you don't need, and drop them from run_quality()'s list.   ║
║                                                                              ║
║  Two rules when you write the real command:                                  ║
║    1. argv LIST, never a shell string — no quoting bugs, no shell injection.  ║
║    2. Call binaries by BARE NAME. These blocks inherit the operator's PATH   ║
║       (see utils.scrubbed_env), so `bun`, `uv`, `pytest` resolve exactly as  ║
║       they do in their terminal. Never hard-code an absolute path like       ║
║       /Users/you/.bun/bin/bun — that bakes your machine into the trace.      ║
║                                                                              ║
║  The command runs code an agent just wrote. So it runs in the run's worktree ║
║  inside the sandbox (sandbox.py), with every credential-looking environment  ║
║  variable removed (utils.scrubbed_env).                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, scrubbed_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000


PLACEHOLDER = "SSSF_PLACEHOLDER"


def _placeholder(name: str) -> list[str]:
    """A command that fails and says why. Replace every call to this."""
    return ["sh", "-c", f"echo '{PLACEHOLDER} {name}: no {name} command is configured. Edit "
                        f"adws/adw_modules/quality.py and replace _placeholder(\"{name}\") "
                        f"in {name}() with the real argv.' >&2; exit 1"]


def is_placeholder(spec: QualityCheckSpec) -> bool:
    return any(PLACEHOLDER in part for part in spec.argv)


def require(*names: str) -> None:
    """Refuse to start an ADW whose quality blocks are still placeholders.

    Called before any agent runs, so an unconfigured repo costs nothing: no
    plan, no build, no tokens spent on a run whose gate could never be real.
    """
    missing = [name for name in names if is_placeholder(SPECS[name]())]
    if missing:
        raise SystemExit(
            f"quality: the {', '.join(missing)} block(s) still run the placeholder command, so "
            f"this ADW has nothing real to verify against. Edit adws/adw_modules/quality.py and "
            f"set the argv in {', '.join(f'{n}_spec()' for n in missing)} — e.g. "
            f'["uv", "run", "pytest", "-q"] or ["bun", "test"] — or drop the block from '
            f"run_quality() if this repo does not have one.")


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = scrubbed_env()             # the operator's environment, minus credentials

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            [*run.box(), *spec.argv],
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A missing binary lands here as exit 127 with the real message — no
        # pre-flight probe needed, and none wanted.
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# Replace every argv below. See the banner at the top of this file. An ADW that
# runs a block calls `require(name)` first, so keep SPECS in step with
# run_quality() when you add or drop one.

def test_spec() -> QualityCheckSpec:
    """The project's test suite. The highest-value block to wire up first."""
    return QualityCheckSpec(
        name="test",
        area="backend",
        operation="build",
        argv=_placeholder("test"),        # e.g. ["bun", "test"] or ["uv", "run", "pytest", "-q"]
        timeout_seconds=600,
    )


def lint_spec() -> QualityCheckSpec:
    return QualityCheckSpec(
        name="lint",
        area="backend",
        operation="lint",
        argv=_placeholder("lint"),        # e.g. ["bun", "x", "oxlint@1.36.0", "src"]
    )


def typecheck_spec() -> QualityCheckSpec:
    return QualityCheckSpec(
        name="typecheck",
        area="backend",
        operation="typecheck",
        argv=_placeholder("typecheck"),   # e.g. ["bun", "x", "tsc", "--noEmit"]
    )


def build_spec() -> QualityCheckSpec:
    return QualityCheckSpec(
        name="build",
        area="backend",
        operation="build",
        argv=_placeholder("build"),       # e.g. ["bun", "build", "src/index.ts", "--outdir", "dist"]
    )


SPECS = {"test": test_spec, "lint": lint_spec, "typecheck": typecheck_spec, "build": build_spec}


def test(run) -> QualityCheckResult:
    return _run(test_spec(), run)


def lint(run) -> QualityCheckResult:
    return _run(lint_spec(), run)


def typecheck(run) -> QualityCheckResult:
    return _run(typecheck_spec(), run)


def build(run) -> QualityCheckResult:
    return _run(build_spec(), run)


def run_tests(run) -> QualityResult:
    """The test suite alone, as a QualityResult — the deterministic test phase.

    This is what replaces a `tester` agent once the command is written down. An
    agent rediscovering the runner on every run costs a fortune to learn what a
    subprocess already knows; the repair loop is unchanged, because a failure
    still reaches the builder through `as_envelope` below.
    """
    check = test(run)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    blocks: list[Callable] = [test, lint, typecheck, build]
    checks = [block(run) for block in blocks]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
