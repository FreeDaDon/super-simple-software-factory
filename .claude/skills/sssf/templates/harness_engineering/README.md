# harness_engineering/

What an agent's harness can *do*, beyond its built-in tools. Yours the moment it is stamped.

Each entry in an agent's `harness_engineering:` list is the path to an **MCP config JSON file**, passed to that agent only as `claude --mcp-config <path>`. With no entries the agent gets no MCP servers at all (every call runs with `--strict-mcp-config`).

```yaml
agents:
  - name: builder
    harness_engineering:
      - adws/adw_data/harness_engineering/docs-mcp.json
    tools: [Read, Edit, Write, Bash, Grep, Glob, "mcp__docs__search"]   # MCP tools MUST be named here
```

An MCP tool the agent should use has to be on its `tools` list as `mcp__<server>__<tool>`; the list is an allowlist and anything unnamed is denied.

The agent runs inside the bubblewrap sandbox, and so does any MCP server it starts. The config file is mapped in read-only, but a server binary or config that lives under `$HOME` is invisible unless its directory is on `defaults.sandbox.home_readonly`.

Subagents need no file: add `Agent` to an agent's `tools` and it can fan work out to Claude Code's built-in subagents (the starter planner and scout do).
