# SPDX-License-Identifier: Apache-2.0
"""Only the operations that ask for a read-state change may name the read-state service. The
registry, the gate, exposure and the plugin bridge decide nothing about read state: the handler
that marks mail read is the one place that does, after the gate has confirmed it."""

from pathlib import Path

import oaken.core.operations as pkg
import oaken.plugins as plugins

ROOT = Path(pkg.__file__).parent
ALLOWED = {ROOT / "builtin" / "mail.py", ROOT / "builtin" / "common.py"}
WORDS = ("ReadState", "need_readstate", "svc.readstate", ".mark_read(")


def test_only_the_mail_handlers_touch_read_state() -> None:
    offenders = []
    files = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and p not in ALLOWED]
    files += [p for p in Path(plugins.__file__).parent.rglob("*.py") if "tests" not in p.parts]
    for path in files:
        # Code only: a docstring may say what the registry does not do.
        text = "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith(("#", '"'))
        )
        for w in WORDS:
            # services.py only carries the handle; it never calls it.
            if w in text and path.name != "services.py":
                offenders.append(f"{path.name}: {w}")
    assert offenders == []
