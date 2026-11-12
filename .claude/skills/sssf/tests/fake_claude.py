#!/usr/bin/env python3
"""A stand-in for the `claude` CLI that speaks its stream-json protocol.

It reads the prompt from stdin, works out which agent it is from the system
prompt, does that agent's job in the cwd, and ends with a `result` event whose
text is the agent's Report JSON. FAKE_CLAUDE_MODE changes the behaviour:

    breach       the agent also writes hack.txt and edits an adws/ module
    hang         print one event, then sleep forever (watchdog test)
    ratelimit    exit with a rejected rate_limit_event (resetsAt = now + 1)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

args = sys.argv[1:]
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
session = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "")
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd()}) + "\n")


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session})
if mode == "hang":
    time.sleep(3600)
if mode == "ratelimit":
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
          "resetsAt": int(time.time()) + 1, "rateLimitType": "five_hour"}})
    emit({"type": "result", "subtype": "success", "is_error": True,
          "result": "You've hit your limit", "session_id": session, "usage": {}})
    sys.exit(1)

handoff = re.search(r"### context_handoff_dir\s+(\S+)", prompt)
handoff = Path(handoff.group(1)) if handoff else Path(".")
adw_id = handoff.parent.name
cwd = Path.cwd()

if "Planner Agent" in system:
    (handoff / "plan.md").write_text("# plan\nadd feature file\n")
    (cwd / "specs").mkdir(exist_ok=True)
    spec = cwd / "specs" / f"{adw_id}_feature.md"
    spec.write_text("# plan\nadd feature file\n")
    report = {"status": "success", "summary": "planned", "artifacts": [str(handoff / "plan.md"), str(spec)],
              "commit_message": f"Add spec {adw_id}", "may_modify": json.loads(os.environ.get("FAKE_MAY_MODIFY", "[]"))}
elif "Builder Agent" in system:
    name = f"feature_{adw_id}.txt"
    (cwd / name).write_text(f"built by {adw_id}\n")
    if mode == "breach":
        (cwd / "hack.txt").write_text("pwned\n")
        (cwd / "adws" / "adw_modules" / "gates.py").write_text("# gutted\n")
    edit = os.environ.get("FAKE_EDIT_TEST")
    if edit:
        Path(cwd / edit).write_text("def test_add():\n    assert True\n")
    report = {"status": "success", "summary": "built", "changed_files": [name],
              "commit_message": f"Build {adw_id}"}
elif "Documenter Agent" in system:
    (cwd / "app_docs").mkdir(exist_ok=True)
    doc = cwd / "app_docs" / f"{adw_id}.md"
    doc.write_text("# change\n")
    report = {"status": "success", "summary": "documented", "artifacts": [str(doc)],
              "document_path": str(doc), "commit_message": f"Document {adw_id}"}
elif "Review" in system:
    report = {"status": "success", "summary": "reviewed", "approved": True,
              "findings": [{"requirement": "feature", "met": True}], "artifacts": []}
else:
    report = {"status": "success", "summary": "ok", "findings": []}

text = json.dumps(report)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
      "usage": {"input_tokens": 10, "output_tokens": 5}}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
      "session_id": session, "total_cost_usd": 0.001,
      "usage": {"input_tokens": 10, "output_tokens": 5},
      "modelUsage": {"claude-haiku-5-5": {"costUSD": 0.001, "contextWindow": 200000}}})
