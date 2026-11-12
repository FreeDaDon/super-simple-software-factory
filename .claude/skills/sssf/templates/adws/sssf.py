#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""Trace queries and run control, standard library only (no sqlite3 CLI needed).

Usage:
    uv run adws/sssf.py query "<sql>"      print rows, tab-separated
    uv run adws/sssf.py kill <adw_id>      stop a run's live processes (agents first)

The db path comes from SSSF_DB, default adws/adw_data/sssf.db.
"""

import os
import signal
import sqlite3
import subprocess
import sys

DB = os.environ.get("SSSF_DB", "adws/adw_data/sssf.db")


def connect() -> sqlite3.Connection:
    if not os.path.exists(DB):
        raise SystemExit(f"no trace db at {DB} — run an ADW first (just demo)")
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def query(sql: str) -> int:
    for row in connect().execute(sql):
        print("\t".join("" if v is None else str(v) for v in row))
    return 0


def _command_of(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True)
    return result.stdout.strip()


def kill(adw_id: str) -> int:
    rows = connect().execute(
        "SELECT kind, name, pid, command FROM processes WHERE adw_id=? AND ended_at IS NULL "
        "ORDER BY kind='adw', id", (adw_id,)).fetchall()
    if not rows:
        print(f"{adw_id}: nothing recorded as running")
        return 0
    for kind, name, pid, command in rows:
        live = _command_of(pid)
        # A recycled pid belongs to someone else now: only kill what still looks like ours.
        expected = "claude" if kind == "agent" else (command or "").split(" ")[0]
        if not live or (expected and expected not in live):
            print(f"skip {kind} {name or ''} pid {pid}: not running as {expected!r} any more")
            continue
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {kind} {name or ''} pid {pid}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("query", "kill"):
        raise SystemExit(__doc__)
    sys.exit(query(sys.argv[2]) if sys.argv[1] == "query" else kill(sys.argv[2]))
