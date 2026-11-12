#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the SSSF factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(5 starter agents), adws/adw_data/harness_engineering/ (MCP config home),
adws/adw_sssf_config/sssf.config.yaml, .env.sample, justfile, .gitignore entries.
Existing files are skipped unless --force.
"""

import argparse
import shutil
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/runs/",
    "adws/adw_data/sssf.db*",
    ".env",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # An earlier version committed with `git add -A`, and 15 .pyc files showed
    # up in the first repo it was installed into. Commits now stage only paths
    # agents touched, but bytecode still has no business in a repo.
    "__pycache__/",
    "*.pyc",
]


def stamp(src: Path, dest: Path, force: bool, stamped: list, skipped: list) -> None:
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name == "__pycache__":
                continue
            stamp(child, dest / child.name, force, stamped, skipped)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stamped.append(str(dest))


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# sssf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


# Marker file -> the test command it implies. First match wins.
TEST_RUNNERS = [
    ("pyproject.toml", '["uv", "run", "pytest", "-q"]'),
    ("pytest.ini", '["uv", "run", "pytest", "-q"]'),
    ("bun.lock", '["bun", "test"]'),
    ("bun.lockb", '["bun", "test"]'),
    ("package.json", '["npm", "test"]'),
    ("go.mod", '["go", "test", "./..."]'),
    ("Cargo.toml", '["cargo", "test"]'),
]


def detect_test_command(root: Path) -> str:
    return next((argv for marker, argv in TEST_RUNNERS if (root / marker).exists()), "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    root = Path.cwd()
    stamped, skipped = [], []

    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped)
    stamp(TEMPLATES / "prompt_engineering",
          root / "adws" / "adw_data" / "prompt_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "harness_engineering",
          root / "adws" / "adw_data" / "harness_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "sssf.config.yaml",
          root / "adws" / "adw_sssf_config" / "sssf.config.yaml",
          args.force, stamped, skipped)
    stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Skipped like any other file if the repo already has a justfile.
    stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped, skipped)
    ensure_gitignore(root, stamped)

    print(f"sssf installed into {root}")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    print("\nnext steps:")
    suggestion = detect_test_command(root)
    print("  0. set your test command in adws/adw_modules/quality.py, test_spec():")
    print(f"       argv={suggestion}    # detected from this repo — check it" if suggestion
          else '       argv=["uv", "run", "pytest", "-q"]  (or bun test, npm test, go test ./...)')
    print("     until you do, every ADW with a test phase refuses to start (the read-only")
    print("     demos below still run)")
    print("  1. claude auth status    # SSSF runs every agent on your Claude subscription;")
    print("                           # sign in with `claude auth login` if needed. No API keys,")
    print("                           # and leave ANTHROPIC_API_KEY unset (it would bill the API)")
    print("  2. just demo             # two cheap read-only runs, end to end")
    print("  3. just sessions         # what just happened")
    print("  4. just obs              # the trace UI, needs bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
