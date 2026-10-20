# SPDX-License-Identifier: Apache-2.0
"""Typed internal identifiers.

``MessageId``, ``ThreadId``, ``JobId`` and ``AgentId`` are UUIDv7 strings (time-ordered, so they
sort by creation). ``AccountId`` is a short slug chosen by the user or by account setup.
Provider-native ids are opaque strings and are never used as internal keys.
"""

import os
import time
import uuid
from typing import NewType

AccountId = NewType("AccountId", str)
MessageId = NewType("MessageId", str)
ThreadId = NewType("ThreadId", str)
JobId = NewType("JobId", str)
AgentId = NewType("AgentId", str)


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    """A UUIDv7 (RFC 9562): 48-bit millisecond timestamp, then random bits."""
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & (2**48 - 1)) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a
    value |= 0b10 << 62  # variant
    value |= rand & (2**62 - 1)  # rand_b
    return uuid.UUID(int=value)


def new_message_id() -> MessageId:
    return MessageId(str(uuid7()))


def new_thread_id() -> ThreadId:
    return ThreadId(str(uuid7()))


def new_job_id() -> JobId:
    return JobId(str(uuid7()))


def new_agent_id() -> AgentId:
    return AgentId(str(uuid7()))
