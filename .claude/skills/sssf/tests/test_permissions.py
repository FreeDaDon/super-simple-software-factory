"""permissions.enforce: forced breaches are detected, rolled back, and named (V1, item 4)."""

import subprocess
from types import SimpleNamespace

import pytest
from conftest import git

from adw_modules import permissions
from adw_modules.data_types import AgentConfig, PromptEngineering, SSSFConfig
from adw_modules.workspace import Workspace

PE = PromptEngineering(system="s.md", user="u.md")
BUILDER = AgentConfig(name="builder", prompt_engineering=PE)                 # writes: None
READONLY = AgentConfig(name="scout", prompt_engineering=PE, writes=[])
PLANNER = AgentConfig(name="planner", prompt_engineering=PE, writes=["specs/"])


@pytest.fixture
def run(repo):
    (repo / "adws" / "adw_modules").mkdir(parents=True)
    (repo / "adws" / "adw_modules" / "gates.py").write_text("REAL = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "factory")
    ws = Workspace(home_root=str(repo), repo_root=str(repo), branch="sssf/x",
                   baseline=git(repo, "rev-parse", "HEAD"))
    return SimpleNamespace(repo_root=repo, cfg=SSSFConfig(), workspace=ws)


def enforce(run, agent, act):
    before = permissions.snapshot(run)
    act()
    return permissions.enforce(run, None, agent, before)


def test_allowed_write_passes_and_is_reported(run, repo):
    touched = enforce(run, BUILDER, lambda: (repo / "app.py").write_text("changed\n"))
    assert touched == ["app.py"]


def test_protected_tracked_edit_is_rolled_back(run, repo):
    target = repo / "adws" / "adw_modules" / "gates.py"
    with pytest.raises(permissions.PermissionBreach, match="adws/adw_modules/gates.py"):
        enforce(run, BUILDER, lambda: target.write_text("# gutted\n"))
    assert target.read_text() == "REAL = True\n"


def test_untracked_file_from_read_only_agent_is_deleted(run, repo):
    with pytest.raises(permissions.PermissionBreach, match="hack.txt"):
        enforce(run, READONLY, lambda: (repo / "hack.txt").write_text("x"))
    assert not (repo / "hack.txt").exists()


def test_agent_commit_is_undone_and_judged(run, repo):
    head = git(repo, "rev-parse", "HEAD")
    target = repo / "adws" / "adw_modules" / "gates.py"

    def commit_protected_edit():
        target.write_text("# gutted\n")
        git(repo, "commit", "-qam", "sneaky")

    with pytest.raises(permissions.PermissionBreach) as info:
        enforce(run, BUILDER, commit_protected_edit)
    assert "HEAD" in str(info.value) and "gates.py" in str(info.value)
    assert git(repo, "rev-parse", "HEAD") == head
    assert target.read_text() == "REAL = True\n"


def test_agent_commit_of_allowed_work_still_breaches_but_keeps_work(run, repo):
    def commit_allowed():
        (repo / "app.py").write_text("changed\n")
        git(repo, "commit", "-qam", "agent commit")

    with pytest.raises(permissions.PermissionBreach, match="only the harness commits"):
        enforce(run, BUILDER, commit_allowed)
    assert (repo / "app.py").read_text() == "changed\n"        # kept, uncommitted
    assert "app.py" in git(repo, "status", "--porcelain")


def test_gitignored_env_write_is_caught(run, repo):
    with pytest.raises(permissions.PermissionBreach, match=r"\.env"):
        enforce(run, BUILDER, lambda: (repo / ".env").write_text("TOKEN=x\n"))
    assert not (repo / ".env").exists()


def test_existing_env_is_restored(run, repo):
    (repo / ".env").write_text("ORIGINAL=1\n")
    with pytest.raises(permissions.PermissionBreach):
        enforce(run, BUILDER, lambda: (repo / ".env").write_text("CHANGED=1\n"))
    assert (repo / ".env").read_text() == "ORIGINAL=1\n"


def test_reverted_uncommitted_work_is_restored_exactly(run, repo):
    target = repo / "adws" / "adw_modules" / "gates.py"
    target.write_text("REAL = True\nOPERATOR_WIP = 1\n")       # dirty before the agent
    with pytest.raises(permissions.PermissionBreach):
        enforce(run, BUILDER, lambda: subprocess.run(["git", "checkout", "--", str(target)],
                                                     cwd=repo, check=True))
    assert target.read_text() == "REAL = True\nOPERATOR_WIP = 1\n"


def test_same_size_edit_is_detected(run, repo):
    # The old numstat fingerprint missed an edit that kept line counts equal.
    target = repo / "adws" / "adw_modules" / "gates.py"
    target.write_text("REAL = 1111\n")
    with pytest.raises(permissions.PermissionBreach):
        enforce(run, BUILDER, lambda: target.write_text("REAL = 2222\n"))
    assert target.read_text() == "REAL = 1111\n"


# ── the grader (item 2) ──────────────────────────────────────────────────────

def test_deleting_an_existing_test_fails(run, repo):
    with pytest.raises(permissions.PermissionBreach, match="deleted an existing test"):
        enforce(run, BUILDER, lambda: (repo / "tests" / "test_app.py").unlink())
    assert (repo / "tests" / "test_app.py").exists()


def test_weakening_an_undeclared_test_fails(run, repo):
    target = repo / "tests" / "test_app.py"
    original = target.read_text()
    with pytest.raises(permissions.PermissionBreach, match="did not declare"):
        enforce(run, BUILDER, lambda: target.write_text("def test_add():\n    assert True\n"))
    assert target.read_text() == original


def test_declared_test_edit_is_allowed(run, repo):
    run.workspace.may_modify = ["tests/test_app.py"]
    touched = enforce(run, BUILDER, lambda: (repo / "tests" / "test_app.py").write_text("x = 1\n"))
    assert touched == ["tests/test_app.py"]


def test_declared_test_still_cannot_be_deleted(run, repo):
    run.workspace.may_modify = ["tests/test_app.py"]
    with pytest.raises(permissions.PermissionBreach, match="deleted"):
        enforce(run, BUILDER, lambda: (repo / "tests" / "test_app.py").unlink())


def test_adding_a_new_test_is_allowed(run, repo):
    touched = enforce(run, BUILDER, lambda: (repo / "tests" / "test_new.py").write_text("x = 1\n"))
    assert touched == ["tests/test_new.py"]


def test_existing_package_manifest_is_grader(run, repo):
    (repo / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pkg")
    run.workspace.baseline = git(repo, "rev-parse", "HEAD")
    with pytest.raises(permissions.PermissionBreach, match="package.json"):
        enforce(run, BUILDER, lambda: (repo / "package.json").write_text('{"scripts": {"test": "true"}}\n'))


def test_planner_writes_only_specs(run, repo):
    assert enforce(run, PLANNER, lambda: ((repo / "specs").mkdir(),
                                          (repo / "specs" / "p.md").write_text("plan"))) == ["specs/p.md"]
    with pytest.raises(permissions.PermissionBreach):
        enforce(run, PLANNER, lambda: (repo / "app.py").write_text("no\n"))


@pytest.mark.parametrize("path,pattern,expected", [
    ("test_x.py", "**/test_*.py", True),
    ("a/b/test_x.py", "**/test_*.py", True),
    ("tests/unit/x.py", "**/tests/**", True),
    ("adws/adw_data/sessions/x/y.py", "adws/adw_*.py", False),
    ("adws/adw_plan.py", "adws/adw_*.py", True),
    (".env.local", ".env.*", True),
    ("pkg/.env", "**/.env", True),
])
def test_glob(path, pattern, expected):
    assert permissions._matches(path, pattern) is expected
