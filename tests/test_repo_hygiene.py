# SPDX-License-Identifier: Apache-2.0
"""Guards that keep databases, mail and secrets out of version control."""

import fnmatch
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    "*.db",
    "*.db-*",
    "*.sqlite*",
    "*.eml",
    "*.mbox",
    ".env",
    ".env.*",
    "token*.json",
    "credentials*.json",
    "client_secret*.json",
]
ALLOWED = {".env.example"}
# Hand-written, synthetic mail on reserved example domains (see the 06 fixtures). Real mail is
# still refused everywhere else, and anything else under these folders is still checked.
SYNTHETIC_MAIL_DIRS = ("src/oaken/mail/tests/fixtures/",)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "ls-files"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.splitlines()


def test_no_sensitive_files_tracked() -> None:
    bad = [
        p
        for p in _tracked()
        if Path(p).name not in ALLOWED
        and not (p.endswith(".eml") and p.startswith(SYNTHETIC_MAIL_DIRS))
        and any(fnmatch.fnmatch(Path(p).name, pat) for pat in FORBIDDEN)
    ]
    assert not bad, f"sensitive files are tracked: {bad}"


def test_gitignore_covers_patterns() -> None:
    text = (ROOT / ".gitignore").read_text()
    for pat in FORBIDDEN:
        assert pat in text, f".gitignore is missing {pat}"
    assert "site/" in text or "/site" in text
