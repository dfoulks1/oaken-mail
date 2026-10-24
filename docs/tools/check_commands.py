# SPDX-License-Identifier: Apache-2.0
"""Run the shell commands the docs promise work.

A fenced ``bash`` block that follows a ``<!-- check -->`` line is run, one command per line, in a
throwaway ``OAKEN_HOME`` with no network and no real mail. Every command must exit 0. A block
that needs a real account, Ollama or a running daemon is marked ``<!-- manual -->`` (or left
unmarked) and is never run. ``oaken ...`` is run as this checkout's own CLI, and a leading
``uv run`` is ignored, so the same block reads naturally in the docs.

``check_docs.check`` calls :func:`check` through the extension hook it has had since 01a.
"""

import os
import re
import shlex
import subprocess  # noqa: S404 (runs the docs' own commands, never a shell)
import sys
import tempfile
from pathlib import Path

from model import Config

MARK = "<!-- check -->"
FENCE = re.compile(r"^```(?:bash|sh|shell)\s*$")
TIMEOUT = 60


def blocks(text: str) -> list[tuple[int, list[str]]]:
    """``(line number, commands)`` for every ``<!-- check -->`` block in ``text``."""
    lines = text.splitlines()
    found: list[tuple[int, list[str]]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == MARK:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and FENCE.match(lines[j].strip()):
                k = j + 1
                cmds: list[str] = []
                while k < len(lines) and not lines[k].startswith("```"):
                    line = lines[k].strip()
                    if line and not line.startswith("#"):
                        cmds.append(line)
                    k += 1
                found.append((j + 1, cmds))
                i = k
        i += 1
    return found


def argv_for(command: str) -> list[str]:
    """The command as a list, with ``oaken`` meaning this checkout's CLI and any trailing
    ``# comment`` dropped."""
    words = shlex.split(command, comments=True)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    if words and words[0] == "oaken":
        words = [sys.executable, "-m", "oaken_cli", *words[1:]]
    return words


def run(command: str, home: Path) -> str | None:
    """``None`` if the command exits 0, else why it did not."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OAKEN_")}
    env.update(OAKEN_HOME=str(home), OAKEN_STATE_DIR=str(home / "state"))
    try:
        done = subprocess.run(  # noqa: S603 (the docs' own command, no shell)
            argv_for(command),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
        return f"exit {done.returncode}: {tail[0]}"
    return None


def check(cfg: Config) -> list[str]:
    """One problem per command that failed, naming the file and line."""
    problems: list[str] = []
    for path in sorted(cfg.docs_dir.rglob("*.md")):
        rel = path.relative_to(cfg.docs_dir).as_posix()
        if rel.startswith(("prompts/", "tools/")):
            continue
        for line, commands in blocks(path.read_text(encoding="utf-8")):
            with tempfile.TemporaryDirectory(prefix="oaken-cmd-") as tmp:
                for command in commands:
                    why = run(command, Path(tmp))
                    if why is not None:
                        problems.append(f"docs/{rel}:{line}: `{command}` failed ({why})")
    return problems
