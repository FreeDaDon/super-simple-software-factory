"""Whole ADWs as real processes, against the fake `claude` CLI. No subscription use."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from conftest import FAKE_CLAUDE, SKILL, git


def set_test_command(repo, argv):
    quality = repo / "adws" / "adw_modules" / "quality.py"
    text = quality.read_text()
    marker = 'argv=_placeholder("test"),'
    assert marker in text
    quality.write_text(text.replace(marker, f"argv={argv!r},"))


def adw(repo, script, prompt="add a feature", env=None, adw_id=None):
    log = repo.parent / "fake_calls.jsonl"
    full_env = {**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE), "FAKE_CLAUDE_LOG": str(log),
                **(env or {})}
    args = [sys.executable, f"adws/{script}", prompt, *(["--adw-id", adw_id] if adw_id else [])]
    proc = subprocess.run(args, cwd=repo, env=full_env, capture_output=True, text=True, timeout=120)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return proc, calls


def phases(repo):
    db = sqlite3.connect(repo / "adws" / "adw_data" / "sssf.db")
    return [r[0] for r in db.execute("SELECT name FROM phases ORDER BY adw_id, seq")]


def adw_ids(repo):
    db = sqlite3.connect(repo / "adws" / "adw_data" / "sssf.db")
    return [r[0] for r in db.execute("SELECT adw_id FROM sessions ORDER BY started_at")]


def test_placeholder_test_command_refuses_before_any_agent(stamped):
    proc, calls = adw(stamped, "adw_build_test.py")
    assert proc.returncode != 0
    assert "placeholder" in proc.stderr and "quality.py" in proc.stderr
    assert calls == []


def test_simple_sdlc_green_lands_on_its_own_branch(stamped):
    # The suite leaves a file behind, the way `uv run` writes uv.lock.
    set_test_command(stamped, ["sh", "-c", "ls feature_*.txt && touch junk.lock"])
    (stamped / "operator_wip.txt").write_text("mine\n")              # untracked operator work
    main_head = git(stamped, "rev-parse", "main")
    proc, calls = adw(stamped, "adw_simple_sdlc.py")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    names = phases(stamped)
    assert names[:5] == ["request", "plan", "commit_plan", "build", "test_1"]
    assert "review_1" in names and "commit_docs" in names
    # main and the operator's checkout are untouched
    assert git(stamped, "rev-parse", "main") == main_head
    assert (stamped / "operator_wip.txt").exists()
    assert "quality.py" in git(stamped, "status", "--porcelain")
    # the work is on sssf/<adw_id>, three commits, none carrying operator files
    adw_id = adw_ids(stamped)[0]
    log = git(stamped, "log", "--format=%s", f"main..sssf/{adw_id}").splitlines()
    assert log == [f"Document {adw_id}", f"Build {adw_id}", f"Add spec {adw_id}"]
    files = git(stamped, "diff", "--name-only", f"main..sssf/{adw_id}").splitlines()
    assert f"feature_{adw_id}.txt" in files
    assert "operator_wip.txt" not in files and "adws/adw_modules/quality.py" not in files
    assert "junk.lock" not in files                   # only agent-touched paths are committed
    assert f"merge sssf/{adw_id}" in proc.stdout
    # agents ran in the worktree, not the checkout
    assert all(c["cwd"] != str(stamped) for c in calls)


def test_red_suite_ends_on_a_test_and_skips_review(stamped):
    set_test_command(stamped, ["sh", "-c", "exit 1"])
    proc, _ = adw(stamped, "adw_simple_sdlc.py")
    assert proc.returncode == 1
    names = phases(stamped)
    assert [n for n in names if n.startswith("fix_")] == ["fix_1", "fix_2"]
    assert names[-1] == "test_3"                      # never ends on an unverified fix
    assert not any(n.startswith("review") for n in names)


def test_last_fix_that_works_is_scored_green(stamped, tmp_path):
    # red until the builder has run three times (build + fix_1 + fix_2)
    set_test_command(stamped, ["sh", "-c", "test $(ls feature_* 2>/dev/null | wc -l) -ge 1 "
                                           "&& test -f ok3"])
    counter = tmp_path / "builds"
    wrapper = tmp_path / "claude_counting.py"
    wrapper.write_text(f"""#!{sys.executable}
import os, subprocess, sys
data = sys.stdin.read()
if "Builder Agent" in " ".join(sys.argv):
    n = int(open({str(counter)!r}).read()) + 1 if os.path.exists({str(counter)!r}) else 1
    open({str(counter)!r}, "w").write(str(n))
    if n >= 3:
        open("ok3", "w").write("")
sys.exit(subprocess.run([{sys.executable!r}, {str(FAKE_CLAUDE)!r}, *sys.argv[1:]], input=data, text=True).returncode)
""")
    wrapper.chmod(0o755)
    proc, _ = adw(stamped, "adw_simple_sdlc.py", env={"CLAUDE_PATH": str(wrapper)})
    names = phases(stamped)
    assert [n for n in names if n.startswith(("test_", "fix_"))] == \
        ["test_1", "fix_1", "test_2", "fix_2", "test_3"]
    assert proc.returncode == 0, proc.stdout[-3000:]


def test_parallel_runs_do_not_cross_contaminate(stamped):
    main_head = git(stamped, "rev-parse", "main")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: adw(stamped, "adw_plan_build.py", adw_id=f"par{i}0000"),
                                [1, 2]))
    for proc, _ in results:
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    for adw_id, other in (("par10000", "par20000"), ("par20000", "par10000")):
        files = git(stamped, "diff", "--name-only", f"main..sssf/{adw_id}").splitlines()
        assert f"feature_{adw_id}.txt" in files and f"feature_{other}.txt" not in files
    assert git(stamped, "rev-parse", "main") == main_head
    assert not list(stamped.glob("feature_*"))


def test_builder_breach_fails_and_leaves_no_trace(stamped):
    proc, _ = adw(stamped, "adw_plan_build.py", env={"FAKE_CLAUDE_MODE": "breach"})
    # The builder may write anywhere but the factory: hack.txt is its business,
    # gutting the gates it is judged by is not.
    assert proc.returncode != 0
    assert "adws/adw_modules/gates.py" in proc.stderr and "restored" in proc.stderr
    adw_id = adw_ids(stamped)[0]
    tree = stamped.parent / ".sssf-worktrees" / f"{stamped.name}-{adw_id}"
    assert "build" in phases(stamped) and "commit" not in phases(stamped)
    assert (tree / "adws" / "adw_modules" / "gates.py").read_text() == \
        (stamped / "adws" / "adw_modules" / "gates.py").read_text()


def test_builder_cannot_weaken_an_undeclared_test(stamped):
    set_test_command(stamped, ["sh", "-c", "exit 0"])
    proc, _ = adw(stamped, "adw_plan_build_test.py", env={"FAKE_EDIT_TEST": "tests/test_app.py"})
    assert proc.returncode != 0 and "did not declare" in proc.stderr


def test_plan_declared_test_edit_is_allowed(stamped):
    set_test_command(stamped, ["sh", "-c", "exit 0"])
    declared = json.dumps([{"path": "tests/test_app.py", "reason": "behaviour changes"}])
    proc, _ = adw(stamped, "adw_plan_build_test.py",
                  env={"FAKE_EDIT_TEST": "tests/test_app.py", "FAKE_MAY_MODIFY": declared})
    assert proc.returncode == 0, proc.stdout[-3000:]


def test_joined_build_reads_the_plans_declaration(stamped):
    declared = json.dumps([{"path": "tests/test_app.py", "reason": "behaviour changes"}])
    proc, _ = adw(stamped, "adw_plan.py", env={"FAKE_MAY_MODIFY": declared}, adw_id="join0001")
    assert proc.returncode == 0, proc.stdout[-2000:]
    proc, _ = adw(stamped, "adw_build.py", env={"FAKE_EDIT_TEST": "tests/test_app.py"},
                  adw_id="join0001")
    assert proc.returncode == 0, proc.stdout[-2000:]


def test_repo_root_pytest_does_not_collect_the_factory(stamped):
    # adw_build_test.py matches pytest's `*_test.py`. In a target repo's own
    # environment (no pyyaml) collecting it is an import error that turns the
    # whole suite red — which is what the first live run hit.
    bare = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    base_python = os.path.join(sys.base_prefix, "bin", "python3")
    proc = subprocess.run(["uv", "run", "--no-project", "--python", base_python,
                           "--with", "pytest", "python", "-m", "pytest", "adws"],
                          cwd=stamped, capture_output=True, text=True, env=bare)
    assert proc.returncode == 5, proc.stdout[-2000:]   # "no tests collected", not an error


def test_generated_adw_runs(stamped):
    subprocess.run([sys.executable, str(SKILL / "scripts" / "make_adw.py"), "--name", "gen",
                    "--agents", "planner,builder"], cwd=stamped, check=True, capture_output=True)
    proc, calls = adw(stamped, "adw_gen.py")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert len(calls) == 2
