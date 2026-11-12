# Plan Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Plan the work described in `prompt`.

1. Write the full plan to `<context_handoff_dir>/plan.md` — this is the copy the builder reads.
2. Copy that file into the repo under `specs/`:
   - **List `specs/` before you pick the name.** A session that plans more than once reuses its `<adw_id>`, so the obvious name may already be taken.
   - Base name: `specs/<adw_id>_<slug>.md`, where `<adw_id>` is the session directory name inside `context_handoff_dir` (`.../sessions/<adw_id>/context_handoff`) and `<slug>` is two to four kebab-case words naming the work.
   - If a file with that name already exists, use `specs/<adw_id>_<slug>_v2.md`, then `_v3`, and so on until the name is free. **Never overwrite an existing spec** — the earlier plan is the record of what was asked for then.
   - **Copy it, do not retype it.** One bash call does the whole step:
     `mkdir -p specs && cp "<context_handoff_dir>/plan.md" "specs/<adw_id>_<slug>.md"`
     Writing the plan a second time through `write` re-emits every line you already wrote, which costs the whole document again in output tokens and lets the two copies drift.
3. Emit your `Report` JSON, declaring BOTH paths in `artifacts`.

## Report

Respond with ONLY valid JSON matching `PlanOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence describing the plan>",
  "artifacts": ["<context_handoff_dir>/plan.md", "specs/<adw_id>_<slug>.md"],
  "commit_message": "<imperative one-line git subject for committing THIS PLAN DOCUMENT, not the work it describes — e.g. 'Add spec for the /health endpoint'>",
  "may_modify": [{"path": "tests/test_api.py", "reason": "the request renames /status to /health, which this test asserts"}],
  "notes_for_next_agent": "<what the builder must know>"
}
```

`may_modify` is usually `[]`: list an existing test or test-config file only when the request cannot be met without changing it, one exact path per entry, no globs.

Both `artifacts` entries are the paths you ACTUALLY wrote, `_v2` suffix and all. Gates open these files — a name you meant to use fails them.
