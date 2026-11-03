# SPDX-License-Identifier: Apache-2.0
"""``oaken-triagers``: the first-party triagers, registered in the ``oaken.triagers`` group.

``spamcheck`` (the built-in header rules) and ``rules`` (the reader's own rules) are
deterministic; ``ai`` asks a model through ``ctx.llm``, never directly. Core does not import this
package. Each can be switched off with ``[plugins.triager.<name>] enabled = false``.
"""

from oaken_triagers.triage import AITriager, SpamCheckTriager
from oaken_triagers.triage_custom import CustomRulesTriager

__all__ = ["AITriager", "CustomRulesTriager", "SpamCheckTriager"]
