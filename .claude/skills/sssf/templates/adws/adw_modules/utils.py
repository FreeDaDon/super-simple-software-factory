"""Small shared helpers. Anything bigger belongs in its own module."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def operator_env() -> dict[str, str]:
    """The engineer's own environment, as their shell would hand it over.

    Agents and quality blocks are meant to see exactly what the operator sees:
    their PATH, their toolchains, their globally installed packages. Copying
    os.environ gets almost all the way there — but ADWs launch under `uv run`,
    which prepends its ephemeral venv's bin to PATH and sets VIRTUAL_ENV. That
    venv holds the ADW's OWN dependencies (pydantic, pyyaml), not the
    operator's, so anything a subprocess resolves through it — `python3`,
    `pip`, every globally pip-installed CLI — silently becomes the wrong one.

    Stripping the venv restores parity: `python3` in an agent's bash is the
    same `python3` the engineer gets in their terminal. The ADW's own imports
    are unaffected; this env is only ever handed to child processes.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", "")
    if not venv:
        return env
    venv_bin = str(Path(venv) / "bin")
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != venv_bin]
    env["PATH"] = os.pathsep.join(parts)
    return env


# Names of environment variables that hold credentials. Quality commands run
# code an agent just wrote, so they get the operator's environment WITHOUT these:
# a test file is all it takes to read os.environ and print it into a log.
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_PAT$|^AWS_|^GH_|"
                        r"^GITHUB_|^ANTHROPIC_|^OPENAI_|^OPENROUTER_|DATABASE_URL|_DSN$)",
                        re.IGNORECASE)


def scrubbed_env() -> dict[str, str]:
    """operator_env() minus every variable whose name says it is a credential."""
    return {k: v for k, v in operator_env().items() if not SECRET_ENV.search(k)}


# Secret-shaped values, replaced before anything reaches the trace (JSONL, db,
# raw agent output). Agents read files; what they read rides back in tool
# results, and the trace is shown by a web UI.
_SECRET_VALUES = [
    # Body limited to base64 plus (escaped) newlines, so inside serialized JSON
    # the match stops at the closing quote and the document stays valid.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*(-----END [A-Z ]*PRIVATE KEY-----)?"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
    # KEY=value / "token": "value" — the shape of a .env line or a config entry.
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)"
               r"(\\?[\"']?\s*[=:]\s*\\?[\"']?)(?=[^\s\"'\\]*[A-Za-z])([^\s\"'\\]{8,})"),
]
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    for pattern in _SECRET_VALUES[:-1]:
        text = pattern.sub(REDACTED, text)
    return _SECRET_VALUES[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def new_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_prompt(arg: str) -> str:
    """CLI prompt arg: a file path resolves to its contents, else inline text."""
    try:
        p = Path(arg)
        if p.is_file():
            return p.read_text()
    except OSError:
        pass
    return arg


def engineer_name() -> str:
    name = os.environ.get("ENGINEER_NAME", "").strip()
    if name:
        return name
    try:
        out = subprocess.run(["git", "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return os.environ.get("USER", "engineer")
