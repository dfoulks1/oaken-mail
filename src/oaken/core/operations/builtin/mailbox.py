# SPDX-License-Identifier: Apache-2.0
"""The mailbox structure a client shows (folders nested under Inbox, and labels), and the person's
own moves and tags. Nothing here touches read state, and nothing here is offered to an AI client
yet: sorting someone's mail is theirs to enable."""

from pydantic import BaseModel, Field

from oaken.core.ids import AccountId, MessageId
from oaken.core.mailbox import SEP
from oaken.core.operations.builtin.system import Empty
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class Counts(BaseModel):
    total: int = 0
    unread: int = 0


class FolderOut(BaseModel):
    path: str  # relative to Inbox: "Lists/Dev" is Inbox/Lists/Dev
    name: str
    depth: int
    direct: Counts  # messages filed exactly here
    tree: Counts  # here and in every folder below it


class LabelOut(BaseModel):
    name: str
    count: Counts


class MailboxOutput(BaseModel):
    inbox: Counts
    discarded: Counts
    folders: list[FolderOut]
    labels: list[LabelOut]
    content_is_untrusted: bool = True  # a folder or label name can come from the mail's own Sieve


@operation(
    "list_mailbox",
    "The sidebar: Inbox and Discarded, the folder tree under Inbox, and labels, each with how many"
    " messages are in it and how many are unread. Empty folders and labels are included.",
    permission="read",
    input=Empty,
    output=MailboxOutput,
    mcp="on",
)
async def list_mailbox(ctx: OpContext, inp: Empty) -> MailboxOutput:
    mb = ctx.svc.need_mailbox()
    await mb.sync_derived()  # folders and labels Sieve made get a row, so they persist
    a = ctx.svc.attrs
    loc, loc_un = await a.counts("location"), await a.counts("location", unread_only=True)
    lab, lab_un = await a.counts("label"), await a.counts("label", unread_only=True)
    dis, dis_un = await a.counts("disposition"), await a.counts("disposition", unread_only=True)

    def below(path: str, counts: dict[str, int]) -> int:
        return sum(n for p, n in counts.items() if p == path or p.startswith(path + SEP))

    folders = [
        FolderOut(
            path=n.path,
            name=n.name,
            depth=n.depth,
            direct=Counts(total=loc.get(n.path, 0), unread=loc_un.get(n.path, 0)),
            tree=Counts(total=below(n.path, loc), unread=below(n.path, loc_un)),
        )
        for n in sorted(
            await mb.nodes("folder"), key=lambda n: [s.lower() for s in n.path.split(SEP)]
        )
    ]
    labels = [
        LabelOut(name=n.path, count=Counts(total=lab.get(n.path, 0), unread=lab_un.get(n.path, 0)))
        for n in await mb.nodes("label")
    ]
    return MailboxOutput(
        inbox=Counts(total=dis.get("inbox", 0), unread=dis_un.get("inbox", 0)),
        discarded=Counts(total=dis.get("discarded", 0), unread=dis_un.get("discarded", 0)),
        folders=folders,
        labels=labels,
    )


class CreateFolderInput(BaseModel):
    path: str = Field(min_length=1, max_length=400)  # "Lists/Dev"; a leading "Inbox/" is dropped


class CreateFolderOutput(BaseModel):
    path: str
    created: list[str]  # the folders that are new (empty if it already existed)


@operation(
    "create_folder",
    "Make a folder under Inbox, and any folders above it. A path that already exists is fine.",
    permission="mutate",
    input=CreateFolderInput,
    output=CreateFolderOutput,
    mcp="never",
)
async def create_folder(ctx: OpContext, inp: CreateFolderInput) -> CreateFolderOutput:
    path, created = await ctx.svc.need_mailbox().create_folder(inp.path)
    return CreateFolderOutput(path=path, created=created)


class CreateLabelInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateLabelOutput(BaseModel):
    name: str
    created: bool


@operation(
    "create_label",
    "Make a label (a tag for finding mail). A label that already exists is fine.",
    permission="mutate",
    input=CreateLabelInput,
    output=CreateLabelOutput,
    mcp="never",
)
async def create_label(ctx: OpContext, inp: CreateLabelInput) -> CreateLabelOutput:
    name, created = await ctx.svc.need_mailbox().create_label(inp.name)
    return CreateLabelOutput(name=name, created=created)


class MoveInput(BaseModel):
    ids: list[str] = Field(min_length=1)
    path: str | None = None  # a folder (made if new), or null / "Inbox" to put them back


class MoveOutput(BaseModel):
    path: str | None
    moved: list[str]


@operation(
    "move_to_folder",
    "File messages into a folder under Inbox (made if new), or back into the Inbox. The person's"
    " choice beats a Sieve rule's.",
    permission="mutate",
    input=MoveInput,
    output=MoveOutput,
    mcp="never",
)
async def move_to_folder(ctx: OpContext, inp: MoveInput) -> MoveOutput:
    ids = [MessageId(i) for i in ctx.limits.ids(inp.ids)]
    path, moved = await ctx.svc.need_mailbox().move(ids, inp.path)
    return MoveOutput(path=path, moved=moved)


class TagInput(BaseModel):
    ids: list[str] = Field(min_length=1)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class TagOutput(BaseModel):
    changed: list[str]


@operation(
    "set_labels",
    "Add labels to messages (made if new) and remove labels the person added earlier.",
    permission="mutate",
    input=TagInput,
    output=TagOutput,
    mcp="never",
)
async def set_labels(ctx: OpContext, inp: TagInput) -> TagOutput:
    ids = [MessageId(i) for i in ctx.limits.ids(inp.ids)]
    changed = await ctx.svc.need_mailbox().tag(ids, inp.add, inp.remove)
    return TagOutput(changed=changed)


class CountsInput(BaseModel):
    account_id: str | None = None  # merged across accounts when omitted
    # discarded is excluded from locations/labels/starred by default
    include_discarded: bool = False


class MailboxCounts(BaseModel):
    inbox: Counts = Counts()
    starred: Counts = Counts()
    discarded: Counts = Counts()
    locations: dict[str, Counts] = Field(default_factory=dict)
    labels: dict[str, Counts] = Field(default_factory=dict)
    content_is_untrusted: bool = True  # a folder or label name can come from the mail's own Sieve


@operation(
    "mailbox_counts",
    "The sidebar's numbers in one call: Inbox, Starred and Discarded, every folder (location) and"
    " every label, each with how many messages and how many are unread. No mail content.",
    permission="read",
    input=CountsInput,
    output=MailboxCounts,
    mcp="on",
)
async def mailbox_counts(ctx: OpContext, inp: CountsInput) -> MailboxCounts:
    account = AccountId(inp.account_id) if inp.account_id else None
    g = await ctx.svc.attrs.mailbox_counts(
        account_id=account, include_discarded=inp.include_discarded
    )

    def c(group: str, key: str) -> Counts:
        total, unread = g.get(group, {}).get(key, (0, 0))
        return Counts(total=total, unread=unread)

    return MailboxCounts(
        inbox=c("disposition", "inbox"),
        starred=c("starred", "starred"),
        discarded=c("disposition", "discarded"),
        locations={
            k: Counts(total=t, unread=u) for k, (t, u) in sorted(g.get("location", {}).items())
        },
        labels={k: Counts(total=t, unread=u) for k, (t, u) in sorted(g.get("label", {}).items())},
    )


OPERATIONS: tuple[AnyOperation, ...] = (
    list_mailbox,
    mailbox_counts,
    create_folder,
    create_label,
    move_to_folder,
    set_labels,
)
__all__ = ["OPERATIONS"]
