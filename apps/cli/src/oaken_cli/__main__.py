# SPDX-License-Identifier: Apache-2.0
"""Run the CLI as ``python -m oaken_cli``. The console script ``oaken`` is the normal entry point;
this mirror is handy for ``python -m`` invocation and for measuring startup without the wrapper."""

from __future__ import annotations

from oaken_cli.main import _console_main

if __name__ == "__main__":
    _console_main()
