# Run ADW

Run a workflow and report on it. **You run and observe — you never step into the process or do the work yourself.**

## Step 0 — translate the request

**Read [how_to_prompt_for_the_eng.md](how_to_prompt_for_the_eng.md) before you launch anything.** The prompt you pass is read by every agent in the chain, so it gets written deliberately: same intent, sharper words, verified paths, and a stated "done means". That cookbook is the whole procedure; this one starts once you have the prompt.

## The orchestrator's posture

The ADW is the worker. Your job is to launch it, watch the trace, and tell the engineer what happened. Do not read the agent's target files and "help", do not fix the code an agent was supposed to fix, do not edit an envelope. If a run fails, report the failing phase and its violations — the fix is a config, prompt, or ADW change, made deliberately, and then a re-run.

## Launch

Which chain to launch is decided in `how_to_prompt_for_the_eng.md`, and the short version is: **the ADW the engineer named, or else the most complete composed chain the work justifies — never a single-agent one.** Read `ls adws/adw_*.py` and the `Phases:` line in each docstring to see what this repo has; the names below are shape, not a menu.

```bash
uv run adws/<end-to-end-chain>.py "add a /health endpoint"
uv run adws/<plan-build-verify-chain>.py requests/health.md
uv run adws/<build-first-chain>.py "implement the plan" --adw-id a1b2c3d4
uv run adws/<recon-chain>.py "where is auth handled" --config path/to/other.config.yaml
```

The prompt is inline text or a file path. Launch in the background so you can poll while it works; the `adw_id` is printed on startup — capture it, everything else keys off it.

Every run works in its own git worktree, `../.sssf-worktrees/<repo>-<adw_id>`, on branch `sssf/<adw_id>` cut from the checkout's HEAD. The engineer's checkout and its uncommitted changes are never touched (the run prints a note when there are some: they are not part of the run). The trace db and session files stay in the checkout. A chain that needs a real test command (`quality.require`) refuses to start, before spending anything, if `quality.py` still has the placeholder; report that verbatim rather than editing `quality.py` yourself.

### Listen for the roster

The chain says *what runs*; the config says *who runs it*. **If the engineer references a roster, a config, or a model tier, pass it — do not fall through to the default.**

```bash
ls adws/adw_sssf_config/                                          # every roster on disk
grep -nE '^ *(- name|model):' adws/adw_sssf_config/<roster>.yaml  # who is in it, and on which model
```

That gives the path to pass and who is in it. An agent with no `model:` line inherits `defaults.model`.

Read those from disk every time. Rosters are the engineer's to add, rename, and retune, so a name you remember from a doc is a guess.

They will rarely say `--config`. Treat any of these as naming a roster, then resolve it to a file:

| What they say | What it means |
|---|---|
| "run it on the frontier config", "use the frontier roster" | the roster file whose name matches |
| "run this with the big models", "use the sota roster" | the non-default roster — confirm which if there is more than one. Each config's header comment lists the names it answers to, so `head -3` on the file settles it |
| "have opus plan this one" | a roster whose planner is that model; if none exists, say so rather than editing the config mid-request |
| nothing about models at all | the default, `adws/adw_sssf_config/sssf.config.yaml` |

`--config` takes the path directly; the justfile recipes read `SSSF_CONFIG` instead:

```bash
uv run adws/<chain>.py "<prompt>" --config adws/adw_sssf_config/sssf.frontier.config.yaml
SSSF_CONFIG=adws/adw_sssf_config/sssf.frontier.config.yaml just <recipe> "<prompt>"
```

Two things that bite:

- **Never swap rosters on your own.** A different roster is a different cost and a different result. If the default's model looks wrong for the work, say so and let the engineer choose.
- **Switching rosters mid-session breaks resumption.** `agent_map.json` records the model each coding-agent session was created with, so a joined run (`--adw-id`) whose config now names a different model starts that agent **fresh** instead of resuming its context window. That is deliberate — a bad resume is worse — but it means "plan on the frontier roster, then build on the default" costs the builder its accumulated context. Say so when you report it.

`--adw-id` is optional on **every** ADW. Given one, the run joins that session if it exists or creates it pinned to exactly that id: same `sessions/{adw_id}/` dirs, same `context_handoff/`, envelopes appended, and each agent resumes its existing coding-agent context window via `agent_map.json`. That is how you chain ADWs — plan under one id, then build under the same id. A joined run also goes back into the same git worktree and branch, and reads back the baseline commit and the plan's `may_modify` list pinned in `adws/adw_data/runs/{adw_id}.json`; if that worktree has been removed, the join refuses and you start a new run.

## Observe

The trace db is `adws/adw_data/sssf.db`. It is WAL, so reads never block the running writers — poll it as often as you like.

```bash
# where the run stands
uv run adws/sssf.py query \
  "select seq, name, kind, owner, status, attempt from phases where adw_id='a1b2c3d4' order by seq;"

# the live tail — cursor on rowid, same query the visualizer polls
uv run adws/sssf.py query \
  "select rowid, type, name, started_at from events where adw_id='a1b2c3d4' and rowid > 0 order by rowid limit 50;"

# why a phase failed
uv run adws/sssf.py query \
  "select attempt, gate, passed, checks_json from gate_results where adw_id='a1b2c3d4';"

# session-level status
uv run adws/sssf.py query \
  "select adw_id, request, status, total_tokens from sessions order by started_at desc limit 5;"

# what an agent actually did, slowest tool calls first
uv run adws/sssf.py query \
  "select name, tokens, started_at, ended_at from events
   where adw_id='a1b2c3d4' and type='tool_call' order by ended_at desc limit 20;"
```

Poll on a cursor: keep the highest `rowid` you have seen and query `where rowid > ?`. Don't re-read the whole table each pass.

`tool_call` rows carry a real span, so durations come off the columns — see `references/observability.md` for which fields each event type populates.

The ADW also narrates to stdout, and every line it prints is written to the db as a `log` event — terminal and swim lane tell the same story by construction, so tailing the background process is a valid second view rather than a competing source of truth.

Files are the raw record if you need more than the db shows: `adws/adw_data/sessions/{adw_id}/{agent}/raw_output.jsonl` (full coding-agent stream), `envelope.json` (the parsed final response), `prompts/` (exactly what was sent), and `context_handoff/` (what agents wrote for each other).

## When a run is stuck

A hung coding agent produces no events at all, so the trace goes quiet rather than red. Read it in this order:

```bash
just phases <adw_id>     # which phase is still `running`
just procs <adw_id>      # what that phase is actually running, with pids
just kill <adw_id>       # stop it — children first, then the workflow
```

`processes` rows with `ended_at IS NULL` are the live ones. A quiet trace is not always a hang:

- **Waiting on the subscription limit.** A `log` event named `rate_limited` says which agent hit the usage window and `waiting_until` when it resets. The run sleeps until then (plus a minute) and resumes the same session; that is working as designed, not stuck. A reset further away than `defaults.rate_limit_max_wait_seconds` (6 h) fails the phase instead, and the error says to rejoin later with `--adw-id`.
- **A genuinely hung agent.** The watchdog kills a `claude` process with no output for `defaults.agent_idle_timeout_seconds` (15 min) or running past `agent_max_seconds` (1 h), and the phase fails with that reason. `just kill` is for when you want it gone sooner.
- **An agent that never started.** If `procs` shows a `claude` child but the phase has no `tool_call` events and its `raw_output.jsonl` is empty, check that `claude auth status` still shows your login and that nothing is blocking the subprocess (a missing `bwrap` only warns; it does not block).

`just kill` verifies each pid still matches the command that was recorded before signalling, because pids get recycled.

A killed run marks itself `fail` and closes its process rows, so the trace never claims work is in flight that is already dead.

## Report

Tell the engineer, in order: which chain and which roster you launched (name the config whenever it was not the default), which phase is running now (or which failed), phase statuses in sequence, and for a failure the gate violations or the error verbatim. For a run that got as far as finishing, say where the work is: the run prints its branch and three commands (review, merge, discard). Nothing is merged into the engineer's branch; relay the commands and leave the merge to them. A run that failed mid-phase prints nothing, but its worktree and branch are still there, like every run's: `just worktrees` lists them. A run stopped by `max_run_cost_usd` (default $25 list-price equivalent) fails with that reason; say so rather than re-running. Remember **every phase defaults to `fail`** — a phase showing `fail` may simply never have completed; `queued` means it never started. Don't dress up a partial run as a success.

For a visual live view, the visualizer app in the skill (`just obs`, or tmux sessions viz-api :4600 + viz-ui :4601) polls this same db (bound to 127.0.0.1; `SSSF_VIZ_HOST` overrides it, with a warning, since the UI has no authentication) — sessions as cards, runs as swim lanes, phases and tool calls drill-in. The sqlite queries above remain the headless equivalent.
