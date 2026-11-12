# Create Config

Generate `sssf.config.yaml` — the agent roster for a target repo.

## Generate it

```bash
uv run .claude/skills/sssf/scripts/make_config.py
```

Writes `adws/adw_sssf_config/sssf.config.yaml` — creating the directory if needed — with the starter agents (planner, builder, scout, reviewer, documenter) wired to the prompt files `/sssf install` stamped into `adws/adw_data/prompt_engineering/`. That path is the default every ADW and the justfile look for; `--config` overrides it. `make_config.py` refuses to overwrite an existing config unless you pass `--force`, so retuning an existing roster is a hand edit — see `update_config.md`.

## The rule

**One agent, one prompt, one purpose.** An entry defines who an agent *is*: its model, effort level, and exactly one system prompt plus one user prompt. How it gets *used* — the output type, a per-call user prompt override — lives at the ADW call site, never here.

## Schema

```yaml
defaults:
  coding_agent: claude_code        # every agent runs through the claude CLI on your subscription
  model: sonnet                    # fable | opus | sonnet | haiku, or a full id like claude-sonnet-5-5
  effort: medium                   # low | medium | high | xhigh | max
  harness_engineering: []          # MCP config JSON paths (--mcp-config)
  setting_sources: ""              # no settings layers; the repo's CLAUDE.md is still injected (from the run's baseline commit)
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py", adws/conftest.py,
                    .claude/, .github/, .env, "**/.env", .env.*, "**/.env.*"]
  grader_files: ["**/test_*.py", "**/tests/**", "**/package.json", ...]   # existing ones locked unless the plan declares them
  data_dir: adws/adw_data          # runtime home: {data_dir}/sessions/{adw_id}/{agent_name}/
  worktree_dir: ../.sssf-worktrees # one git worktree per run, on branch sssf/<adw_id>
  sandbox: {enabled: true, home_readonly: [.local, ...], home_writable: [.claude, .claude.json, .cache, .npm]}
  agent_idle_timeout_seconds: 900  # watchdog: kill a silent claude process
  agent_max_seconds: 3600          # watchdog: kill a claude process that runs this long
  max_run_cost_usd: 25             # list-price-equivalent cap for one run
  rate_limit_max_wait_seconds: 21600   # longest wait for a subscription-limit reset

observability:
  db: adws/adw_data/sssf.db        # tracer writes here; the UI polls it
  poll_ms: 500                     # visualizer live-poll cadence

agents:
  - name: planner                  # ADW scripts name agents, never models
    model: opus
    effort: high
    color: "#a78bfa"               # optional hex — this agent's lane color in the visualizer
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md

  - name: scout
    model: haiku
    effort: low                    # unset keys fall through to defaults
    purpose: Find and report where things live; change nothing.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/scout/system.md
      user: adws/adw_data/prompt_engineering/scout/user.md
    tools:                         # optional allowlist — omit the key to inherit defaults.tools
      - Read
      - Bash
```

The template (`templates/sssf.config.yaml`, which `make_config.py` copies) spells every list out in full; the abbreviated lines above are shape, not values to paste. Every agent entry merges over `defaults`, so an entry only states what differs. Tool names are Claude Code's and are case-sensitive: `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `Agent`. A read-only recon agent gets `[Read, Grep, Glob, Bash]`; a builder omits `tools` and inherits the roster default.

## After generating

1. Each agent needs its prompt pair to exist on disk: `adws/adw_data/prompt_engineering/{name}/system.md` and `user.md`. `agents.validate()` fails the run at startup if either is missing.
2. Write `purpose` as one sentence and make the system prompt say the same thing — the two should not drift.
3. Validate by running the smallest ADW that names your agents; a bad entry fails fast, before anything spawns.

Full field-by-field spec, effort levels, and model resolution: `references/config.md`. Retuning an existing roster: `update_config.md`.
