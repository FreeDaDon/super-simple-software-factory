"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

A snapshot is a git TREE of the whole working state (tracked and untracked,
written through a scratch index), so comparing two snapshots is exact to the
byte, and rolling a path back restores precisely what was there — including
uncommitted work an agent reverted with `git checkout`. Appearing,
disappearing, and changing all count.

Three more holes are closed here:

  * HEAD is pinned per call. Only the harness commits; an agent that runs
    `git commit` would otherwise make its edits vanish from the diff. A moved
    HEAD is reset (the edits stay in the tree and are judged like any other),
    and the move itself is a breach.
  * Gitignored files are invisible to git, so `.env` files are fingerprinted
    on their own (`WATCHED_IGNORED`) and judged against `protected_files`.
  * The grader: a test or test-config file that existed at the run's baseline
    may be modified only if the plan declared it (`PlanOutput.may_modify`),
    and may never be deleted. New tests are always fine.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Keys that drive it, all in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    defaults.grader_files      tests and test config, locked unless the plan declares them
    agents[].writes            None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .data_types import AgentConfig, SSSFConfig

# Ignored files worth watching anyway: credentials live in them.
WATCHED_IGNORED = [":(glob)**/.env", ":(glob)**/.env.*", ":(glob).env", ":(glob).env.*"]


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


@dataclass
class Snapshot:
    head: str                                   # HEAD when the snapshot was taken
    tree: str                                   # git tree of the full working state
    ignored: dict[str, bytes] = field(default_factory=dict)   # watched ignored files


def _git(args: list[str], cwd, env=None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    return result.stdout if result.returncode == 0 else ""


def _git_ok(args: list[str], cwd) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def _working_tree(cwd) -> str:
    """Write the working state (tracked + untracked, minus ignored) as a tree.

    Goes through a scratch copy of the index, so the real index is untouched and
    git's stat cache still spares it re-hashing every file.
    """
    index = Path(cwd) / _git(["rev-parse", "--git-path", "index"], cwd).strip()
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "index"
        if index.exists():
            scratch.write_bytes(index.read_bytes())
        env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        subprocess.run(["git", "add", "-A"], cwd=cwd, env=env, capture_output=True)
        return _git(["write-tree"], cwd, env=env).strip()


def snapshot(run, head: str | None = None) -> Snapshot:
    cwd = run.repo_root
    head = head or _git(["rev-parse", "HEAD"], cwd).strip()
    ignored = {}
    for path in _git(["ls-files", "--others", "--ignored", "--exclude-standard", "--",
                      *WATCHED_IGNORED], cwd).splitlines():
        try:
            ignored[path] = (Path(cwd) / path).read_bytes()
        except OSError:
            pass
    return Snapshot(head=head, tree=_working_tree(cwd), ignored=ignored)


def changed(before: Snapshot, after: Snapshot, cwd) -> list[str]:
    """Every path whose content differs — appeared, vanished, or rewritten."""
    paths = set(_git(["diff-tree", "-r", "--name-only", "--no-renames",
                      before.tree, after.tree], cwd).split("\n"))
    paths |= {p for p in set(before.ignored) | set(after.ignored)
              if before.ignored.get(p) != after.ignored.get(p)}
    return sorted(p for p in paths if p)


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):        # zero or more whole directories
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig, root=None) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    In an isolated run it lives in the operator's checkout, outside the
    worktree, and never appears in a snapshot at all; this grant covers a run
    that is not isolated, where it would. Returned relative to `root`.
    """
    data_dir = Path(cfg.defaults.data_dir)
    if data_dir.is_absolute():
        try:
            data_dir = data_dir.relative_to(Path(root or Path.cwd()).resolve())
        except ValueError:
            return []                      # outside the repo: never in a snapshot
    return [str(data_dir).rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig, root=None) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in always_writable(cfg, root)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def is_grader(path: str, cfg: SSSFConfig) -> bool:
    return any(_matches(path, p) for p in cfg.defaults.grader_files)


def grader_violation(run, path: str) -> str:
    """Why changing this grader file is not allowed, or "" when it is."""
    ws = run.workspace
    if not (ws.baseline and is_grader(path, run.cfg)
            and _git_ok(["cat-file", "-e", f"{ws.baseline}:{path}"], run.repo_root)):
        return ""                        # not a grader file, or new since the baseline
    if not (Path(run.repo_root) / path).exists():
        return "deleted an existing test/grader file"
    if path not in ws.may_modify:
        return ("modified an existing test/grader file the plan did not declare "
                "in may_modify")
    return ""


def _roll_back(run, path: str, before: Snapshot) -> str:
    """Put one path back exactly as it was before the agent ran."""
    cwd = run.repo_root
    target = Path(cwd) / path
    if path in before.ignored:
        target.write_bytes(before.ignored[path])
        return "restored"
    if _git_ok(["cat-file", "-e", f"{before.tree}:{path}"], cwd):
        ok = _git_ok(["restore", f"--source={before.tree}", "--worktree", "--", path], cwd)
        return "restored" if ok else "could not restore"
    try:
        target.unlink()
        return "deleted"
    except FileNotFoundError:
        return "already gone"
    except OSError as error:
        return f"could not delete ({error})"


def enforce(run, phase, agent: AgentConfig, before: Snapshot) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.
    Anything it introduced outside its permissions is rolled back before the
    phase dies; what cannot be undone is named.
    """
    cwd = run.repo_root
    problems: list[str] = []
    head = _git(["rev-parse", "HEAD"], cwd).strip()
    if before.head and head != before.head:
        # Keep the agent's changes in the tree, drop its commits, judge the rest.
        _git(["reset", "-q", before.head], cwd)
        problems.append(f"  - HEAD — moved {before.head[:7]} → {head[:7]}: only the harness "
                        f"commits; the commit(s) were undone and their changes judged below")

    after = snapshot(run, before.head)
    touched = changed(before, after, cwd)
    reasons = {}
    for path in touched:
        why = grader_violation(run, path)
        if why:
            reasons[path] = why
        elif not permitted(path, agent, run.cfg, cwd):
            reasons[path] = "outside this agent's write permission"
    if not (reasons or problems):
        return touched

    for path, why in reasons.items():
        problems.append(f"  - {path} — {why}; {_roll_back(run, path, before)}")
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    raise PermissionBreach(
        f"{agent.name} ({scope}) broke {len(problems)} rule(s):\n" + "\n".join(problems))
