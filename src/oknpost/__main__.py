# SPDX-License-Identifier: Apache-2.0
"""`python -m oknpost` runs the daemon in the foreground: it is what `oknpost start` runs in the background."""

import sys

from oknpost import main

sys.exit(main(["start", "--fg", *sys.argv[1:]]))
