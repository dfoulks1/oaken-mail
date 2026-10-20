# SPDX-License-Identifier: Apache-2.0
"""A lexer and parser for the Sieve grammar (RFC 5228, section 8).

The parser accepts any command or test name and reads its arguments generically; ``validate``
then checks names against the supported set and the script's ``require`` lines. That keeps this
file about syntax only. Errors carry a line number.
"""

from dataclasses import dataclass, field
from typing import Final

from oaken.core.errors import OakenError

StrList = tuple[str, ...]
Arg = StrList | int

MAX_NESTING: Final = 32
# Tagged arguments that take a value. Every other tag stands alone.
TAGS_WITH_VALUE: Final = frozenset(
    {
        ":comparator",
        ":count",
        ":value",
        ":index",
        ":zone",
        ":days",
        ":subject",
        ":from",
        ":addresses",
        ":handle",
        ":flags",
        ":seconds",
        ":importance",
        ":mime",
    }
)


class SieveError(OakenError):
    code = "sieve_error"

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass(frozen=True, slots=True)
class Node:
    """A command or a test."""

    name: str
    line: int
    tags: dict[str, Arg | None] = field(default_factory=dict)
    args: tuple[Arg, ...] = ()
    tests: tuple["Node", ...] = ()  # allof/anyof/not children, or the test of if/elsif
    block: tuple["Node", ...] | None = None


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # ident | tag | string | number | punct | eof
    value: str | int
    line: int


def _word(c: str) -> bool:
    """Letters, digits and underscore, ASCII only (str.isalnum also accepts other scripts)."""
    return c == "_" or "a" <= c <= "z" or "A" <= c <= "Z" or "0" <= c <= "9"


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise SieveError("unterminated comment", line)
            line += text.count("\n", i, end)
            i = end + 2
        elif c == '"':
            value, i, newlines = _quoted(text, i, line)
            out.append(Token("string", value, line))
            line += newlines
        elif c in "[](){},;":
            out.append(Token("punct", c, line))
            i += 1
        elif c == ":":
            j = i + 1
            while j < n and _word(text[j]):
                j += 1
            name = text[i:j]
            if name == ":":
                raise SieveError("a lone ':'", line)
            out.append(Token("tag", name.lower(), line))
            i = j
        elif "0" <= c <= "9":
            j = i
            while j < n and "0" <= text[j] <= "9":
                j += 1
            if j - i > 12:
                raise SieveError("number too large", line)
            num = int(text[i:j])
            if j < n and text[j] in "KkMmGg":
                num *= {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[text[j].lower()]
                j += 1
            out.append(Token("number", num, line))
            i = j
        elif _word(c) and not ("0" <= c <= "9"):
            j = i
            while j < n and _word(text[j]):
                j += 1
            word = text[i:j]
            if word.lower() == "text" and text[j : j + 1] == ":":
                value, i, newlines = _multiline(text, j + 1, line)
                out.append(Token("string", value, line))
                line += newlines
                continue
            out.append(Token("ident", word.lower(), line))
            i = j
        else:
            raise SieveError(f"unexpected character {c!r}", line)
    out.append(Token("eof", "", line))
    return out


def _quoted(text: str, i: int, line: int) -> tuple[str, int, int]:
    i += 1
    chars: list[str] = []
    newlines = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            return "".join(chars), i + 1, newlines
        if c == "\\" and i + 1 < len(text):
            i += 1
            c = text[i]
        if c == "\n":
            newlines += 1
        chars.append(c)
        i += 1
    raise SieveError("unterminated string", line)


def _multiline(text: str, i: int, line: int) -> tuple[str, int, int]:
    """``text:`` [comment] newline, lines, then a line holding only a dot (dot-stuffed)."""
    eol = text.find("\n", i)
    rest = text[i:eol].strip() if eol >= 0 else "x"
    if eol < 0 or (rest and not rest.startswith("#")):
        raise SieveError("text: must be followed by a line end", line)
    lines: list[str] = []
    pos = eol + 1
    newlines = 1
    while pos <= len(text):
        end = text.find("\n", pos)
        raw = text[pos:] if end < 0 else text[pos:end]
        row = raw.rstrip("\r")
        newlines += 1
        if row == ".":
            return "".join(f"{x}\n" for x in lines), (len(text) if end < 0 else end + 1), newlines
        lines.append(row[1:] if row.startswith("..") else row)
        if end < 0:
            break
        pos = end + 1
    raise SieveError("unterminated text: block", line)


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.pos = 0

    @property
    def tok(self) -> Token:
        return self.toks[self.pos]

    def take(self) -> Token:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def is_punct(self, p: str) -> bool:
        return self.tok.kind == "punct" and self.tok.value == p

    def expect(self, p: str) -> Token:
        if not self.is_punct(p):
            found = self.tok.value or "end of script"
            raise SieveError(f"expected {p!r}, found {found!r}", self.tok.line)
        return self.take()

    def commands(self, depth: int, until_brace: bool) -> tuple[Node, ...]:
        if depth > MAX_NESTING:
            raise SieveError("blocks nested too deeply", self.tok.line)
        out: list[Node] = []
        while True:
            if self.tok.kind == "eof":
                if until_brace:
                    raise SieveError("missing '}'", self.tok.line)
                return tuple(out)
            if until_brace and self.is_punct("}"):
                return tuple(out)
            out.append(self.command(depth))

    def command(self, depth: int) -> Node:
        t = self.take()
        if t.kind != "ident":
            raise SieveError(f"expected a command, found {t.value!r}", t.line)
        tags, args = self.arguments()
        tests: tuple[Node, ...] = ()
        if t.value in ("if", "elsif"):
            tests = (self.test(depth),)
        if self.is_punct("{"):
            self.take()
            block = self.commands(depth + 1, True)
            self.expect("}")
            return Node(str(t.value), t.line, tags, args, tests, block)
        if t.value in ("if", "elsif", "else"):
            raise SieveError(f"{t.value} needs a block", t.line)
        self.expect(";")
        return Node(str(t.value), t.line, tags, args, tests)

    def test(self, depth: int) -> Node:
        if depth > MAX_NESTING:
            raise SieveError("tests nested too deeply", self.tok.line)
        t = self.take()
        if t.kind != "ident":
            raise SieveError(f"expected a test, found {t.value!r}", t.line)
        tags, args = self.arguments()
        children: tuple[Node, ...] = ()
        if t.value in ("allof", "anyof"):
            self.expect("(")
            items = [self.test(depth + 1)]
            while self.is_punct(","):
                self.take()
                items.append(self.test(depth + 1))
            self.expect(")")
            children = tuple(items)
        elif t.value == "not":
            children = (self.test(depth + 1),)
        return Node(str(t.value), t.line, tags, args, children)

    def arguments(self) -> tuple[dict[str, Arg | None], tuple[Arg, ...]]:
        tags: dict[str, Arg | None] = {}
        args: list[Arg] = []
        while True:
            t = self.tok
            if t.kind == "tag":
                self.take()
                name = str(t.value)
                tags[name] = self.value() if name in TAGS_WITH_VALUE else None
            elif t.kind in ("string", "number") or self.is_punct("["):
                args.append(self.value())
            else:
                return tags, tuple(args)

    def value(self) -> Arg:
        t = self.take()
        if t.kind == "string":
            return (str(t.value),)
        if t.kind == "number":
            return int(t.value)
        if t.kind == "punct" and t.value == "[":
            items: list[str] = []
            while True:
                s = self.take()
                if s.kind != "string":
                    raise SieveError("a string list holds only strings", s.line)
                items.append(str(s.value))
                if self.is_punct(","):
                    self.take()
                    continue
                self.expect("]")
                return tuple(items)
        raise SieveError(f"expected a value, found {t.value!r}", t.line)


def parse_script(text: str) -> tuple[Node, ...]:
    """The commands of a script. Raises ``SieveError`` with a line number on a syntax error."""
    return _Parser(tokenize(text)).commands(0, False)
