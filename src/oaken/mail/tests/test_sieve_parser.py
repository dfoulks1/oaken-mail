# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken.mail.sieve import REFUSED, SUPPORTED, SieveError, compile_script
from oaken.mail.sieve.parser import parse_script, tokenize


def test_comments_strings_and_numbers() -> None:
    nodes = parse_script(
        '# line comment\n/* block\ncomment */ require ["fileinto"]; size :over 100K;'
    )
    assert nodes[0].name == "require"
    assert nodes[0].args == (("fileinto",),)
    assert nodes[1].tags == {":over": None}
    assert nodes[1].args == (102400,)


@pytest.mark.parametrize(
    ("text", "value"), [("1K", 1024), ("2M", 2 << 20), ("1g", 1 << 30), ("7", 7)]
)
def test_number_quantifiers(text: str, value: int) -> None:
    assert tokenize(text)[0].value == value


def test_string_escapes() -> None:
    (tok,) = [t for t in tokenize(r'"a\"b\\c"') if t.kind == "string"]
    assert tok.value == 'a"b\\c'


def test_multiline_text_block_with_dot_stuffing() -> None:
    nodes = parse_script("vacation text:\nHello\n..dotted\n.\n;")
    assert nodes[0].args == (("Hello\n.dotted\n",),)


def test_text_block_may_carry_a_comment() -> None:
    nodes = parse_script("keep; reject text: # why\nno\n.\n;")
    assert nodes[1].args == (("no\n",),)


def test_if_elsif_else_and_nested_tests() -> None:
    (node,) = parse_script('if allof (not header :is "a" "b", anyof (true, false)) { keep; }')
    assert node.name == "if"
    test = node.tests[0]
    assert test.name == "allof" and [t.name for t in test.tests] == ["not", "anyof"]
    assert node.block is not None and node.block[0].name == "keep"


def test_string_list_and_tags_with_values() -> None:
    (node,) = parse_script('if header :comparator "i;octet" :is ["a","b"] "c" { stop; }')
    test = node.tests[0]
    assert test.tags[":comparator"] == ("i;octet",)
    assert test.args == (("a", "b"), ("c",))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("keep", "expected ';'"),
        ("if true { keep; ", "missing '}'"),
        ("if true keep;", "needs a block"),
        ('keep; "x";', "expected a command"),
        ("/* never closed", "unterminated comment"),
        ('"never closed', "unterminated string"),
        ("keep;;", "expected a command"),
        ("header [1];", "only strings"),
        ("size :over;\n@", "unexpected character"),
        ("text:\nno dot", "unterminated text"),
    ],
)
def test_syntax_errors_carry_a_line(text: str, message: str) -> None:
    with pytest.raises(SieveError) as exc:
        parse_script(text)
    assert message in exc.value.message
    assert exc.value.line is not None


def test_error_line_numbers_follow_newlines() -> None:
    with pytest.raises(SieveError) as exc:
        parse_script("keep;\n\n\nif true {\n keep\n}")
    assert exc.value.line == 6


def test_nesting_is_limited() -> None:
    deep = "if true {" * 40 + "keep;" + "}" * 40
    with pytest.raises(SieveError, match="nested too deeply"):
        parse_script(deep)


# -- validation ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("script", "message"),
    [
        ('fileinto "a";', 'needs require "fileinto"'),
        ('require "nonsense";', "unknown extension"),
        ('keep; require "fileinto";', "require must come before"),
        ('if true { require "fileinto"; }', "only allowed at the top level"),
        ("frobnicate;", "unknown command"),
        ("if frob { keep; }", "unknown test"),
        ("else { keep; }", "without a matching if"),
        ("if true { keep; } keep; else { keep; }", "without a matching if"),
        ("if size :over 1 :under 2 { keep; }", "takes 1 argument"),
        ("if size 5 { keep; }", "exactly one of"),
        ("if size { keep; }", "takes 1 argument"),
        ('if size :over "x" { keep; }', "takes a number"),
        ('if header :regex "a" "b" { keep; }', 'needs require "regex"'),
        ('if header :count "ge" "a" "1" { keep; }', 'needs require "relational"'),
        ('require "relational"; if header :count "zz" "a" "1" { keep; }', "relation must be"),
        ('if header :is :contains "a" "b" { keep; }', "only one match type"),
        ('if header :comparator "x" :is "a" "b" { keep; }', "unknown comparator"),
        ('if header :comparator "i;ascii-numeric" :is "a" "b" { keep; }', "needs require"),
        ('require "regex"; if header :regex "a" "(" { keep; }', "bad regular expression"),
        ('require "regex"; if header :regex "a" "(a+)+$" { keep; }', "repeats a repetition"),
        ('require "body"; if body :raw :contains "x" { keep; }', "not supported"),
        ('keep :flags "x";', 'needs require "imap4flags"'),
        ('redirect :copy "a@b.c";', 'needs require "copy"'),
        ("keep { keep; }", "does not take a block"),
        ("discard 1;", "takes 0 argument"),
    ],
)
def test_validation_errors(script: str, message: str) -> None:
    with pytest.raises(SieveError) as exc:
        compile_script("t.sieve", script)
    assert message in exc.value.message


@pytest.mark.parametrize("ext", sorted(REFUSED))
def test_refused_extensions_say_why(ext: str) -> None:
    with pytest.raises(SieveError, match="not supported"):
        compile_script("t.sieve", f'require "{ext}";')


def test_every_supported_extension_can_be_required() -> None:
    compile_script("t.sieve", "require [" + ",".join(f'"{e}"' for e in sorted(SUPPORTED)) + "];")


def test_script_hash_is_stable_and_content_sensitive() -> None:
    a = compile_script("a", "keep;")
    assert a.sha256 == compile_script("b", "keep;").sha256
    assert a.sha256 != compile_script("a", "discard;").sha256


def test_oversize_script_is_refused() -> None:
    with pytest.raises(SieveError, match="larger than"):
        compile_script("t", "keep;" * 100, max_bytes=50)
