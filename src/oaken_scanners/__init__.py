# SPDX-License-Identifier: Apache-2.0
"""``oaken-scanners``: attachment scanners, registered in the ``oaken.actions`` group.

A scanner is an action on the ``scan`` route (ADR 0020): core opens each quarantined attachment
0400 only inside one block, the scanner says ``clean``, ``infected`` or ``error``, and only a
``clean`` verdict ever lets the ``attachment`` action release the file. With no scanner installed
nothing is released.

``clamav`` scans with ClamAV's ``clamd`` daemon and works only while clamd is running (it talks to
clamd, it does not run ClamAV itself). Core does not import this package. It is opt-in
(``enabled_default = False``); turn it on with ``[plugins.action.clamav] enabled = true``.
"""

from oaken_scanners.clamav import ClamavScanner

__all__ = ["ClamavScanner"]
