# SPDX-License-Identifier: Apache-2.0
"""The quarantine folder: where attachments flagged as risky are put, and never opened.

Core only ever *writes* here. It never reads a quarantined file back, never parses it and never
hands it to a model; the user decides what to do with it (usually: delete it). The folder and
the folders inside it are owner-only (0700, so the owner can list them) and every file is 0600:
no execute bit, and the name ends in ``.quarantined`` so a file manager does not open it by
double-click. On Windows, where POSIX modes do not apply, the same layout is used and the user's
profile permissions protect it.
"""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from oaken.core.errors import StorageError
from oaken.core.ids import MessageId
from oaken.core.logging import get_logger
from oaken.core.paths import data_dir
from oaken.core.settings import AttachmentSettings

log = get_logger("oaken.quarantine")

SUFFIX = ".quarantined"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_MAX = 80


def safe_name(filename: str | None, part_path: str) -> str:
    """A file name that cannot escape the folder, hide itself or mean anything to a shell."""
    stem = _UNSAFE.sub("_", (filename or "").strip()).strip("._") or "attachment"
    return f"{part_path.replace('.', '-')}_{stem[:_NAME_MAX]}{SUFFIX}"


@dataclass(frozen=True)
class QuarantineStats:
    files: int
    bytes: int


class Quarantine:
    def __init__(self, root: Path, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.root = root
        self.max_bytes = max_bytes

    def ensure(self) -> Path:
        """Create the folder (owner-only) if needed."""
        _private_dir(self.root)
        return self.root

    def path_for(self, message_id: MessageId, filename: str | None, part_path: str) -> Path:
        return self.root / _UNSAFE.sub("_", message_id) / safe_name(filename, part_path)

    def put(
        self, message_id: MessageId, filename: str | None, part_path: str, data: bytes
    ) -> Path | None:
        """Write one attachment. Returns its path, or None when it is over ``max_bytes`` (it stays
        in the message, which is kept whole in ``raw.db``). Idempotent: a file already there with
        the same size is left alone."""
        if len(data) > self.max_bytes:
            log.warning("attachment too large to quarantine", message_id=message_id, size=len(data))
            return None
        target = self.path_for(message_id, filename, part_path)
        self.ensure()
        _private_dir(target.parent)
        if target.exists() and target.stat().st_size == len(data):
            return target
        tmp = target.with_name(target.name + ".part")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError as exc:
            raise StorageError(f"could not quarantine an attachment: {exc.strerror}") from exc
        _chmod(target, 0o600)
        return target

    def stats(self) -> QuarantineStats:
        files = total = 0
        if self.root.is_dir():
            for p in self.root.rglob("*"):
                if p.is_file():
                    files += 1
                    total += p.stat().st_size
        return QuarantineStats(files, total)

    def problems(self) -> list[str]:
        """Permissions that are looser than they should be (a check, not a repair). Reads only
        directory entries and file modes, never file contents."""
        if os.name == "nt" or not self.root.exists():
            return []
        found: list[str] = []
        for p in [self.root, *self.root.rglob("*")]:
            mode = stat.S_IMODE(p.lstat().st_mode)
            if p.is_dir() and mode & 0o077:
                found.append(f"{p.name or p}: folder is open to other users ({mode:o})")
            elif p.is_file() and mode & 0o177:
                found.append(f"{p.name}: file has loose permissions ({mode:o})")
        return found


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except (OSError, NotImplementedError):  # e.g. Windows
        log.debug("could not set permissions", path=path.name)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(path, 0o700)


def quarantine_dir(settings: AttachmentSettings) -> Path:
    """The configured folder: absolute as given, else under the data folder."""
    p = Path(settings.quarantine_dir).expanduser()
    return p if p.is_absolute() else data_dir() / p
