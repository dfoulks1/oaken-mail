# SPDX-License-Identifier: Apache-2.0
"""Run compiled scripts against one message and report what they decided.

The interpreter never delivers, sends or deletes anything. It returns a ``SieveResult``:
a disposition (inbox, filed or discarded), and effects (labels, flags, suppressed intents and an
analysis marker), each with the ``script:line`` that caused it.
"""

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Final, Literal

from oaken.core.settings import SieveSettings
from oaken.mail.sieve.match import (
    compiled_regex,
    fold,
    glob_match,
    glob_tokens,
    numeric,
)
from oaken.mail.sieve.parser import Arg, Node, SieveError
from oaken.mail.sieve.script import Script

Disposition = Literal["inbox", "filed", "discarded"]
Kind = Literal["label", "flag", "intent", "analysis"]

_SYSTEM_FLAGS: Final = {
    "\\seen": "\\Seen",
    "\\answered": "\\Answered",
    "\\flagged": "\\Flagged",
    "\\deleted": "\\Deleted",
    "\\draft": "\\Draft",
}
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*|[0-9]+)\}")
_MAILBOX_BAD = re.compile(r"[\x00-\x1f\x7f]")
_INTENTS: Final = ("redirect", "reject", "ereject", "vacation")


@dataclass(frozen=True, slots=True)
class Envelope:
    """The SMTP envelope, as far as it can be known: a provider does not hand it over, so it is
    derived from ``Return-Path`` and ``Delivered-To`` style headers."""

    sender: str | None = None
    recipient: str | None = None


@dataclass(slots=True)
class MessageView:
    """What a script may look at: headers, envelope, size and the body text. Never attachments."""

    headers: Mapping[str, tuple[str, ...]]
    envelope: Envelope
    size: int
    now: datetime
    body_text: Callable[[], str]
    _body: str | None = field(default=None, repr=False)

    def body(self) -> str:
        if self._body is None:
            self._body = self.body_text()
        return self._body


@dataclass(frozen=True, slots=True)
class Effect:
    kind: Kind
    value: str
    rule: str  # "script.sieve:LINE"


@dataclass(frozen=True, slots=True)
class SieveResult:
    disposition: Disposition
    disposition_rule: str  # the rule that decided it, or "implicit-keep"
    effects: tuple[Effect, ...]
    errors: tuple[str, ...]  # scripts that failed; their actions were dropped
    steps: int

    def of(self, kind: Kind) -> tuple[Effect, ...]:
        return tuple(e for e in self.effects if e.kind == kind)


class _Stop(Exception):  # noqa: N818 - control flow, not an error
    pass


@dataclass(slots=True)
class _State:
    effects: list[Effect] = field(default_factory=list)
    keep: str | None = None  # rule of an explicit keep
    filed: str | None = None  # rule of the first fileinto
    discard: str | None = None
    flags: list[str] = field(default_factory=list)
    steps: int = 0


def run(
    scripts: Sequence[Script], view: MessageView, settings: SieveSettings | None = None
) -> SieveResult:
    """Run each script in order. ``stop`` ends them all. A script that fails (a bad regular
    expression built from a variable, a limit) loses its own actions and nothing else."""
    cfg = settings or SieveSettings()
    state = _State()
    errors: list[str] = []
    for script in scripts:
        saved = copy.deepcopy(state)
        try:
            _Run(script, view, cfg, state).block(script.nodes)
        except _Stop:
            break
        except SieveError as exc:
            errors.append(f"{script.name}: {exc.message}")
            steps = state.steps
            state = saved
            state.steps = steps
    return _result(state, tuple(errors))


def _result(state: _State, errors: tuple[str, ...]) -> SieveResult:
    effects = list(state.effects)
    if state.discard is not None and state.keep is None and state.filed is None:
        disposition: Disposition = "discarded"
        rule = state.discard
    elif state.keep is not None:
        disposition, rule = "inbox", state.keep
    elif state.filed is not None:
        disposition, rule = "filed", state.filed
    else:
        disposition, rule = "inbox", "implicit-keep"
    if disposition != "discarded":
        for flag in state.flags:
            effects.append(Effect("flag", flag, rule))
    return SieveResult(disposition, rule, tuple(_unique(effects)), errors, state.steps)


def _unique(effects: list[Effect]) -> list[Effect]:
    seen: set[tuple[str, str]] = set()
    out: list[Effect] = []
    for e in effects:
        key = (e.kind, e.value.casefold() if e.kind in ("label", "flag") else e.value)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


class _Run:
    def __init__(self, script: Script, view: MessageView, cfg: SieveSettings, state: _State):
        self.script, self.view, self.cfg, self.state = script, view, cfg, state
        self.vars: dict[str, str] = {}
        self.match_vars: list[str] = []

    # -- plumbing -----------------------------------------------------------------------
    def rule(self, node: Node) -> str:
        return f"{self.script.name}:{node.line}"

    def tick(self, n: int = 1) -> None:
        self.state.steps += n
        if self.state.steps > self.cfg.max_steps:
            raise SieveError(f"step limit ({self.cfg.max_steps}) reached")

    def emit(self, kind: Kind, value: str, node: Node) -> None:
        if len(self.state.effects) >= self.cfg.max_actions:
            raise SieveError(f"more than {self.cfg.max_actions} actions")
        self.state.effects.append(Effect(kind, value, self.rule(node)))

    def strings(self, arg: Arg) -> tuple[str, ...]:
        if isinstance(arg, int):
            return (str(arg),)
        if not self.script.variables:
            return arg
        return tuple(self.expand(s) for s in arg)

    def expand(self, s: str) -> str:
        if "${" not in s:
            return s

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name.isdigit():
                i = int(name)
                return self.match_vars[i] if i < len(self.match_vars) else ""
            return self.vars.get(name.lower(), "")

        return _VAR.sub(sub, s)

    # -- commands -----------------------------------------------------------------------
    def block(self, nodes: tuple[Node, ...]) -> None:
        taken = True  # an if/elsif chain has been satisfied (or there is none)
        for n in nodes:
            self.tick()
            if n.name == "require":
                continue
            if n.name == "if":
                taken = self.test(n.tests[0])
                if taken:
                    self.block(n.block or ())
            elif n.name == "elsif":
                if not taken:
                    taken = self.test(n.tests[0])
                    if taken:
                        self.block(n.block or ())
            elif n.name == "else":
                if not taken:
                    self.block(n.block or ())
                taken = True
            else:
                self.command(n)
                taken = True

    def command(self, n: Node) -> None:
        name = n.name
        if name == "stop":
            raise _Stop
        if name == "keep":
            self.state.keep = self.rule(n)
            self.add_flags(n)
        elif name == "discard":
            self.state.discard = self.state.discard or self.rule(n)
        elif name == "fileinto":
            self.fileinto(n)
        elif name in _INTENTS:
            self.intent(n)
        elif name == "skip_enrich":
            self.emit("analysis", "skip_enrich", n)
        elif name == "set":
            self.set_var(n)
        elif name in ("setflag", "addflag", "removeflag"):
            self.flags_command(n)
        else:  # pragma: no cover - validate() rejects every other name
            raise SieveError(f"cannot run {name!r}", n.line)

    def fileinto(self, n: Node) -> None:
        mailbox = self.strings(n.args[0])[0].strip()
        if not mailbox or len(mailbox) > 255 or _MAILBOX_BAD.search(mailbox):
            raise SieveError(f"bad mailbox name {mailbox[:40]!r}", n.line)
        self.add_flags(n)
        if mailbox.casefold() == "inbox":
            self.state.keep = self.state.keep or self.rule(n)
            return
        self.emit("label", mailbox, n)
        if ":copy" in n.tags:
            self.state.keep = self.state.keep or self.rule(n)
        else:
            self.state.filed = self.state.filed or self.rule(n)

    def intent(self, n: Node) -> None:
        detail = ""
        if n.args:
            detail = " ".join(self.strings(n.args[0]))[:200]
        self.emit("intent", f"{n.name} {detail}".strip(), n)

    def add_flags(self, n: Node) -> None:
        extra = n.tags.get(":flags")
        if extra is not None:
            self.change_flags("addflag", self.strings(extra), n)

    def flags_command(self, n: Node) -> None:
        self.change_flags(n.name, self.strings(n.args[0]), n)

    def change_flags(self, op: str, values: tuple[str, ...], n: Node) -> None:
        wanted = _flag_list(values)
        current = self.state.flags
        if op == "setflag":
            current[:] = wanted
        elif op == "addflag":
            current.extend(f for f in wanted if f.casefold() not in {c.casefold() for c in current})
        else:
            drop = {f.casefold() for f in wanted}
            current[:] = [f for f in current if f.casefold() not in drop]
        if len(current) > 64:
            raise SieveError("more than 64 flags", n.line)

    def set_var(self, n: Node) -> None:
        name = self.strings(n.args[0])[0].lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise SieveError(f"bad variable name {name!r}", n.line)
        value = self.strings(n.args[1])[0]
        for tag, fn in _MODIFIERS:
            if tag in n.tags:
                value = fn(value)
        self.vars[name] = value[: self.cfg.max_variable_chars]

    # -- tests --------------------------------------------------------------------------
    def test(self, n: Node) -> bool:
        self.tick()
        name = n.name
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "not":
            return not self.test(n.tests[0])
        if name == "allof":
            return all(self.test(t) for t in n.tests)
        if name == "anyof":
            return any(self.test(t) for t in n.tests)
        if name == "exists":
            return all(self.view.headers.get(h.lower()) for h in self.strings(n.args[0]))
        if name == "size":
            limit = n.args[0] if isinstance(n.args[0], int) else 0
            return self.view.size > limit if ":over" in n.tags else self.view.size < limit
        if name == "header":
            return self.header_test(n)
        if name in ("address", "envelope"):
            return self.address_test(n)
        if name == "body":
            return self.compare(n, [self.view.body()], self.strings(n.args[0]))
        if name == "hasflag":
            return self.compare(n, list(self.state.flags), self.strings(n.args[0]))
        if name == "string":
            return self.compare(n, list(self.strings(n.args[0])), self.strings(n.args[1]))
        return self.date_test(n)

    def header_test(self, n: Node) -> bool:
        values: list[str] = []
        for h in self.strings(n.args[0]):
            values.extend(self.view.headers.get(h.lower(), ()))
        return self.compare(n, values, self.strings(n.args[1]))

    def address_test(self, n: Node) -> bool:
        keys = self.strings(n.args[1])
        values: list[str] = []
        if n.name == "envelope":
            for part in self.strings(n.args[0]):
                env = self.view.envelope
                addr = {"from": env.sender, "to": env.recipient}.get(part.lower())
                if addr:
                    values.append(addr)
        else:
            for h in self.strings(n.args[0]):
                for raw in self.view.headers.get(h.lower(), ()):
                    values.extend(a for _, a in getaddresses([raw]) if a)
        part_tag = next(
            (t for t in n.tags if t in (":localpart", ":domain", ":user", ":detail")), None
        )
        parts = [p for p in (_address_part(v, part_tag) for v in values) if p is not None]
        return self.compare(n, parts, keys, count_of=values)

    def date_test(self, n: Node) -> bool:
        if n.name == "currentdate":
            when, part, keys = self.view.now, self.strings(n.args[0])[0], self.strings(n.args[1])
        else:
            found = self.view.headers.get(self.strings(n.args[0])[0].lower(), ())
            if not found:
                return False
            try:
                when = parsedate_to_datetime(found[0])
            except (TypeError, ValueError, IndexError):
                return False
            part, keys = self.strings(n.args[1])[0], self.strings(n.args[2])
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        zone = n.tags.get(":zone")
        if ":originalzone" not in n.tags or n.name == "currentdate":
            when = when.astimezone(_zone(self.strings(zone)[0]) if zone is not None else UTC)
        value = _date_part(when, part.lower())
        if value is None:
            raise SieveError(f"unknown date part {part!r}", n.line)
        return self.compare(n, [value], keys)

    # -- the match engine ---------------------------------------------------------------
    def compare(
        self,
        n: Node,
        values: Sequence[str],
        keys: Sequence[str],
        *,
        count_of: Sequence[str] | None = None,
    ) -> bool:
        comp_arg = n.tags.get(":comparator")
        comparator = self.strings(comp_arg)[0] if comp_arg is not None else "i;ascii-casemap"
        kind = next((t for t in (":is", ":contains", ":matches", ":regex") if t in n.tags), ":is")
        if ":count" in n.tags or ":value" in n.tags:
            return self.relational(n, values, keys, comparator, count_of)
        self.tick(max(1, len(values)) * max(1, len(keys)))
        f = fold(comparator)
        limit = self.cfg.max_match_chars
        for value in values:
            if comparator == "i;ascii-numeric":
                if kind != ":is":
                    raise SieveError("i;ascii-numeric only supports :is", n.line)
                if any(numeric(value) == numeric(k) for k in keys):
                    return True
                continue
            if kind == ":is":
                fv = f(value)
                if any(fv == f(k) for k in keys):
                    return True
            elif kind == ":contains":
                fv = f(value)
                if any(f(k) in fv for k in keys):
                    return True
            elif kind == ":matches":
                if self.matches(f, value[:limit], keys):
                    return True
            elif self.regex(value[: self.cfg.max_regex_chars], keys, comparator):
                return True
        return False

    def matches(self, f: Callable[[str], str], value: str, keys: Sequence[str]) -> bool:
        fv = f(value)
        for key in keys:
            tokens = [t if t in ("*", "?") else "=" + f(t[1:]) for t in glob_tokens(key)]
            caps = glob_match(tokens, fv, value)
            if caps is not None:
                if self.script.variables:
                    self.match_vars = [value, *caps][:10]
                return True
        return False

    def regex(self, value: str, keys: Sequence[str], comparator: str) -> bool:
        for key in keys:
            pattern = compiled_regex(key, comparator == "i;ascii-casemap")
            m = pattern.search(value)
            if m is not None:
                if self.script.variables:
                    self.match_vars = [m.group(0), *(g or "" for g in m.groups())][:10]
                return True
        return False

    def relational(
        self,
        n: Node,
        values: Sequence[str],
        keys: Sequence[str],
        comparator: str,
        count_of: Sequence[str] | None,
    ) -> bool:
        is_count = ":count" in n.tags
        rel = self.strings(n.tags[":count" if is_count else ":value"] or ())[0]
        self.tick(max(1, len(values)) * max(1, len(keys)))
        if is_count:
            have = len(count_of if count_of is not None else values)
            return any(_rel(rel, (0, have), numeric(k)) for k in keys)
        key_of: Callable[[str], tuple[int, int] | str] = (
            numeric if comparator == "i;ascii-numeric" else fold(comparator)
        )
        return any(_rel(rel, key_of(v), key_of(k)) for v in values for k in keys)


# -- helpers --------------------------------------------------------------------------------
def _rel(op: str, a: Any, b: Any) -> bool:
    """Compare two values of the same kind (both numeric keys or both folded strings)."""
    order = int(a > b) - int(a < b)
    return {
        "eq": order == 0,
        "ne": order != 0,
        "gt": order > 0,
        "ge": order >= 0,
        "lt": order < 0,
        "le": order <= 0,
    }.get(op, False)


def _flag_list(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for word in value.split():
            out.append(_SYSTEM_FLAGS.get(word.lower(), word))
    return out


def _address_part(address: str, part: str | None) -> str | None:
    """The part asked for; None when it does not exist (an address with no +detail)."""
    local, _, domain = address.rpartition("@")
    if not local:  # no "@": the whole thing is the local part
        local, domain = address, ""
    if part == ":localpart":
        return local
    if part == ":domain":
        return domain
    if part in (":user", ":detail"):
        user, plus, detail = local.partition("+")
        return user if part == ":user" else (detail if plus else None)
    return address


def _zone(spec: str) -> timezone:
    if spec.lower() == "local":
        return UTC
    m = re.fullmatch(r"([+-])(\d\d)(\d\d)", spec)
    if m is None or int(m.group(2)) > 23 or int(m.group(3)) > 59:
        raise SieveError(f"bad time zone {spec!r}")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def _date_part(when: datetime, part: str) -> str | None:
    offset = when.utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    zone = f"{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"
    parts = {
        "year": f"{when.year:04d}",
        "month": f"{when.month:02d}",
        "day": f"{when.day:02d}",
        "date": when.strftime("%Y-%m-%d"),
        "julian": str(when.toordinal() + 1721424),
        "hour": f"{when.hour:02d}",
        "minute": f"{when.minute:02d}",
        "second": f"{when.second:02d}",
        "time": when.strftime("%H:%M:%S"),
        "iso8601": when.strftime("%Y-%m-%dT%H:%M:%S") + zone[:3] + ":" + zone[3:],
        "std11": when.strftime("%a, %d %b %Y %H:%M:%S ") + zone,
        "zone": zone,
        "weekday": str((when.weekday() + 1) % 7),
    }
    return parts.get(part)


def _first_upper(s: str) -> str:
    return s[:1].upper() + s[1:]


def _first_lower(s: str) -> str:
    return s[:1].lower() + s[1:]


def _quote_wildcards(s: str) -> str:
    return re.sub(r"([*?\\])", r"\\\1", s)


# Applied lowest precedence number last, as RFC 5229 section 4 lists them.
_MODIFIERS: Final[tuple[tuple[str, Callable[[str], str]], ...]] = (
    (":lower", str.lower),
    (":upper", str.upper),
    (":lowerfirst", _first_lower),
    (":upperfirst", _first_upper),
    (":quotewildcard", _quote_wildcards),
    (":length", lambda s: str(len(s))),
)

__all__ = ["Effect", "Envelope", "MessageView", "SieveResult", "run"]
