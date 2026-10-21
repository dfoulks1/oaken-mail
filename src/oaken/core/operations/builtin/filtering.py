# SPDX-License-Identifier: Apache-2.0
"""Sieve operations: check a script, try it on a message without storing anything, and list the
scripts core has loaded. Re-running the filters over stored mail is ``reprocess`` with the
``filter`` stage. None of these is offered over MCP: scripts are the person's own configuration."""

import asyncio
import base64
import binascii
from pathlib import Path

from pydantic import BaseModel, Field

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import MessageId
from oaken.core.operations.builtin.system import Empty
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.mail.filtering import (
    check_script_name,
    delete_script,
    load_scripts,
    save_script,
    scripts_dir,
)
from oaken.mail.parse import parse
from oaken.mail.sieve import SieveError, compile_script, run, view_of

MAX_EML_BYTES = 8 * 1024 * 1024


class CheckInput(BaseModel):
    script: str = Field(max_length=1_000_000)
    name: str = Field(default="script.sieve", max_length=128)


class CheckOutput(BaseModel):
    ok: bool
    error: str | None = None
    line: int | None = None
    requires: list[str] = []
    sha256: str | None = None


def _check(name: str, text: str, max_bytes: int) -> tuple[CheckOutput, object | None]:
    try:
        script = compile_script(name, text, max_bytes=max_bytes)
    except SieveError as exc:
        return CheckOutput(ok=False, error=exc.message, line=getattr(exc, "line", None)), None
    return CheckOutput(ok=True, requires=sorted(script.requires), sha256=script.sha256), script


@operation(
    "sieve_check",
    "Compile a Sieve script and report the first error, with its line.",
    permission="read",
    input=CheckInput,
    output=CheckOutput,
    mcp="never",
)
async def sieve_check(ctx: OpContext, inp: CheckInput) -> CheckOutput:
    return _check(inp.name, inp.script, ctx.svc.settings.mail.sieve.max_script_bytes)[0]


class TestInput(BaseModel):
    __test__ = False  # not a pytest class
    script: str = Field(max_length=1_000_000)
    name: str = Field(default="script.sieve", max_length=128)
    message_id: str | None = Field(default=None, description="A stored message to run it on.")
    eml_base64: str | None = Field(default=None, description="Or a message as base64 .eml bytes.")


class EffectOut(BaseModel):
    kind: str
    value: str
    rule: str


class TestOutput(BaseModel):
    __test__ = False
    check: CheckOutput
    disposition: str | None = None
    rule: str | None = None
    effects: list[EffectOut] = []
    errors: list[str] = []
    steps: int = 0


@operation(
    "sieve_test",
    "Run a Sieve script on one message and show each effect and its rule. Stores nothing.",
    permission="read",
    input=TestInput,
    output=TestOutput,
    mcp="never",
)
async def sieve_test(ctx: OpContext, inp: TestInput) -> TestOutput:
    if (inp.message_id is None) == (inp.eml_base64 is None):
        raise InvalidInput("give exactly one of message_id and eml_base64")
    settings = ctx.svc.settings.mail
    checked, script = _check(inp.name, inp.script, settings.sieve.max_script_bytes)
    if script is None:
        return TestOutput(check=checked)
    if inp.message_id is not None:
        raw = await ctx.svc.raw.get_raw(MessageId(inp.message_id))
    else:
        try:
            raw = base64.b64decode(inp.eml_base64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise InvalidInput("eml_base64 is not valid base64") from None
        if len(raw) > MAX_EML_BYTES:
            raise InvalidInput(f"the message is larger than {MAX_EML_BYTES} bytes")
    parsed = await asyncio.to_thread(parse, raw, settings)
    view = view_of(parsed, size=len(raw), now=ctx.svc.clock.now())
    result = await asyncio.to_thread(run, [script], view, settings.sieve)  # type: ignore[list-item]
    return TestOutput(
        check=checked,
        disposition=result.disposition,
        rule=result.disposition_rule,
        effects=[EffectOut(kind=e.kind, value=e.value, rule=e.rule) for e in result.effects],
        errors=list(result.errors),
        steps=result.steps,
    )


class ScriptInfo(BaseModel):
    name: str
    sha256: str


class Problem(BaseModel):
    name: str
    problem: str


class ListScriptsOutput(BaseModel):
    enabled: bool
    directory: str
    sha256: str
    scripts: list[ScriptInfo]
    problems: list[Problem]


@operation(
    "sieve_list",
    "List the Sieve scripts on disk, and any skipped with the reason.",
    permission="read",
    input=Empty,
    output=ListScriptsOutput,
    mcp="never",
)
async def sieve_list(ctx: OpContext, inp: Empty) -> ListScriptsOutput:
    settings = ctx.svc.settings.mail
    directory = scripts_dir(settings)
    report = await asyncio.to_thread(load_scripts, Path(directory), settings)  # read afresh
    return ListScriptsOutput(
        enabled=settings.sieve.enabled,
        directory=str(directory),
        sha256=report.sha256,
        scripts=[ScriptInfo(name=s.name, sha256=s.sha256) for s in report.scripts],
        problems=[Problem(name=n, problem=p) for n, p in report.problems],
    )


class GetScriptInput(BaseModel):
    name: str = Field(max_length=128)


class GetScriptOutput(BaseModel):
    name: str
    text: str
    sha256: str


@operation(
    "sieve_get",
    "Read one Sieve script's text, so an editor can load it.",
    permission="read",
    input=GetScriptInput,
    output=GetScriptOutput,
    mcp="never",
)
async def sieve_get(ctx: OpContext, inp: GetScriptInput) -> GetScriptOutput:
    import hashlib

    name = check_script_name(inp.name)
    directory = scripts_dir(ctx.svc.settings.mail)
    path = directory / name
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except FileNotFoundError:
        raise NotFound(f"no such script: {name}") from None
    return GetScriptOutput(name=name, text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


class SaveScriptInput(BaseModel):
    name: str = Field(max_length=128)
    text: str = Field(max_length=1_000_000)


class SaveScriptOutput(BaseModel):
    saved: bool
    check: CheckOutput
    sha256: str | None = None


@operation(
    "sieve_save",
    "Compile a Sieve script and, only if it compiles, save it so it runs on the next filter pass.",
    permission="mutate",
    input=SaveScriptInput,
    output=SaveScriptOutput,
    mcp="never",  # writing the user's own filters is never offered to an agent (prompt 26)
)
async def sieve_save(ctx: OpContext, inp: SaveScriptInput) -> SaveScriptOutput:
    settings = ctx.svc.settings.mail
    name = check_script_name(inp.name)
    checked, script = _check(name, inp.text, settings.sieve.max_script_bytes)
    if script is None:  # refuse to persist anything that does not compile (prompt 26 req 1)
        return SaveScriptOutput(saved=False, check=checked)
    directory = scripts_dir(settings)
    await asyncio.to_thread(save_script, directory, name, inp.text)
    return SaveScriptOutput(saved=True, check=checked, sha256=checked.sha256)


class DeleteScriptInput(BaseModel):
    name: str = Field(max_length=128)


class DeleteScriptOutput(BaseModel):
    deleted: bool


@operation(
    "sieve_delete",
    "Remove one Sieve script. It stops running on the next filter pass.",
    permission="mutate",
    input=DeleteScriptInput,
    output=DeleteScriptOutput,
    mcp="never",
)
async def sieve_delete(ctx: OpContext, inp: DeleteScriptInput) -> DeleteScriptOutput:
    name = check_script_name(inp.name)
    directory = scripts_dir(ctx.svc.settings.mail)
    done = await asyncio.to_thread(delete_script, directory, name)
    return DeleteScriptOutput(deleted=done)


OPERATIONS: tuple[AnyOperation, ...] = (
    sieve_check,
    sieve_test,
    sieve_list,
    sieve_get,
    sieve_save,
    sieve_delete,
)
