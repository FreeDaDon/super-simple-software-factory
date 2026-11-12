"""One git worktree per run: the operator's checkout is never the agents' workspace.

Every run works in `{worktree_dir}/<repo>-<adw_id>` on branch `sssf/<adw_id>`,
cut from the checkout's HEAD. That gives three guarantees at once:

  * the operator's uncommitted work is never swept into an agent commit, and
    never reverted by an agent's `git checkout`,
  * two runs never share a `git status`, a snapshot, or a commit,
  * the base branch is untouched; an accepted run leaves its branch for the
    operator to merge.

The ADW process `chdir`s into the worktree, so every relative path — git,
gates, quality commands — resolves there. Paths that belong to the operator's
checkout (config, prompts, the trace db, session dirs) are made absolute first,
so the trace stays in one place for the visualizer.

What the run decided — its baseline commit and the grader edits its plan
declared — is pinned in `{data_dir}/runs/<adw_id>.json`, outside the session
dir that agents can write. A joined run (`--adw-id`) reuses both.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .data_types import SSSFConfig


@dataclass
class Workspace:
    home_root: str                  # the operator's checkout
    repo_root: str                  # where this run works: the worktree, or home_root
    branch: str = ""                # sssf/<adw_id>, "" when not isolated
    baseline: str = ""              # the commit the run started from
    may_modify: list[str] = field(default_factory=list)   # declared by the plan
    # Paths agents changed (as verified by permissions.enforce) and not yet
    # committed. A commit takes exactly these — never what a test run or a
    # tool left behind, like a fresh lockfile or a cache.
    touched: list[str] = field(default_factory=list)

    @property
    def isolated(self) -> bool:
        return bool(self.branch)


def _git(cwd, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _toplevel(cwd: Path) -> Path | None:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                            capture_output=True, text=True)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def absolutize(cfg: SSSFConfig, base: Path) -> None:
    """Pin every config path to the operator's checkout before the chdir."""
    def pin(path: str) -> str:
        return str((base / path).resolve()) if path and not Path(path).is_absolute() else path

    cfg.defaults.data_dir = pin(cfg.defaults.data_dir)
    cfg.defaults.worktree_dir = pin(cfg.defaults.worktree_dir)
    cfg.observability.db = pin(cfg.observability.db)
    for agent in cfg.agents:
        agent.prompt_engineering.system = pin(agent.prompt_engineering.system)
        agent.prompt_engineering.user = pin(agent.prompt_engineering.user)
        agent.harness_engineering = [pin(p) for p in agent.harness_engineering]


def state_path(cfg: SSSFConfig, adw_id: str) -> Path:
    return Path(cfg.defaults.data_dir) / "runs" / f"{adw_id}.json"


def save(cfg: SSSFConfig, adw_id: str, ws: Workspace) -> None:
    path = state_path(cfg, adw_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(ws), indent=2))


def prepare(cfg: SSSFConfig, adw_id: str) -> Workspace:
    """Create (or rejoin) the run's worktree and move this process into it."""
    cwd = Path.cwd().resolve()
    absolutize(cfg, cwd)
    path = state_path(cfg, adw_id)
    if path.exists():                                   # a joined run
        ws = Workspace(**json.loads(path.read_text()))
        if ws.isolated and not Path(ws.repo_root).is_dir():
            raise SystemExit(f"run {adw_id}: its worktree {ws.repo_root} is gone — "
                             f"start a new run instead of joining this one")
        os.chdir(ws.repo_root)
        return ws

    home = _toplevel(cwd)
    if home is None or not _git(home, "rev-parse", "--verify", "-q", "HEAD", check=False):
        # Not a repo, or no commit to branch from: run in place. Read-only ADWs
        # work; a commit phase will refuse with its own message.
        ws = Workspace(home_root=str(home or cwd), repo_root=str(home or cwd))
        save(cfg, adw_id, ws)
        return ws

    branch = f"sssf/{adw_id}"
    # Outside the checkout on purpose: a worktree nested inside it would be
    # collected by the operator's own `pytest` / `bun test` runs.
    tree = Path(cfg.defaults.worktree_dir) / f"{home.name}-{adw_id}"
    baseline = _git(home, "rev-parse", "HEAD")
    _git(home, "worktree", "add", "-q", "-b", branch, str(tree), baseline)
    ws = Workspace(home_root=str(home), repo_root=str(tree.resolve()),
                   branch=branch, baseline=baseline)
    save(cfg, adw_id, ws)
    if _git(home, "status", "--porcelain", check=False):
        print(f"note: your checkout has uncommitted changes. They are NOT part of run "
              f"{adw_id}, which works on {branch} from {baseline[:7]}.")
    os.chdir(ws.repo_root)
    return ws


def handoff_commands(ws: Workspace) -> list[str]:
    """What the operator runs to take an isolated run's work, or throw it away."""
    if not ws.isolated:
        return []
    return [f"git -C {ws.home_root} log --oneline {ws.baseline[:7]}..{ws.branch}",
            f"git -C {ws.home_root} merge {ws.branch}",
            f"git -C {ws.home_root} worktree remove {ws.repo_root} && "
            f"git -C {ws.home_root} branch -D {ws.branch}"]
