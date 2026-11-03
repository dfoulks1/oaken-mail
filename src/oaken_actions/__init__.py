# SPDX-License-Identifier: Apache-2.0
"""``oaken-actions``: the first-party actions, registered in the ``oaken.actions`` group.

``attachment`` saves attachments off a message and quarantines the risky ones. Core does not import
this package. Switch it off with ``[plugins.action.attachment] enabled = false``.
"""

from oaken_actions.attachment import AttachmentAction

__all__ = ["AttachmentAction"]
