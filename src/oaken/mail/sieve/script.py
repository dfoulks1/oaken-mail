# SPDX-License-Identifier: Apache-2.0
"""Compile a script: parse it, then check names, tags, argument counts and ``require`` lines.

Everything Oaken supports is listed here, so this file is also the answer to "does it do X?".
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from oaken.mail.sieve.parser import Arg, Node, SieveError, parse_script

# Extensions that are implemented. Anything else in a ``require`` is an error.
SUPPORTED: Final = frozenset(
    {
        "fileinto",
        "copy",
        "imap4flags",
        "envelope",
        "body",
        "relational",
        "comparator-i;octet",
        "comparator-i;ascii-casemap",
        "comparator-i;ascii-numeric",
        "regex",
        "variables",
        "date",
        "subaddress",
        "reject",
        "ereject",
        "vacation",
        "vnd.oaken.analysis",
    }
)
# Refused on purpose, with the reason shown to the user.
REFUSED: Final = {
    "editheader": "the raw message is the source of truth and is never rewritten",
    "include": "scripts are run in name order; use several files instead",
    "notify": "Oaken sends nothing",
    "mime": "attachments and MIME parts are never opened",
    "foreverypart": "attachments and MIME parts are never opened",
    "extracttext": "attachments and MIME parts are never opened",
}

MATCH_TYPES: Final = (":is", ":contains", ":matches", ":regex", ":count", ":value")
ADDRESS_PARTS: Final = (":all", ":localpart", ":domain", ":user", ":detail")
RELATIONS: Final = ("eq", "ne", "gt", "ge", "lt", "le")
_COMMON: Final = (*MATCH_TYPES, ":comparator")

# name -> (extension needed, allowed tags, (min args, max args))
COMMANDS: Final[dict[str, tuple[str | None, tuple[str, ...], tuple[int, int]]]] = {
    "require": (None, (), (1, 1)),
    "if": (None, (), (0, 0)),
    "elsif": (None, (), (0, 0)),
    "else": (None, (), (0, 0)),
    "stop": (None, (), (0, 0)),
    "keep": (None, (":flags",), (0, 0)),
    "discard": (None, (), (0, 0)),
    "redirect": (None, (":copy",), (1, 1)),
    "fileinto": ("fileinto", (":copy", ":flags"), (1, 1)),
    "reject": ("reject", (), (1, 1)),
    "ereject": ("ereject", (), (1, 1)),
    "vacation": (
        "vacation",
        (":days", ":subject", ":from", ":addresses", ":mime", ":handle"),
        (1, 1),
    ),
    "set": (
        "variables",
        (":lower", ":upper", ":lowerfirst", ":upperfirst", ":quotewildcard", ":length"),
        (2, 2),
    ),
    "setflag": ("imap4flags", (), (1, 1)),
    "addflag": ("imap4flags", (), (1, 1)),
    "removeflag": ("imap4flags", (), (1, 1)),
    "skip_enrich": ("vnd.oaken.analysis", (), (0, 0)),
}
TESTS: Final[dict[str, tuple[str | None, tuple[str, ...], tuple[int, int]]]] = {
    "true": (None, (), (0, 0)),
    "false": (None, (), (0, 0)),
    "not": (None, (), (0, 0)),
    "allof": (None, (), (0, 0)),
    "anyof": (None, (), (0, 0)),
    "header": (None, _COMMON, (2, 2)),
    "address": (None, (*_COMMON, *ADDRESS_PARTS), (2, 2)),
    "envelope": ("envelope", (*_COMMON, *ADDRESS_PARTS), (2, 2)),
    "exists": (None, (), (1, 1)),
    "size": (None, (":over", ":under"), (1, 1)),
    "body": ("body", (*_COMMON, ":text"), (1, 1)),
    "hasflag": ("imap4flags", _COMMON, (1, 1)),
    "date": ("date", (*_COMMON, ":zone", ":originalzone"), (3, 3)),
    "currentdate": ("date", (*_COMMON, ":zone"), (2, 2)),
    "string": ("variables", _COMMON, (2, 2)),
}
_TAG_EXT: Final = {
    ":copy": "copy",
    ":flags": "imap4flags",
    ":regex": "regex",
    ":count": "relational",
    ":value": "relational",
    ":user": "subaddress",
    ":detail": "subaddress",
    ":zone": "date",
    ":originalzone": "date",
}
# A tag that is the value of a test, not a flag.
_REFUSED_TAGS: Final = {":raw": "body :raw", ":content": "body :content", ":index": "header :index"}
_COMPARATORS: Final = ("i;octet", "i;ascii-casemap", "i;ascii-numeric")
_VARIABLE = re.compile(r"\$\{")


@dataclass(frozen=True, slots=True)
class Script:
    name: str
    nodes: tuple[Node, ...]
    requires: frozenset[str]
    sha256: str

    @property
    def variables(self) -> bool:
        return "variables" in self.requires


def compile_script(name: str, text: str, *, max_bytes: int = 256 * 1024) -> Script:
    """Parse and validate. Raises ``SieveError`` (with a line number) on the first problem."""
    if len(text.encode("utf-8", "replace")) > max_bytes:
        raise SieveError(f"script is larger than {max_bytes} bytes")
    nodes = parse_script(text)
    requires: set[str] = set()
    _requires(nodes, requires)
    _commands(nodes, frozenset(requires), top=True)
    return Script(name, nodes, frozenset(requires), hashlib.sha256(text.encode()).hexdigest())


def _requires(nodes: tuple[Node, ...], out: set[str]) -> None:
    seen_other = False
    for n in nodes:
        if n.name != "require":
            seen_other = True
            continue
        if seen_other:
            raise SieveError("require must come before every other command", n.line)
        for ext in _strings(n.args[0] if n.args else (), n):
            if ext in REFUSED:
                raise SieveError(f"extension {ext!r} is not supported: {REFUSED[ext]}", n.line)
            if ext not in SUPPORTED:
                raise SieveError(f"unknown extension {ext!r}", n.line)
            out.add(ext)


def _strings(arg: Arg, node: Node) -> tuple[str, ...]:
    if isinstance(arg, int):
        raise SieveError(f"{node.name}: expected a string", node.line)
    return arg


def _commands(nodes: tuple[Node, ...], req: frozenset[str], *, top: bool) -> None:
    previous = ""
    for n in nodes:
        if n.name == "require":
            if not top:
                raise SieveError("require is only allowed at the top level", n.line)
            previous = n.name
            continue
        spec = COMMANDS.get(n.name)
        if spec is None:
            raise SieveError(f"unknown command {n.name!r}", n.line)
        if n.name in ("elsif", "else") and previous not in ("if", "elsif"):
            raise SieveError(f"{n.name} without a matching if", n.line)
        _check(n, spec, req)
        if n.name in ("if", "elsif"):
            _test(n.tests[0], req)
        if n.block is not None:
            if n.name not in ("if", "elsif", "else"):
                raise SieveError(f"{n.name} does not take a block", n.line)
            _commands(n.block, req, top=False)
        previous = n.name


def _test(n: Node, req: frozenset[str]) -> None:
    spec = TESTS.get(n.name)
    if spec is None:
        raise SieveError(f"unknown test {n.name!r}", n.line)
    _check(n, spec, req)
    for child in n.tests:
        _test(child, req)
    if n.name == "not" and len(n.tests) != 1:
        raise SieveError("not takes exactly one test", n.line)
    comp = n.tags.get(":comparator")
    if comp is not None:
        name = _strings(comp, n)[0]
        if name not in _COMPARATORS:
            raise SieveError(f"unknown comparator {name!r}", n.line)
        if f"comparator-{name}" not in req and name != "i;ascii-casemap" and name != "i;octet":
            raise SieveError(f'comparator {name!r} needs require "comparator-{name}"', n.line)
        if name == "i;octet" and "comparator-i;octet" not in req:
            raise SieveError('comparator "i;octet" needs require "comparator-i;octet"', n.line)
    kinds = [
        t for t in n.tags if t in (":is", ":contains", ":matches", ":regex", ":count", ":value")
    ]
    if len(kinds) > 1:
        raise SieveError("only one match type is allowed", n.line)
    if ":count" in n.tags or ":value" in n.tags:
        rel = _strings(n.tags.get(":count") or n.tags.get(":value") or (), n)
        if len(rel) != 1 or rel[0] not in RELATIONS:
            raise SieveError(f"relation must be one of {', '.join(RELATIONS)}", n.line)
    _static_regexes(n)


def _check(
    n: Node, spec: tuple[str | None, tuple[str, ...], tuple[int, int]], req: frozenset[str]
) -> None:
    ext, tags, (lo, hi) = spec
    if ext is not None and ext not in req:
        raise SieveError(f'{n.name} needs require "{ext}"', n.line)
    for tag in n.tags:
        if tag in _REFUSED_TAGS:
            raise SieveError(f"{_REFUSED_TAGS[tag]} is not supported", n.line)
        if tag not in tags:
            raise SieveError(f"{n.name} does not take {tag}", n.line)
        need = _TAG_EXT.get(tag)
        if need is not None and need not in req:
            raise SieveError(f'{tag} needs require "{need}"', n.line)
    if not lo <= len(n.args) <= hi:
        raise SieveError(
            f"{n.name} takes {lo if lo == hi else f'{lo} to {hi}'} argument(s)", n.line
        )
    if n.name == "size" and not (":over" in n.tags) ^ (":under" in n.tags):
        raise SieveError("size needs exactly one of :over and :under", n.line)
    if n.name == "size" and not isinstance(n.args[0], int):
        raise SieveError("size takes a number", n.line)


def _static_regexes(n: Node) -> None:
    """Fail at compile time on a regular expression that is wrong or unsafe."""
    if ":regex" not in n.tags:
        return
    from oaken.mail.sieve.match import check_regex

    keys = n.args[-1] if n.args and not isinstance(n.args[-1], int) else ()
    for pattern in keys:
        if not _VARIABLE.search(pattern):
            check_regex(pattern, n.line)
