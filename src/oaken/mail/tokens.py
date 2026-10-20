# SPDX-License-Identifier: Apache-2.0
"""Token counting. Counting is deterministic, local and cheap (far cheaper than a model pass);
it never returns text, so it cannot carry an instruction anywhere.

Two implementations: ``EstimateTokenizer`` (no files, always available) and ``FileTokenizer``
(exact, from a local Hugging Face ``tokenizer.json``). Nothing is downloaded.
"""

import re
from pathlib import Path
from typing import Protocol

from oaken.core.errors import ConfigError
from oaken.core.settings import MailSettings

_PIECE = re.compile(r"[^\W_]+|[^\w\s]|_", re.UNICODE)
_CJK = re.compile(r"[⺀-鿿가-힯＀-￯]")


class Tokenizer(Protocol):
    name: str
    exact: bool  # False means ``count`` is an estimate

    def count(self, text: str) -> int: ...


class EstimateTokenizer:
    """About one token per short word or punctuation mark, one more per seven characters of a
    long word, and one per CJK character. Close enough for budgeting; labelled an estimate."""

    name = "estimate"
    exact = False

    def count(self, text: str) -> int:
        total = 0
        for piece in _PIECE.findall(text):
            if _CJK.match(piece):
                total += len(piece)
            else:
                total += 1 + len(piece) // 7
        return total


class FileTokenizer:
    """Exact counts from a local ``tokenizer.json`` (the ``tokenizers`` package, offline)."""

    exact = True

    def __init__(self, path: Path) -> None:
        from tokenizers import Tokenizer as HFTokenizer

        try:
            self._tok = HFTokenizer.from_file(str(path))
        except Exception as exc:  # noqa: BLE001 - the library raises a bare Exception
            raise ConfigError(f"cannot load tokenizer file {path}: {exc}") from exc
        self.name = f"file:{path.name}"

    def count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids)


def load_tokenizer(settings: MailSettings) -> Tokenizer:
    """The configured tokenizer, or the estimate when none is configured."""
    if settings.tokenizer_file is None:
        return EstimateTokenizer()
    path = Path(settings.tokenizer_file).expanduser()
    if not path.is_file():
        raise ConfigError(f"mail.tokenizer_file does not exist: {path}")
    return FileTokenizer(path)
