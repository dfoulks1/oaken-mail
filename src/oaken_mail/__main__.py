# SPDX-License-Identifier: Apache-2.0
"""`python -m oaken_mail` runs the daemon in the foreground: it is what `oaken-mail start` runs in the background."""

import sys

from oaken_mail import main

sys.exit(main(["start", "--fg", *sys.argv[1:]]))
