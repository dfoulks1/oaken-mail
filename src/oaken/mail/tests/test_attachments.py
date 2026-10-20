# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken.core.settings import AttachmentSettings
from oaken.mail import is_quarantined


@pytest.mark.parametrize(
    ("filename", "ctype", "flagged"),
    [
        ("invoice.pdf", "application/pdf", False),
        ("photo.jpg", "image/jpeg", False),
        ("notes.txt", "text/plain", False),
        ("setup.exe", "application/octet-stream", True),
        ("invoice.pdf.exe", "application/octet-stream", True),
        ("SCRIPT.JS", "text/javascript", True),
        ("budget.xlsm", "application/vnd.ms-excel.sheet.macroenabled.12", True),
        ("archive.zip", "application/zip", True),
        ("noname", "application/x-msdownload", True),
        ("invoice‮gpj.exe", "image/jpeg", True),
        (None, "application/pdf", False),
        ("README", "text/plain", False),
    ],
)
def test_default_quarantine_rules(filename: str | None, ctype: str, flagged: bool) -> None:
    assert is_quarantined(filename, ctype) is flagged


def test_quarantine_rules_are_configurable() -> None:
    cfg = AttachmentSettings(
        extra_quarantine_extensions=[".pdf"],
        extra_quarantine_types=["image/x-weird"],
        exempt_extensions=["zip"],
    )
    assert is_quarantined("a.pdf", "application/pdf", cfg)
    assert is_quarantined("a.png", "image/x-weird", cfg)
    assert not is_quarantined("a.zip", "application/octet-stream", cfg)
    assert is_quarantined("a.zip", "application/zip", cfg)  # the type rule still applies
