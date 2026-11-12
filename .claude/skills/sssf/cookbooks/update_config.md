# Update Config

Add or retune agents in `sssf.config.yaml`.

## Retune model or effort

Edit the agent's entry in place:

```yaml
  - name: builder
    model: sonnet        # alias: fable | opus | sonnet | haiku — or a full id, claude-sonnet-5-5
    effort: high         # was medium: low | medium | high | xhigh | max
```

An alias always means the newest model of that family, so it moves when a new one ships. Pin a full id when you are comparing runs. Claude Code cannot list models, so `agents.validate()` only checks that the value looks like a Claude model; a well-formed id that does not exist fails on the agent's first call. Re-run `just demo` after editing the roster to find out cheaply. See `references/config.md`.

Everything runs on your Claude subscription, so model choice is a usage-limit decision: Opus drains the five-hour window several times faster than Sonnet, and Haiku barely registers.

**A model change means a fresh session.** `agent_map.json` records the model string each coding-agent session was created with. When a joined run (`--adw-id`) finds the config's model no longer matches the recorded one, that agent starts a **new** session rather than resuming — the map is updated, never a bad resume. Effort changes do not invalidate a session; model changes do. Expect the agent to lose its accumulated context window on the first run after the change.

## Recolor an agent's lane

```yaml
  - name: builder
    color: "#22d3ee"      # hex; the starter roster ships violet/cyan/amber/green
```

Purely cosmetic and safe to change mid-project: the color rides the `agent_start` event and the `agent_sessions` row, so the visualizer picks it up on the next run without touching past sessions. Omit the key to let the UI's fallback palette choose.

## Retune tools

Claude Code's tool names, case-sensitive: `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, plus `Agent` for subagents. `Glob` covers what `ls` and `find` did, so there is no separate tool for either.

Set the roster-wide floor in `defaults`, then narrow per agent:

```yaml
defaults:
  tools: [Read, Grep, Glob, Bash, Edit, Write]

agents:
  - name: reviewer
    tools:                # explicit list wins over defaults
      - Read
      - Grep
      - Glob
      - Bash
      - Write
```

The list is an allowlist and the agent runs under `--permission-mode dontAsk`: a tool not named is **denied**, not prompted for. A misspelled or wrong-case name (`read`, `ls`) is rejected by `agents.validate()` before anything spawns.

**Resolution:** the agent's own list wins → else it inherits `defaults.tools` → else the default set (`Read, Edit, Write, Bash, Grep, Glob`). An empty list is not "all tools"; it is a tool-less agent, and it will stall.

Narrow by role, not by reflex:

- Any agent that must produce a `context_handoff/` artifact needs **`Write`** — without it, it falls back to a `Bash` heredoc to create the file the gate checks for.
- Withhold `Edit`/`Write` only where the restriction *is* the guarantee. The reviewer's contract is "change nothing", so withholding `Edit` makes that structural instead of merely prompted. (`writes: []` is what actually enforces it.)
- Recon agents should get the full read surface (`Read`, `Grep`, `Glob`) — cheaper and more legible in the trace than the equivalent `Bash` calls.
- Add `Agent` to let an agent fan work out to Claude Code subagents. Their tool calls appear in the trace marked `subagent_of`.

## Add MCP servers

```yaml
    harness_engineering:
      - adws/adw_data/harness_engineering/docs-mcp.json    # an MCP config FILE PATH
```

Entries are paths to MCP config JSON files, passed as `claude --mcp-config <path>`, applied to that agent only. Every call runs with `--strict-mcp-config`, so an agent with no entries has no MCP servers at all, whatever your own Claude Code has installed. The starter roster ships with none — this is an escape hatch, not a default.

**Adding an MCP server is a two-part edit.** The config path goes in `harness_engineering`, *and* each tool you want the agent to use goes in its `tools` list as `mcp__<server>__<tool>`:

```yaml
  - name: reviewer
    harness_engineering:
      - adws/adw_data/harness_engineering/docs-mcp.json     # defines server "docs"
    tools:
      - Read
      - Grep
      - Glob
      - Bash
      - mcp__docs__search                                   # REQUIRED — or the tool is never offered
```

Skip the second half and it fails quietly: server loaded, run green, tool never available to the model.

## Add a new agent

Three steps, all required — skipping any one fails `agents.validate()` at ADW startup, before anything spawns:

1. **Prompts.** Create `adws/adw_data/prompt_engineering/{name}/system.md` (Purpose + Instructions — the agent's static identity, nothing else) and `user.md` (an h3 per incoming datum: `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`, then the task, then a `## Report` section showing the exact output JSON). Copy an existing pair as the shape.
2. **Config entry.** Name, purpose, prompt refs, plus anything that differs from `defaults`.
3. **An output type.** Every agent call parses against a concrete Pydantic model in `adw_modules/data_types.py`. If none of `PlanOutput`, `BuildOutput`, `ScoutOutput`, `ReviewOutput`, `DocumentOutput` fits the new agent's report, add one — see `update_modules.md`. The user prompt's `Report` section must show exactly that JSON shape.

Then name the agent in an ADW's `REQUIRED_AGENTS` and call it.

## Rules that do not bend

- ADW scripts name **agents**, never models. Swapping a model is a config edit and touches no Python.
- One agent, one prompt, one purpose. If an entry needs two purposes, it is two agents.
- Output types never appear in config — they live at the call site, paired with the user prompt.

Full spec: `references/config.md`.
