#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run one check and print a short answer: ``NAME: PASS`` or ``NAME: FAIL`` with the last lines. The full output is kept in .qa/NAME.log.

usage: run_check.py NAME [--tail N] -- COMMAND [ARGS...]
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if "--" not in argv or not argv or argv[0] == "--":
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    name, opts, cmd = argv[0], argv[1:split], argv[split + 1 :]
    tail = int(opts[opts.index("--tail") + 1]) if "--tail" in opts else 25
    if not cmd:
        print(__doc__, file=sys.stderr)
        return 2
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603 (our own fixed commands)
    took = time.monotonic() - started
    log = ROOT / ".qa" / f"{name}.log"
    log.parent.mkdir(exist_ok=True)
    out = proc.stdout + proc.stderr
    log.write_text(out)
    if proc.returncode == 0:
        print(f"{name}: PASS ({took:.1f}s)")
    else:
        print(f"{name}: FAIL (exit {proc.returncode}, {took:.1f}s), last {tail} lines, full log {log.relative_to(ROOT)}")
        print("\n".join(out.splitlines()[-tail:]))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
