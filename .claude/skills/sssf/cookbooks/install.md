# Install

`/sssf install` — stamp the entire factory out of the skill and into the current working directory.

## Run it

```bash
uv run .claude/skills/sssf/scripts/install.py
```

Run from the **target repo root** — the cwd is where everything lands. If the skill lives in your user scope, the path is `~/.claude/skills/sssf/scripts/install.py`.

## What gets stamped

`install.py` copies `templates/` into the cwd:

| Stamped | From | Tracked? |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes — the agent roster |
| `.env.sample` | `templates/env.sample` | yes |
| `adws/adw_*.py` | `templates/adws/` | yes — the twelve starter ADWs |
| `adws/sssf.py`, `adws/conftest.py` | `templates/adws/` | yes — trace queries and `kill` without the sqlite3 CLI; a conftest that keeps a repo-root `pytest` from collecting `adw_build_test.py` / `adw_plan_build_test.py` |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes — all low-level logic |
| `adws/adw_data/prompt_engineering/{planner,builder,scout,reviewer,documenter}/` | `templates/prompt_engineering/` | yes — **the user-owned home for prompts** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes — **the user-owned home for MCP config files** |
| `justfile` | `templates/justfile` | yes — starter recipes: `just demo`, the workflows, the trace reads, `just obs` |
| `adws/adw_data/sessions/`, `adws/adw_data/runs/`, `adws/adw_data/sssf.db` | created at runtime | no — gitignored |
| `../.sssf-worktrees/<repo>-<adw_id>` | created per run (`defaults.worktree_dir`) | outside the repo — one git worktree per run, on branch `sssf/<adw_id>` |

The two `*_engineering` dirs mirror the two config keys of the same name: `prompt_engineering` is what an agent is told, `harness_engineering` is what its harness can do. Both are yours the moment they are stamped. Edit them in `adws/adw_data/`, never back inside the skill.

`harness_engineering/` ships with a README only: an agent's `harness_engineering:` entries are paths to MCP config JSON files, and the starter roster uses none. Subagents need no file, since the planner and scout simply have the `Agent` tool.

## Idempotency

Re-running is safe. `install.py` skips **every** file that already exists — your config, your prompts, and previously stamped code alike — and reports what it skipped, so a second run doubles as a drift check. To refresh stamped code (`adw_modules/`, the starter `adw_*.py`) to the skill's current version, run with `--force` — but know that `--force` overwrites ALL existing stamped files, including `sssf.config.yaml` and `prompt_engineering/`, so commit or back up user-owned edits first.

## Post-install checklist

1. **Claude Code is installed and signed in** — `claude --version`, then `claude auth status` should show your subscription. If it does not, `claude auth login`. There are no provider keys to set. Set `CLAUDE_PATH` in `.env` if `claude` is not on PATH.
2. **No `ANTHROPIC_API_KEY`** — Claude Code prefers an API key over the subscription login, so a key in your shell would mean metered billing. SSSF strips it from what it hands to `claude`, but do not rely on that: unset it. `SSSF_ALLOW_API_KEY=1` opts back in deliberately.
3. **The models exist** — the roster uses aliases (`opus`, `sonnet`, `haiku`) that Claude Code resolves itself. `agents.validate()` can only check that a model *looks* like a Claude model; the smoke test below is what proves it. See `references/config.md`.
4. **Gitignore** — `install.py` appends `adws/adw_data/sessions/`, `adws/adw_data/runs/`, `adws/adw_data/sssf.db*`, `.env`, `__pycache__/`, and `*.pyc` for you; confirm they landed. They are runtime state, secrets, or bytecode and must never be committed.
5. **Git repo with a commit** — every run branches from your HEAD into its own worktree (`../.sssf-worktrees/<repo>-<adw_id>`, branch `sssf/<adw_id>`), so the repo needs at least one commit. Without one (or outside a git repo) the run works in place in your checkout with no worktree isolation; read-only ADWs still run, and outside a repo a commit phase refuses. Run `git init` and make a first commit before using `adw_plan_build.py`, `adw_plan_build_test.py`, `adw_plan_build_test_quality.py`, or `adw_simple_sdlc.py`. Uncommitted work in your checkout is never part of a run; commit what the agents should see. `adw_document.py` measures the change with `git diff` against a base ref (`main` by default, `--base` to override).
6. **The test command** — `quality.py` ships placeholder blocks that fail, and every ADW with a test phase refuses to start until `test_spec()` holds a real argv. `install.py` prints the command it detected from the repo; check it and set it in `adws/adw_modules/quality.py`. Do the same for `lint_spec()`, `typecheck_spec()`, and `build_spec()` before running `adw_quality.py` or `adw_plan_build_test_quality.py`, which require all four.
7. **bubblewrap** — agents and quality commands run inside a `bwrap` sandbox on Linux (`apt install bubblewrap`). Without it they run unsandboxed and every run prints a warning; set `defaults.sandbox.enabled: false` to acknowledge that. If a tool your tests need lives in `$HOME` and is not found inside the box, add its directory to `defaults.sandbox.home_readonly` (see `references/config.md`).
8. **Smoke test** — `just demo` runs two cheap read-only workflows back to back, or run the smallest ADW directly:

```bash
just demo                                                    # both, end to end
uv run adws/adw_prompt.py "reply with a one-line summary of this repo"   # the raw form
```

Green means the whole path works: config validated, session minted, Claude Code ran, envelope parsed, events landed in `adws/adw_data/sssf.db`. Verify the trace exists before trusting anything larger:

```bash
uv run adws/sssf.py query "select adw_id, status from sessions order by started_at desc limit 1;"
```

If the smoke test fails, fix it before composing chains — every multi-agent ADW rides on this exact path.
