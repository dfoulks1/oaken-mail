# SPDX-License-Identifier: Apache-2.0
"""The sync cursor: what core has been given, per folder, as a small JSON text.

For each folder: the ``UIDVALIDITY`` it had (a different one means every UID is meaningless),
the highest mod-sequence read (when the server keeps them), and the UIDs delivered, as ranges.
Comparing that set with the server's is how new and deleted mail are found, so an interrupted
sync resumes by itself: whatever is not in the set has not been delivered yet.
"""

import json
from dataclasses import dataclass, field

from oaken.core.logging import get_logger
from oaken_imap import uidset

log = get_logger("oaken.imap")


def provider_id(uid: int, validity: int, folder: str) -> str:
    """``42:1700000000:Lists/Dev``. The folder comes last because it may contain a colon."""
    return f"{uid}:{validity}:{folder}"


def parse_provider_id(pid: str) -> tuple[int, int, str] | None:
    uid, _, rest = pid.partition(":")
    validity, _, folder = rest.partition(":")
    if uid.isdigit() and validity.isdigit() and folder:
        return int(uid), int(validity), folder
    return None


@dataclass
class FolderState:
    validity: int
    modseq: int | None = None
    uids: set[int] = field(default_factory=set)

    def ids(self, folder: str) -> list[str]:
        return [provider_id(u, self.validity, folder) for u in sorted(self.uids)]


@dataclass
class Cursor:
    folders: dict[str, FolderState] = field(default_factory=dict)

    def dump(self) -> str:
        return json.dumps(
            {
                "v": 1,
                "f": {
                    name: {"v": s.validity, "m": s.modseq, "u": uidset.dump(s.uids)}
                    for name, s in sorted(self.folders.items())
                },
            },
            separators=(",", ":"),
        )

    @classmethod
    def parse(cls, text: str | None) -> "Cursor":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            if data["v"] != 1:
                raise ValueError("version")
            return cls(
                {
                    str(name): FolderState(
                        int(s["v"]),
                        int(s["m"]) if s.get("m") is not None else None,
                        uidset.parse(str(s["u"])),
                    )
                    for name, s in data["f"].items()
                }
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("imap cursor not understood; starting a full sync")
            return cls()
