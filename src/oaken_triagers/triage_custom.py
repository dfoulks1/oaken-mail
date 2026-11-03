# SPDX-License-Identifier: Apache-2.0
"""``rules``: the reader's own deterministic triage rules.

Each rule is a ``when`` (every key must match) and the level and category it gives. The first rule
that matches, in order, settles triage with no model. Core stores the rules and levels (so they can
be edited and reordered while it runs) and hands them over as this plugin's config; the plugin only
matches. A rule's ``when`` keys are a closed set, listed in ``WHEN_KEYS``.
"""

from email.utils import getaddresses
from fnmatch import fnmatchcase
from pydantic import BaseModel, ConfigDict, Field, model_validator

from oaken.plugins.base import BaseTriager
from oaken.plugins.contracts import CleanMessage, TriageVerdict
from oaken.plugins.kit import header_values
from oaken.core.triage import Category, TriageResult

# What a ``when`` may say, for the help text of the wizard and the CLI.
WHEN_KEYS: dict[str, str] = {
    "from": "sender contains this text, or matches a glob such as *@boss.com",
    "to": "a recipient contains this text, or matches a glob",
    "subject": "subject matches a glob (case does not matter)",
    "subject_contains": "subject contains this text",
    "body_contains": "body contains this text",
    "header_present": "this header exists (for example list-id)",
    "has_attachment": "true: has an attachment; false: has none",
    "is_reply": "true: a reply; false: not a reply",
}


class When(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sender: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    subject_contains: str | None = None
    body_contains: str | None = None
    header_present: str | None = None
    has_attachment: bool | None = None
    is_reply: bool | None = None

    @model_validator(mode="after")
    def _says_something(self) -> "When":
        if not self.model_dump(exclude_none=True):
            raise ValueError(f"a rule needs at least one condition ({', '.join(WHEN_KEYS)})")
        return self


class Rule(BaseModel):
    uid: str = ""
    name: str
    enabled: bool = True
    level: int = Field(ge=0, le=99)
    category: Category
    when: When
    route: list[str] = Field(default_factory=list)


class CustomRulesConfig(BaseModel):
    """Set by core from its tables, not by hand in ``config.toml``. Strict, so a leftover
    ``[plugins.triager.rules]`` from before the rename (``skip_rules`` and the like, now under
    ``[plugins.triager.spamcheck]``) is reported, not silently ignored."""

    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(default_factory=list)


def _glob_or_text(pattern: str, candidates: list[str]) -> bool:
    p = pattern.lower()
    if any(c in p for c in "*?["):
        return any(fnmatchcase(c.lower(), p) for c in candidates)
    return any(p in c.lower() for c in candidates)


def _people(value: str) -> list[str]:
    return [value, *(addr for _, addr in getaddresses([value]) if addr)]


def matches(msg: CleanMessage, when: When) -> bool:
    headers = header_values(msg)
    checks: list[bool] = []
    if when.sender is not None:
        checks.append(_glob_or_text(when.sender, _people(msg.headers["from"].unwrap())
                                    if "from" in msg.headers else []))
    if when.to is not None:
        checks.append(_glob_or_text(when.to, [p for v in headers.get("to", ()) for p in _people(v)]))
    if when.subject is not None:
        checks.append(fnmatchcase(msg.subject.unwrap().lower(), when.subject.lower()))
    if when.subject_contains is not None:
        checks.append(when.subject_contains.lower() in msg.subject.unwrap().lower())
    if when.body_contains is not None:
        checks.append(when.body_contains.lower() in msg.body.unwrap().lower())
    if when.header_present is not None:
        checks.append(when.header_present.lower() in headers)
    if when.has_attachment is not None:
        checks.append(bool(msg.attachments) is when.has_attachment)
    if when.is_reply is not None:
        checks.append(msg.is_reply is when.is_reply)
    return all(checks)


def first_match(msg: CleanMessage, rules: list[Rule]) -> Rule | None:
    return next((r for r in rules if r.enabled and matches(msg, r.when)), None)


class CustomRulesTriager(BaseTriager):
    """Runs before the built-in header rules (rank 5 against 10), so a rule of the reader's wins
    over a spam or bulk header: mail they said matters is not skipped."""

    version = "1"
    rank = 5
    output_model = TriageResult
    config_model = CustomRulesConfig

    async def triage(self, msg: CleanMessage) -> TriageVerdict | None:
        cfg = self.config if isinstance(self.config, CustomRulesConfig) else CustomRulesConfig()
        rule = first_match(msg, cfg.rules)
        if rule is None:
            return None
        return TriageVerdict(
            TriageResult(
                category=rule.category,
                priority=rule.level,
                needs_reply=False,
                waiting_on_others=False,
                reason=f"Decided by custom rule {rule.name}.",
                source="custom",
                rule=rule.name,
                route=rule.route,
            ).model_dump(mode="json")
        )
