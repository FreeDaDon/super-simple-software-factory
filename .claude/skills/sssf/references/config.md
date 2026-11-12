# Config Reference

The full `sssf.config.yaml` spec: every field, how defaults merge, and how model / effort / tools / MCP map onto the `claude` CLI.

It lives at **`adws/adw_sssf_config/sssf.config.yaml`** — the default path every `adw_*.py` and the justfile resolve, and where `install.py` / `make_config.py` stamp it. Pass `--config <path>` to any ADW (or set `SSSF_CONFIG` for the justfile) to run against a different roster.

## Shape

```yaml
defaults:
  coding_agent: claude_code
  model: sonnet                         # alias, or a full id like claude-sonnet-5-5
  effort: medium                        # low | medium | high | xhigh | max
  harness_engineering: []               # MCP config JSON paths
  setting_sources: ""                   # which Claude Code settings layers an agent loads; "" = none
  tools: [Read, Grep, Glob, Bash, Edit, Write]
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py", adws/conftest.py,
                    .claude/, .github/, .env, "**/.env", .env.*, "**/.env.*"]
  grader_files: ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", ...]
  data_dir: adws/adw_data
  worktree_dir: ../.sssf-worktrees
  sandbox:
    enabled: true
    home_readonly: [.local, .bun, .cargo, .rustup, go, .nvm, .pyenv, .deno, .gitconfig]
    home_writable: [.claude, .claude.json, .cache, .npm]
  agent_idle_timeout_seconds: 900
  agent_max_seconds: 3600
  max_run_cost_usd: 25
  rate_limit_max_wait_seconds: 21600

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

agents:
  - name: planner
    model: opus
    effort: high
    color: "#a78bfa"
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    tools: [Read, Grep, Glob, Bash, Write, Agent]
```

## Fields

### `defaults`

| Field | Type | Meaning |
|---|---|---|
| `coding_agent` | `claude_code` | Which interface runs the agent. Claude Code is the only one. Every agent runs as a `claude -p` process on your Claude subscription. |
| `model` | string | A Claude alias (`fable`, `opus`, `sonnet`, `haiku`) or a full id (`claude-sonnet-5-5`). Default `sonnet`. See [Model resolution](#model-resolution). |
| `effort` | enum | Reasoning effort, passed as `claude --effort`. `low \| medium \| high \| xhigh \| max`. Default `medium`. See [Effort levels](#effort-levels). |
| `color` | hex string | Lane color for every agent that does not set its own. Default empty — the visualizer falls back to its own palette. |
| `harness_engineering` | list[string] | Paths to MCP config JSON files, passed as `--mcp-config`. Default none, and with none an agent gets no MCP servers. |
| `setting_sources` | string | Which Claude Code settings layers an agent loads: any of `user`, `project`, `local`, comma-separated. Default `""`: none — no personal hooks, plugins or memory, and no project hooks or permission rules from the run's worktree. The repo's `CLAUDE.md` is still given to every agent, read from the run's baseline commit. See [Settings layers](#settings-layers). |
| `tools` | list[string] | Roster-wide tool allowlist. Every agent that omits its own `tools` inherits this. Unset = `Read, Edit, Write, Bash, Grep, Glob`. |
| `protected_files` | list[string] | Paths **no** agent may modify unless it names them in its own `writes`. Default: `adws/adw_modules/`, `adws/adw_sssf_config/`, `adws/adw_*.py`, `adws/conftest.py`, `.claude/`, `.github/`, `.env`, `**/.env`, `.env.*`, `**/.env.*` — an agent must not be able to edit the machinery that decides whether its work passed, the CI or Claude Code settings around it, or credentials. |
| `grader_files` | list[string] | Tests and the config that decides how they run (`**/test_*.py`, `**/*_test.py`, `**/conftest.py`, `**/tests/**`, `**/test/**`, `**/__tests__/**`, `**/*.test.*`, `**/*.spec.*`, `**/*_test.go`, plus `pyproject.toml`, `setup.cfg`, `pytest.ini`, `tox.ini`, `package.json`, `bunfig.toml`, `vitest.config.*`, `jest.config.*`, `justfile`, `Makefile`, `Cargo.toml`, `go.mod` at any depth). A matching file that existed at the run's baseline may be modified only if the plan declared it in `may_modify`, and may never be deleted. New ones are always allowed. See [The grader lock](#the-grader-lock). |
| `data_dir` | path | Runtime home, in the operator's checkout. Sessions land at `{data_dir}/sessions/{adw_id}/{agent_name}/`; each run's pinned state (baseline commit, `may_modify`, paths touched) at `{data_dir}/runs/{adw_id}.json`. Default `adws/adw_data`. |
| `worktree_dir` | path | Where each run's git worktree is created, relative to the checkout: `{worktree_dir}/<repo>-<adw_id>`, branch `sssf/<adw_id>`. Default `../.sssf-worktrees` — outside the checkout, so your own test runs never collect a run's copy. See [Isolation](#isolation-worktree-and-sandbox). |
| `sandbox.enabled` | bool | Wrap every agent and every quality command in bubblewrap. Default `true`. Linux only; elsewhere, or without `bwrap`, it warns and runs unsandboxed. |
| `sandbox.home_readonly` | list[string] | `$HOME` entries mapped back, read-only, into the box's empty `$HOME`. Default `.local, .bun, .cargo, .rustup, go, .nvm, .pyenv, .deno, .gitconfig` — toolchains, not credentials. |
| `sandbox.home_writable` | list[string] | `$HOME` entries mapped back writable. Default `.claude, .claude.json, .cache, .npm` — `claude` keeps its login and session files in the first two. |
| `agent_idle_timeout_seconds` | int | Watchdog: a `claude` process with no stream output this long is killed and its phase fails. Default `900`. |
| `agent_max_seconds` | int | Watchdog: a `claude` process running longer than this in total is killed. Default `3600`. |
| `max_run_cost_usd` | number | Cap for one run (a joined run continues the session's spend), in list-price-equivalent USD; on a subscription it measures usage, nothing is charged. What remains is passed to every send as `--max-budget-usd` (the CLI checks it between turns), and checked between sends. Default `25`. |
| `rate_limit_max_wait_seconds` | int | When the subscription usage window runs out mid-run, the run waits for the reset and resumes the same session. A reset further away than this fails the run instead. Default `21600` (6 h). |

### `observability`

| Field | Type | Meaning |
|---|---|---|
| `db` | path | SQLite trace db. `tracer.py` writes it directly; the visualizer polls it. Default `adws/adw_data/sssf.db`. |
| `poll_ms` | int | Visualizer live-poll cadence in ms. History uses the same queries, lazy-paged. Default `500`. |

### `agents[]`

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The identifier ADW scripts use. **ADWs name agents, never models.** |
| `purpose` | yes | One sentence: what this agent is for. Should match its `system.md` Purpose. |
| `prompt_engineering.system` | yes | Path to the system prompt — who the agent is, its single purpose, its output contract. Appended to Claude Code's own system prompt (`--append-system-prompt`), so Claude Code's tool-use guidance stays in place. |
| `prompt_engineering.user` | yes | Path to the default user prompt — the task template with `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`. |
| `color` | no | Hex swatch (`"#a78bfa"`) for this agent's lane in the visualizer. Travels config → `agent_sessions.color` → `/api/sessions/:adw_id`, and rides the `agent_start` event so a lane is colored while the agent is still running. Unset = the UI's fallback palette. |
| `coding_agent`, `model`, `effort`, `color`, `harness_engineering` | no | Override the corresponding `defaults` key. |
| `tools` | no | Allowlist of Claude Code tool names. **Omitting the key inherits `defaults.tools`.** A capability list, not a boundary — see `writes`. |
| `writes` | no | What this agent may modify **in the repo**, enforced after every call. Omitted = unrestricted (still barred from `protected_files`). `[]` = no repo writes at all. A list = only those paths: a trailing `/` is a directory prefix, `*` matches within one path segment, `**` crosses segments, anything else is an exact path. Naming a `protected_files` path here is what unlocks it. **The session runtime under `data_dir` is always writable** — `writes: []` means read-only with respect to the repo, not unable to write its own report. |

Output types are deliberately absent: config defines who an agent *is*; the ADW call site defines how it's *used*. One agent serves many calls — same system prompt, different user prompt + output type per call.

A config that still says `thinking:` is rejected with a message naming the agent: the key was renamed `effort`, and Pydantic would otherwise drop an unknown key without a word.

## Defaults merging

`agents.py` merges each entry **over** `defaults`, key by key. An entry states only what differs; anything unset inherits. `agents.validate(cfg, REQUIRED_AGENTS)` then confirms every name an ADW declares exists, has a well-formed model, a valid effort, valid tool names, existing MCP files, and both prompt files present on disk. Any miss fails the run immediately — **no agent is ever spawned against a half-valid config.**

## Effort levels

```
low | medium | high | xhigh | max
```

Passed as `claude --effort`. Higher effort means more reasoning per turn, which costs more of your subscription window and takes longer. Rough guidance: `high` for planners and reviewers, `medium` for builders, `low` for mechanical read-and-report agents. `off` and `minimal`, which existed on the previous harness, do not exist here.

## Model resolution

`model` is one of:

- **An alias** — `fable`, `opus`, `sonnet`, `haiku`. Claude Code resolves an alias to the newest model of that family at call time.
- **A full id** — `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-2`. Anything matching `claude-*`.

Claude Code has no command that lists models, so `agents.validate()` checks the *shape* of the value, not its existence. A well-formed id that does not exist fails on that agent's first call with the CLI's own message. Run `just demo` after editing the roster: it makes the first call cheaply, before a long chain makes it expensively.

Consequences worth knowing:

- **An alias moves.** When a newer Sonnet ships, `sonnet` starts meaning it. That is usually what you want, and not what you want when you are comparing two runs. Pin a full id for a repeatable run.
- **Subscription weight differs a lot.** Opus drains the five-hour window several times faster than Sonnet, and Haiku barely registers. Spend Opus where judgement matters (planning, review).
- The configured model string is recorded per session in `agent_map.json` and mirrored into the `agent_sessions` table; `agent_map.json` and the `agent_end` event also carry `resolved_model`, the concrete id the CLI actually ran (the one that cost the most, when subagents used several). **Changing an agent's model invalidates its session**: a joined run starts that agent fresh instead of resuming a context window built by a different model. Changing only the alias's target (a new release behind `sonnet`) does not, because the config string is unchanged.

## Tools

`tools` is the allowlist the agent runs under. It is passed twice: as `--tools` (the set of tools that exist for this agent) and as `--allowedTools` (pre-approved, so no prompt is ever needed). The agent runs with `--permission-mode dontAsk`, which **denies** any tool not pre-approved. There is no one to answer a prompt in a non-interactive run, so denial is the right default.

Tool names are Claude Code's and are **case-sensitive**:

| Tool | Purpose |
|---|---|
| `Read` | read file contents |
| `Grep` | search file contents |
| `Glob` | find files by pattern (covers what `ls` and `find` did) |
| `Bash` | execute shell commands |
| `Edit` | find/replace edits |
| `Write` | create/overwrite files |
| `Agent` | start a Claude Code subagent (older versions call it `Task`) |
| `WebFetch`, `WebSearch`, `NotebookEdit`, `TodoWrite` | available, none in the starter roster |
| `mcp__<server>__<tool>` | a tool from an MCP server named in `harness_engineering` |

A name may carry a permission rule, `Bash(git *)`, to narrow a tool to matching commands. Validation checks the part before the parenthesis.

**Resolution order:** an agent's own `tools` list wins; an agent that omits the key inherits `defaults.tools`; if neither is set the default set above (`Read, Edit, Write, Bash, Grep, Glob`) applies. An empty list is not "all tools" — it is a tool-less agent, and it will stall.

`tools` is not a sandbox: `Bash` reaches whatever the process can. What it can reach is bounded by the bubblewrap box ([Isolation](#isolation-worktree-and-sandbox)); what it may *change in the repo* is the next section.

## Write permissions — `writes` and `protected_files`

`tools` cannot express a safety boundary, because two of the tools are general
purpose. `Bash` runs anything, including `git checkout`, which discards an
engineer's uncommitted work; `Write` reaches any path, not only the one report
file an agent was granted it for. So "this agent changes nothing" is a claim a
tool list can state but never keep.

`adw_modules/permissions.py` keeps it, the same way every other claim in this
system is kept — after the fact, against the repo. Before an agent's first
prompt the whole working state is snapshotted as a git tree (tracked and
untracked files, written through a scratch index), plus the gitignored `.env*`
files git cannot see; after its last send (including JSON retries and gate
corrections) it is snapshotted again. Comparing the two is exact to the byte:
any path that appeared, vanished, or changed is attributed to that agent.

Comparing snapshots rather than watching writes is deliberate: a path that was
modified before the agent ran and is clean afterwards has been **reverted**, and
a reversion is a modification. That is what catches `git checkout`.

HEAD is pinned per call. Only the harness commits; an agent that runs
`git commit` has its commit undone (the changes stay in the tree and are judged
like any other), and the moved HEAD is itself a breach.

A breach is not a gate violation. Gates are for work an agent can be asked to
redo; a write has already happened, so re-prompting fixes nothing. Instead:

1. every unauthorized change is rolled back to exactly what the path held
   before the agent ran — restored from the before-snapshot (including earlier,
   uncommitted work in the run), or deleted if it did not exist;
2. the phase fails and names every path with what happened to it.

Changes that pass are recorded on the run as touched paths; `run.commit()`
stages exactly those, never `git add -A`.

```yaml
defaults:
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py", adws/conftest.py,
                    .claude/, .github/, .env, "**/.env", .env.*, "**/.env.*"]

agents:
  - name: builder      # no `writes` key -> unrestricted, minus protected_files
  - name: scout
    writes: []         # no repo writes; its findings still land in context_handoff/
  - name: planner
    writes: [specs/]
  - name: documenter
    writes: [app_docs/, docs/, "**/*.md", "*.md"]
```

**The session runtime under `data_dir` is always writable, for every agent.**
`context_handoff/` is how agents hand work to each other, and each agent's
prompts, `raw_output.jsonl`, and `envelope.json` sit beside it. That grant comes
from `data_dir` rather than from `.gitignore`: the runtime is normally ignored,
so it never even appears in a snapshot, but an agent's ability to record its own
work must not depend on a gitignore line someone can delete.

Narrow by role, not by reflex. Anything that must produce a `context_handoff/` artifact needs `Write`, or it will resort to a `Bash` heredoc. Withhold `Edit`/`Write` only where the restriction *is* the guarantee — a reviewer that cannot edit cannot quietly fix what it was asked to report.

### The grader lock

`writes` says where an agent may write; `grader_files` says which existing files no agent may quietly rewrite to make a red suite green. A path matching `grader_files` that existed at the run's baseline commit is checked for every agent, whatever its `writes`:

- deleted → breach, restored;
- modified, and not in the run's `may_modify` list → breach, restored;
- new since the baseline → allowed. Adding tests is what a builder should do.

`may_modify` comes from the plan: `PlanOutput.may_modify` is a list of `{path, reason}`, exact repo-relative paths, no globs, declared before any code exists. The ADW pins it with `run.declare_grader_edits(plan)` right after the plan phase, into `{data_dir}/runs/{adw_id}.json`, which no agent can write. A chain with no planner has an empty list: every existing test is locked. Because `package.json`, `pyproject.toml` and similar are in the default list, a build that needs a new dependency also needs the plan to declare that file. The lock applies only to an isolated run (one with a baseline commit).

## Isolation: worktree and sandbox

**One worktree per run.** `session.ensure` creates `{worktree_dir}/<repo>-<adw_id>` on a new branch `sssf/<adw_id>`, cut from the checkout's HEAD, and the ADW `chdir`s into it. Config paths, prompts, the trace db and session dirs are made absolute first, so they stay in the operator's checkout. The operator's uncommitted work is never part of a run (the run prints a note when there is some), two runs never share a `git status`, and nothing merges into the base branch: a finished run prints `review`, `merge`, and `discard` commands. A joined run (`--adw-id`) reuses the worktree and the state pinned in `{data_dir}/runs/{adw_id}.json`. Without a git repo or a first commit there is nothing to branch from, and the run works in place.

**The sandbox.** `adw_modules/sandbox.py` wraps every agent's `claude` process, every quality command, and every `tests_pass` gate in bubblewrap: the filesystem is read-only, `$HOME` is an empty tmpfs with only `sandbox.home_readonly` / `home_writable` mapped back, the operator's checkout is hidden (its `.git` stays readable), `/tmp` and the PID namespace are private. Writable: the worktree and the run's session dir. Network stays on. MCP config files named in `harness_engineering` are mapped in read-only. If a toolchain your tests need lives under `$HOME` and is missing inside the box, add its directory to `home_readonly`.

### MCP tools must be named explicitly

`tools` is an allowlist over built-in **and** MCP tools alike. An MCP server named in an agent's `harness_engineering` contributes tools only if each one appears in that agent's `tools` list as `mcp__<server>__<tool>`. Leave one out and it is simply never offered; nothing errors.

```yaml
  - name: reviewer
    harness_engineering:
      - adws/adw_data/harness_engineering/docs-mcp.json     # defines server "docs"
    tools:
      - Read
      - Grep
      - Glob
      - Bash
      - mcp__docs__search                                   # REQUIRED — named, or never offered
```

Adding an MCP server is therefore a two-line change, never one.

## Harness engineering

`harness_engineering` entries are paths to **MCP config JSON files**, passed as `claude --mcp-config <path>`, one flag per entry, scoped to that agent only. Every call also runs with `--strict-mcp-config`, so an agent with no entries has **no** MCP servers, regardless of what your own Claude Code setup has installed. The starter roster ships with none.

Subagents need no file. Add `Agent` to an agent's `tools` and it can fan work out to Claude Code's built-in subagents; the starter planner and scout do. A subagent's tool calls appear in the trace with a `subagent_of` field naming the `Agent` call that spawned them.

## Settings layers

Each agent runs with `--setting-sources <defaults.setting_sources>` and `--disable-slash-commands`. The default, `""`, loads no settings layer at all. Your **user** settings stay out: personal hooks (for example a stop hook that nags for a wiki capture), plugins and memory never fire inside a factory agent, so a run behaves the same on your machine and on a colleague's. **Project** settings stay out too: hooks and permission rules would be read from the run's worktree, which an earlier agent in the same run could have edited.

The repo's `CLAUDE.md` is still given to every agent. `agents._repo_instructions` reads it from the run's **baseline commit** (`git show <baseline>:CLAUDE.md`), not from the worktree, and appends it to the system prompt. Set `user,project` if you want those layers back, knowing the runs are then no longer machine-independent; once `project` is listed, Claude Code loads `CLAUDE.md` itself, from the worktree, and the baseline copy is not added.
