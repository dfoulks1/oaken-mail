# SPDX-License-Identifier: Apache-2.0
"""``oaken label | folder | star | mailbox``: how mail is organized, each a thin call to operations.

Labels, folders and stars are one idea: a *mark* a person puts on messages to find them again. They
share one grammar, so what you learn for one holds for the others:

    oaken label  list | create NAME | add NAME ID... | remove NAME ID...
    oaken folder list | create PATH | add PATH ID...  | remove ID...   (``move`` is ``add``)
    oaken star   list |                add ID...       | remove ID...   (``unstar ID...``)

``Mark`` is that grammar; a subclass says which operation each verb is. ``mailbox`` is the other
thing: a read-only look at a mailbox (the folder tree, the counts, a listing, why a message was
filed where it was). Nothing here holds business logic: the operations are core's.
"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.mail_cmd import BULK_LIMIT, _render_mark, _render_messages
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

ACTIONS = ("list", "create", "add", "remove", "move")  # ``move`` is ``add`` for a folder


def _usage(message: str) -> CliError:
    return CliError(message, ExitCode.USAGE)


def _out(result: CallResult) -> dict[str, Any]:
    return result.output if isinstance(result.output, dict) else {}


class Mark:
    """One way of marking messages. ``add`` and ``remove`` put it on and take it off; ``list`` shows
    what exists; ``create`` makes one ahead of use (for the kinds that are named)."""

    noun: ClassVar[str]
    plural: ClassVar[str]
    # Does the verb take a NAME before the ids? (``label add NAME ID...``; ``star add ID...`` has
    # none.)
    add_takes_name: ClassVar[bool] = True
    remove_takes_name: ClassVar[bool] = True
    creatable: ClassVar[bool] = True

    async def show(self, client: ApiClient, args: argparse.Namespace) -> int:
        raise NotImplementedError

    async def create(self, client: ApiClient, args: argparse.Namespace, name: str) -> int:
        raise _usage(f"{self.plural} are not created ahead of use")

    async def add(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        raise NotImplementedError

    async def remove(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        raise NotImplementedError

    # -- the grammar, the same for every mark ---------------------------------------------------
    async def dispatch(self, client: ApiClient, args: argparse.Namespace) -> int:
        action = "add" if args.action == "move" and self.noun == "folder" else args.action
        rest = list(args.rest)
        if action == "list":
            if rest:
                raise _usage(f"{self.noun} list takes no arguments")
            return await self.show(client, args)
        if action == "create":
            if not self.creatable or len(rest) != 1:
                raise _usage(
                    f"usage: oaken {self.noun} create NAME"
                    if self.creatable
                    else f"{self.plural} are not created ahead of use"
                )
            return await self.create(client, args, rest[0])
        if action not in ("add", "remove"):
            raise _usage(f"{self.noun} has no {action!r}; try list, create, add or remove")
        takes = self.add_takes_name if action == "add" else self.remove_takes_name
        name = rest.pop(0) if takes and rest else ""
        if takes and not name:
            raise _usage(f"usage: oaken {self.noun} {action} NAME ID...")
        if not rest:
            raise _usage(f"give at least one message id: oaken {self.noun} {action} ...")
        if len(rest) > BULK_LIMIT and not args.yes:
            raise CliError(
                f"this would change {len(rest)} messages (over {BULK_LIMIT}); re-run with --yes",
                ExitCode.NEEDS_CONFIRMATION,
            )
        method = self.add if action == "add" else self.remove
        return await method(client, args, name, rest)


class Label(Mark):
    noun, plural = "label", "labels"

    async def show(self, client: ApiClient, args: argparse.Namespace) -> int:
        result = await client.call("list_mailbox", {})
        rows = [
            {"label": r["name"], "messages": r["count"]["total"], "unread": r["count"]["unread"]}
            for r in _out(result).get("labels", [])
        ]
        return _table(args, result, rows, ["label", "messages", "unread"], "no labels")

    async def create(self, client: ApiClient, args: argparse.Namespace, name: str) -> int:
        result = await client.call("create_label", {"name": name}, confirm=True)
        made = _out(result).get("created")
        return _say(
            args, result, f"label {_out(result).get('name', name)}: {'made' if made else 'exists'}"
        )

    async def add(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("set_labels", {"ids": ids, "add": [name]}, confirm=True)
        return _say(args, result, f"labelled {len(_out(result).get('changed', []))} with {name}")

    async def remove(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("set_labels", {"ids": ids, "remove": [name]}, confirm=True)
        return _say(args, result, f"took {name} off {len(_out(result).get('changed', []))}")


class Folder(Mark):
    noun, plural = "folder", "folders"
    remove_takes_name = False  # a message is in one folder: removing it puts it back in the Inbox

    async def show(self, client: ApiClient, args: argparse.Namespace) -> int:
        result = await client.call("list_mailbox", {})
        rows = [
            {
                "folder": "  " * int(f["depth"]) + f["name"],
                "path": f["path"],
                "here": f["direct"]["total"],
                "unread": f["direct"]["unread"],
                "with subfolders": f["tree"]["total"],
            }
            for f in _out(result).get("folders", [])
        ]
        return _table(
            args,
            result,
            rows,
            ["folder", "path", "here", "unread", "with subfolders"],
            "no folders",
        )

    async def create(self, client: ApiClient, args: argparse.Namespace, name: str) -> int:
        result = await client.call("create_folder", {"path": name}, confirm=True)
        made = _out(result).get("created", [])
        path = _out(result).get("path", name)
        return _say(args, result, f"folder {path}: {'made' if made else 'exists'}")

    async def add(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("move_to_folder", {"ids": ids, "path": name}, confirm=True)
        return _say(
            args,
            result,
            f"moved {len(_out(result).get('moved', []))} to {_out(result).get('path')}",
        )

    async def remove(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("move_to_folder", {"ids": ids, "path": None}, confirm=True)
        return _say(args, result, f"moved {len(_out(result).get('moved', []))} back to the Inbox")


class Star(Mark):
    noun, plural = "star", "stars"
    add_takes_name = remove_takes_name = False
    creatable = False

    async def show(self, client: ApiClient, args: argparse.Namespace) -> int:
        payload: dict[str, object] = {"starred": True}
        if getattr(args, "limit", None):
            payload["limit"] = args.limit
        return _render_messages(args, await client.call("list_messages", payload))

    async def add(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("set_flagged", {"ids": ids, "flagged": True}, confirm=True)
        return _render_mark(args, result)

    async def remove(
        self, client: ApiClient, args: argparse.Namespace, name: str, ids: list[str]
    ) -> int:
        result = await client.call("set_flagged", {"ids": ids, "flagged": False}, confirm=True)
        return _render_mark(args, result)


MARKS: dict[str, Mark] = {m.noun: m for m in (Label(), Folder(), Star())}


def _say(args: argparse.Namespace, result: CallResult, line: str) -> int:
    def text() -> None:
        from oaken_cli.output import print_text

        print_text(line)

    return emit(args, data=result.output, text=text)


def _table(
    args: argparse.Namespace,
    result: CallResult,
    rows: list[dict[str, Any]],
    columns: list[str],
    empty: str,
) -> int:
    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text(empty)
        else:
            print_table(rows, columns)

    return emit(args, data=result.output, text=text)


def cmd_mark(args: argparse.Namespace) -> int:
    """``oaken label|folder|star ACTION ...`` (the noun is set on each subparser)."""
    return run(lambda client: MARKS[args.noun].dispatch(client, args), args)


def cmd_unstar(args: argparse.Namespace) -> int:
    """``oaken unstar ID...``: the same as ``oaken star remove ID...``."""
    args.action, args.rest = "remove", list(args.ids)
    return run(lambda client: MARKS["star"].dispatch(client, args), args)


# --- mailbox -------------------------------------------------------------------------------


def cmd_mailbox(args: argparse.Namespace) -> int:
    return run(lambda client: _mailbox(client, args), args)


async def _mailbox(client: ApiClient, args: argparse.Namespace) -> int:
    action = args.action
    if action == "tree":
        if args.account:
            raise _usage("tree is not per account yet; use 'counts' or 'messages' with --account")
        return await _tree(client, args)
    if action == "counts":
        return await _counts(client, args)
    if action == "messages":
        return await _messages(client, args)
    if action == "rules":
        if not args.id:
            raise _usage("usage: oaken mailbox rules MESSAGE_ID")
        return await _rules(client, args)
    raise _usage(f"mailbox has no {action!r}; try tree, counts, messages or rules")


async def _tree(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("list_mailbox", {})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        def line(name: str, c: dict[str, Any]) -> dict[str, Any]:
            return {"": name, "messages": c["total"], "unread": c["unread"]}

        rows = [line("Inbox", out["inbox"])]
        rows += [line("  " * (f["depth"] + 1) + f["name"], f["direct"]) for f in out["folders"]]
        rows += [line(f"# {lab['name']}", lab["count"]) for lab in out["labels"]]
        rows.append(line("Discarded", out["discarded"]))
        print_table(rows, ["", "messages", "unread"])
        if not out["folders"] and not out["labels"]:
            print_text(
                "no folders or labels yet: 'oaken folder create PATH', 'oaken label create NAME'"
            )

    return emit(args, data=result.output, text=text)


async def _counts(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {}
    if args.account:
        payload["account_id"] = args.account
    result = await client.call("mailbox_counts", payload)
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table

        rows = [
            {"": name, "messages": out[key]["total"], "unread": out[key]["unread"]}
            for name, key in (
                ("Inbox", "inbox"),
                ("Starred", "starred"),
                ("Discarded", "discarded"),
            )
        ]
        rows += [
            {"": f"folder {k}", "messages": v["total"], "unread": v["unread"]}
            for k, v in sorted(out["locations"].items())
        ]
        rows += [
            {"": f"label {k}", "messages": v["total"], "unread": v["unread"]}
            for k, v in sorted(out["labels"].items())
        ]
        print_table(rows, ["", "messages", "unread"])

    return emit(args, data=result.output, text=text)


async def _messages(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {}
    for field, value in (
        ("account_id", args.account),
        ("location", args.folder),
        ("label", args.label),
        ("limit", args.limit),
    ):
        if value:
            payload[field] = value
    # Mail moved into a folder is "filed", not "inbox", and a label can sit on either: so the
    # listing looks where the thing asked for is, unless --disposition says otherwise.
    where = args.disposition or ("filed" if args.folder else "any" if args.label else None)
    if where:
        payload["disposition"] = where
    if args.offset:
        payload["offset"] = args.offset
    if args.unread:
        payload["unread_only"] = True
    if args.starred:
        payload["starred"] = True
    return _render_messages(args, await client.call("list_messages", payload))


async def _rules(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("get_message_rules", {"message_id": args.id})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        attrs = out.get("attrs", [])
        if not attrs:
            print_text("no rule has marked this message")
            return
        print_table(
            [
                {"kind": a["kind"], "value": a["value"], "by": a["source"], "rule": a["rule"]}
                for a in attrs
            ],
            ["kind", "value", "by", "rule"],
        )

    return emit(args, data=result.output, text=text)
