"""bubblewrap isolation for everything that runs agent-influenced code.

Two things run code an agent controls: the agent itself (Bash), and the quality
commands that execute the tests it wrote. Both run inside the same box:

  * the whole filesystem is mounted read-only,
  * $HOME is an empty tmpfs with only an allowlist mapped back in
    (`defaults.sandbox.home_readonly` / `home_writable`), so ~/.ssh, ~/.aws,
    other projects and their .env files do not exist inside,
  * the operator's checkout is hidden the same way; only the run's worktree
    (rw), the repo's .git (ro) and the run's session dir (rw) are visible,
  * /tmp is private, the PID namespace is private, and the box dies with
    its parent.

Network stays on: claude needs the API, and test suites often fetch packages.
Agents run as root on many VPSes, so a read-only root filesystem is what stops
`rm -rf /` or an edit to /etc — the post-hoc diff in permissions.py only sees
the repo.

Linux only. Where bwrap is missing or cannot run, the box is skipped with a loud
warning; the worktree and permissions.py still apply.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path

from .data_types import SandboxConfig

_warned = False


@functools.cache
def available() -> bool:
    """bwrap is installed AND can build a box here.

    Installed is not enough: some kernels (Ubuntu 24.04's AppArmor user-namespace
    restriction, many containers) refuse the namespaces bwrap needs, and then
    every boxed command would fail instead of running.
    """
    if not (sys.platform.startswith("linux") and shutil.which("bwrap")):
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                            "--unshare-pid", "--", "true"], capture_output=True)
    return probe.returncode == 0


def prefix(cfg: SandboxConfig, *, workdir: Path, home_root: Path,
           writable: list[Path] = (), readable: list[Path] = ()) -> list[str]:
    """The bwrap argv to put in front of a command, or [] when sandboxing is off.

    `workdir` is the run's worktree (rw, and the cwd inside the box);
    `home_root` is the operator's checkout, hidden except for its .git (ro).
    """
    global _warned
    if not cfg.enabled:
        return []
    if not available():
        if not _warned:
            print("WARNING: sssf sandbox is enabled but bubblewrap is not available or "
                  f"cannot create namespaces on this host ({sys.platform}) — agents and "
                  "quality commands run UNSANDBOXED. "
                  "Install bubblewrap (Linux) or set defaults.sandbox.enabled: false "
                  "to silence this.", file=sys.stderr)
            _warned = True
        return []

    home = Path.home()
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", str(home)]
    for name in cfg.home_readonly:
        path = home / name
        if path.exists():
            argv += ["--ro-bind", str(path), str(path)]
    for name in cfg.home_writable:
        path = home / name
        if path.exists():
            argv += ["--bind", str(path), str(path)]
    # The checkout itself may live outside $HOME; hide it either way, then map
    # back the pieces the run needs. Mount order matters: later binds win.
    if not _under(home_root, home):
        argv += ["--tmpfs", str(home_root)]
    git_dir = home_root / ".git"
    if git_dir.exists():
        argv += ["--ro-bind", str(git_dir), str(git_dir)]
    for path in readable:
        if Path(path).exists():
            argv += ["--ro-bind", str(path), str(path)]
    for path in [workdir, *writable]:
        Path(path).mkdir(parents=True, exist_ok=True)
        argv += ["--bind", str(path), str(path)]
    argv += ["--unshare-pid", "--die-with-parent", "--chdir", str(workdir), "--"]
    return argv


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
