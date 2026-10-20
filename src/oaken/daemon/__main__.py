# SPDX-License-Identifier: Apache-2.0
"""``python -m oaken.daemon``: run core in the foreground (what ``--detach`` starts)."""

import sys

from oaken.daemon.lifecycle import run

sys.exit(run())
