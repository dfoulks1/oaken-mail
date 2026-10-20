# SPDX-License-Identifier: Apache-2.0
"""Attachment metadata and the quarantine flag.

Oaken analyses email, not attachments. The parser records what an attachment is and where it
sits in the message (``part_path``); it never decodes or extracts the bytes. They already live
inside the raw message (in ``raw.db``, or in the LFS folder when the message is large), so they
are not copied anywhere. A risky attachment is flagged ``quarantine``: nothing should preview,
serve or extract it without a deliberate action.

``AttachmentExtractor`` is the interface for that deliberate action. Nothing in ingest calls it.
"""

from dataclasses import dataclass
from typing import Protocol

from oaken.core.settings import AttachmentSettings

# Extensions that run code, carry macros, or hide other files. Lower case, no dot.
QUARANTINE_EXTENSIONS = frozenset(
    "exe dll scr bat cmd com msi msp pif js jse vbs vbe wsf wsh ps1 psm1 jar apk app dmg iso img "
    "lnk hta cpl reg sh bin docm xlsm pptm dotm xltm potm xlam ppam sldm zip rar 7z gz tgz bz2 "
    "xz tar ace cab arj".split()
)
QUARANTINE_TYPES = frozenset(
    {
        "application/x-msdownload",
        "application/x-dosexec",
        "application/x-msdos-program",
        "application/x-executable",
        "application/java-archive",
        "application/x-sh",
        "application/x-bat",
        "application/vnd.microsoft.portable-executable",
        "application/x-msi",
        "application/hta",
        "application/zip",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/x-tar",
        "application/gzip",
        "application/vnd.ms-word.document.macroenabled.12",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-powerpoint.presentation.macroenabled.12",
    }
)
# A right-to-left override makes "invoice‮gpj.exe" display as "invoiceexe.jpg".
_DECEPTIVE = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    filename: str | None
    content_type: str
    size: int  # bytes as stored in the message; approximate for base64 and quoted-printable
    content_id: str | None
    inline: bool
    quarantine: bool
    part_path: str  # where the part sits: "0.1" is the second child of the root; see parse.py


def is_quarantined(
    filename: str | None, content_type: str, settings: AttachmentSettings | None = None
) -> bool:
    """Whether an attachment with this name and type is flagged. Judged from the name and the
    declared type only: nothing is opened."""
    cfg = settings or AttachmentSettings()
    ctype = content_type.lower()
    if ctype in QUARANTINE_TYPES or ctype in {t.lower() for t in cfg.extra_quarantine_types}:
        return True
    if not filename:
        return False
    if _DECEPTIVE & set(filename):
        return True
    exempt = {e.lower().lstrip(".") for e in cfg.exempt_extensions}
    risky = QUARANTINE_EXTENSIONS | {e.lower().lstrip(".") for e in cfg.extra_quarantine_extensions}
    if "." not in filename:
        return False
    ext = filename.lower().rsplit(".", 1)[1]  # "invoice.pdf.exe" is judged on "exe"
    return ext in risky and ext not in exempt


class AttachmentExtractor(Protocol):
    """Turns one attachment into text. Only ever called on explicit request."""

    def can_extract(self, info: AttachmentInfo) -> bool: ...

    def extract(self, info: AttachmentInfo, data: bytes) -> str: ...
