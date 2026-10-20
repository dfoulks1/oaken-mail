# SPDX-License-Identifier: Apache-2.0
"""Which folders are synced, and which folder plays which part (Trash, Junk, Archive).

The server's own word comes first: the special-use attributes of RFC 6154. A server that does not
send them gets a guess from the usual names, so ``Deleted Items`` is still Trash. Every folder is a
label; ``INBOX`` is simply ``INBOX``.
"""

import re

from oaken_imap.config import ImapConfig
from oaken_imap.session import Folder

ROLES = {
    "trash": ("\\trash", {"trash", "deleted items", "deleted messages", "deleted"}),
    "junk": ("\\junk", {"junk", "spam", "junk e-mail", "junk email", "bulk mail"}),
    "archive": ("\\archive", {"archive", "archives"}),
}
NEVER_SYNCED = ("trash", "junk")


def _leaf(name: str) -> str:
    return re.split(r"[/.]", name)[-1].strip().lower()


def role_of(folder: Folder, everyone: list[Folder]) -> str | None:
    """``trash``, ``junk``, ``archive`` or None. A role the server names explicitly on some
    folder is never guessed for another."""
    for role, (attr, names) in ROLES.items():
        if attr in folder.attrs:
            return role
        if not any(attr in f.attrs for f in everyone) and _leaf(folder.name) in names:
            if folder.name.upper() != "INBOX":
                return role
    return None


def find(everyone: list[Folder], role: str, configured: str | None) -> Folder | None:
    """The folder that plays ``role``: the one the user named, else the server's."""
    if configured:
        return next((f for f in everyone if f.name.lower() == configured.lower()), None)
    return next((f for f in everyone if role_of(f, everyone) == role), None)


def to_sync(everyone: list[Folder], config: ImapConfig) -> list[Folder]:
    """The folders to mirror: selectable, not Trash or Junk, inside the user's include list and
    outside the exclude list. INBOX first, the rest by name."""
    want = {n.lower() for n in config.include_folders}
    skip = {n.lower() for n in config.exclude_folders}
    chosen = [
        f
        for f in everyone
        if f.selectable
        and role_of(f, everyone) not in NEVER_SYNCED
        and f.name.lower() not in skip
        and (not want or f.name.lower() in want)
    ]
    return sorted(chosen, key=lambda f: (f.name.upper() != "INBOX", f.name.lower()))
