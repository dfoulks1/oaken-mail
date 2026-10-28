#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Shared runner for oaken-trunk dev checks.

Usage (from the repo root, via uv run):
    uv run scripts/run_check.py <dotfile> <label> -- <step_label> <cmd...> [-- <step_label> <cmd...> ...]

Each "--"-delimited group after the leading "--" is one step:
    step_label  human-readable name printed as "==> <label>"
    cmd...      the command + args to run

The runner:
  - reads <dotfile> (JSON) before running and prints time-since-last-run
  - streams each step to stdout (live output)
  - captures each step's output for the dotfile
  - on success writes <dotfile> with sha, timestamp, and per-step output
  - exits non-zero if any step fails (output is still written for the steps that ran)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _elapsed_str(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _git_tree() -> str:
    """Root tree SHA for HEAD — changes whenever any tracked file changes."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _read_lastrun(dotfile: Path) -> dict | None:
    try:
        return json.loads(dotfile.read_text())
    except Exception:
        return None


def _write_lastrun(dotfile: Path, data: dict) -> None:
    dotfile.write_text(json.dumps(data, indent=2) + "\n")


def _run_step(label: str, cmd: list[str]) -> tuple[bool, str]:
    """Run one step, stream to stdout, return (ok, combined_output)."""
    print(f"==> {label}")
    sys.stdout.flush()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            sys.stdout.flush()
            lines.append(line)
        proc.wait()
        ok = proc.returncode == 0
    except FileNotFoundError as exc:
        msg = f"command not found: {exc}\n"
        print(msg, end="")
        lines.append(msg)
        ok = False
    return ok, "".join(lines)


# ---------------------------------------------------------------------------
# arg parsing  (no argparse — stdlib only, keep it simple)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> tuple[Path, str, list[tuple[str, list[str]]]]:
    """
    argv layout:
        dotfile  label  --  step_label cmd...  [--  step_label cmd...  ...]
    """
    if len(argv) < 4 or argv[2] != "--":
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    dotfile = Path(argv[0])
    label = argv[1]
    rest = argv[3:]  # everything after the first "--"

    steps: list[tuple[str, list[str]]] = []
    current: list[str] = []
    for token in rest:
        if token == "--":
            if current:
                steps.append((current[0], current[1:]))
            current = []
        else:
            current.append(token)
    if current:
        steps.append((current[0], current[1:]))

    if not steps:
        print("error: no steps provided", file=sys.stderr)
        sys.exit(2)

    return dotfile, label, steps


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(dotfile: Path, label: str, steps: list[tuple[str, list[str]]]) -> None:
    """Run a named check, writing results to dotfile. Exits non-zero on failure."""

    # -- show last run ---------------------------------------------------------
    last = _read_lastrun(dotfile)
    if last:
        elapsed = int(time.time()) - int(last.get("timestamp", 0))
        sha = last.get("sha", "?")
        print(f"==> last {label}: {sha} ({_elapsed_str(elapsed)} ago)")

        # early exit if nothing has changed since the last passing run
        if last.get("passed") and last.get("tree") and last["tree"] == _git_tree():
            print(f"==> no changes since last run, skipping.")
            sys.exit(0)

    # -- run steps -------------------------------------------------------------
    results: list[dict] = []
    failed = False
    for step_label, cmd in steps:
        ok, output = _run_step(step_label, cmd)
        results.append({"step": step_label, "cmd": cmd, "ok": ok, "output": output})
        if not ok:
            failed = True
            break

    # -- write dotfile ---------------------------------------------------------
    _write_lastrun(dotfile, {
        "label": label,
        "sha": _git_sha(),
        "tree": _git_tree(),
        "timestamp": int(time.time()),
        "passed": not failed,
        "steps": results,
    })

    # -- finish ----------------------------------------------------------------
    if failed:
        failed_step = results[-1]["step"]
        print(f"\n{label}: FAILED at '{failed_step}'", file=sys.stderr)
        sys.exit(1)

    print(f"\nAll {label} checks passed.")


def main() -> None:
    dotfile, label, steps = _parse_args(sys.argv[1:])
    run(dotfile, label, steps)


if __name__ == "__main__":
    # chdir to repo root regardless of where the script lives
    os.chdir(Path(__file__).parent.parent)
    main()
