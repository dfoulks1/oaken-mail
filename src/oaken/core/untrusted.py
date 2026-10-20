# SPDX-License-Identifier: Apache-2.0
"""Mail-derived text that must never be treated as an instruction.

This is the minimal type the plugin contracts need. Prompt 10 adds ``render`` (randomized
delimiters) and the prompt builder; until then ``unwrap`` is the only way to reach the text,
and only prompt-building code and tests should call it. There is deliberately no ``__str__``,
so the text cannot slip into an f-string or a log line by accident.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class UntrustedText:
    value: str = field(repr=False)

    def unwrap(self) -> str:
        """The raw text. Callers are responsible for keeping it out of system prompts."""
        return self.value

    def __str__(self) -> str:
        raise TypeError("UntrustedText has no implicit str(); build prompts with the builder")

    def __repr__(self) -> str:
        return f"UntrustedText(<{len(self.value)} chars>)"

    def __len__(self) -> int:
        return len(self.value)
