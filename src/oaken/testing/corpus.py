# SPDX-License-Identifier: Apache-2.0
"""A synthetic mail corpus for tests and benchmarks. Nothing here is real mail: every address is
on a reserved example domain and every word comes from a fixed list."""

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

_WORDS = (
    "agenda budget schedule review update invoice meeting report project release draft "
    "customer deadline proposal feedback summary contract question follow notes quarter "
    "planning status travel lunch design server backup policy training survey ticket"
).split()
_NAMES = ("alex", "sam", "jordan", "casey", "robin", "taylor", "morgan", "riley")


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(n)).capitalize() + "."


def synthetic_message(index: int, *, seed: int = 0, body_paragraphs: int = 6) -> bytes:
    """One RFC 822 message. Deterministic for a given ``(index, seed)``."""
    rng = random.Random(seed * 1_000_003 + index)  # noqa: S311 (reproducible test data)
    sender, rcpt = rng.sample(_NAMES, 2)
    when = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=index * 7)
    body = "\n\n".join(_sentence(rng, rng.randint(8, 30)) for _ in range(body_paragraphs))
    quoted = "\n".join("> " + _sentence(rng, 12) for _ in range(rng.randint(0, 4)))
    text = (
        f"From: {sender.title()} <{sender}@example.com>\r\n"
        f"To: {rcpt.title()} <{rcpt}@example.org>\r\n"
        f"Subject: {_sentence(rng, 4)[:-1]}\r\n"
        f"Date: {when:%a, %d %b %Y %H:%M:%S +0000}\r\n"
        f"Message-ID: <{index}.{seed}@example.com>\r\n"
        "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}\n\n{quoted}\n"
    )
    return text.encode()


def synthetic_corpus(count: int = 1000, *, seed: int = 0) -> Iterator[tuple[str, bytes]]:
    """``(provider_id, raw)`` pairs, ``count`` of them."""
    for i in range(count):
        yield f"msg-{i}", synthetic_message(i, seed=seed)
