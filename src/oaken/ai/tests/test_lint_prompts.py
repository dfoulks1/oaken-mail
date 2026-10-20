# SPDX-License-Identifier: Apache-2.0
"""Nothing under ``oaken/`` may build an LLM request out of a raw string that holds mail.

The rule, checked on the syntax tree of every non-test module: in a call to ``LLMRequest`` the
``system`` text is a plain constant (never an f-string, a concatenation or a ``.format``), and
every part of ``user`` is a string constant or an ``UntrustedText``. Mail text can only get to a
model by way of ``UntrustedText`` and the ``PromptBuilder``.

TODO(live): once real mail flows through an enricher, run a live pass with a local Ollama and
check the delimiters hold against a model that is actually reading them (see prompt 10).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # src/oaken
# The builder is the one place that assembles a request, from typed values only.
ALLOWED = {ROOT / "ai" / "prompts.py"}


def _is_untrusted(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        return name in {"UntrustedText", "untrusted"}
    return False


def _is_plain_text(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def violations(source: str, where: str = "<source>") -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name) and f.id == "LLMRequest") and not (
            isinstance(f, ast.Attribute) and f.attr == "LLMRequest"
        ):
            continue
        args: dict[str, ast.expr] = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        positional = list(node.args)
        system = args.get("system") or (positional[0] if positional else None)
        user = args.get("user") or (positional[1] if len(positional) > 1 else None)
        line = f"{where}:{node.lineno}"
        if system is not None and not _is_plain_text(system):
            out.append(f"{line}: system text is not a plain string constant")
        if user is None:
            continue
        if not isinstance(user, ast.Tuple | ast.List):
            out.append(f"{line}: user turn is not a literal tuple or list of parts")
            continue
        for part in user.elts:
            if not (_is_plain_text(part) or _is_untrusted(part)):
                out.append(f"{line}: a user part is neither a constant nor UntrustedText")
    return out


def test_no_module_builds_a_request_from_a_raw_string_with_mail() -> None:
    found: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.relative_to(ROOT).parts or path in ALLOWED:
            continue
        found += violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    assert not found, "\n".join(found)


def test_the_lint_catches_an_f_string_with_mail() -> None:
    bad = 'LLMRequest("be helpful", (f"summarize {msg.body}",))'
    assert violations(bad)


def test_the_lint_catches_mail_in_the_system_text() -> None:
    assert violations('LLMRequest(f"rules {msg.subject}", ())')
    assert violations('LLMRequest("rules " + msg.subject, ())')
    assert violations('LLMRequest("rules {}".format(msg.subject), ())')


def test_the_lint_catches_a_user_turn_it_cannot_see_into() -> None:
    assert violations('LLMRequest("rules", parts)')
    assert violations('LLMRequest(system="rules", user=(body,))')


def test_the_lint_allows_constants_and_untrusted_text() -> None:
    ok = 'LLMRequest("rules", ("Summarize:", UntrustedText(body).render(), msg.untrusted()))'
    # ``.render()`` is a method call on untrusted text, which is not one of the allowed shapes;
    # the builder renders at the backend, so requests carry UntrustedText itself.
    assert violations(ok)
    assert not violations('LLMRequest("rules", ("Summarize:", UntrustedText(body)))')
    assert not violations('LLMRequest(system="rules", user=("x", msg.untrusted()))')
