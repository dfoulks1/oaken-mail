# SPDX-License-Identifier: Apache-2.0
"""``inspector``: a deterministic report on a message's headers and MIME structure. No model.

Ported in spirit from the legacy header and MIME analyzers, with their mistakes fixed: the hop
count comes from the ``Received`` headers (the old estimate counted ``by `` inside
``Authentication-Results``), a message is not flagged merely for being old (archived mail would
always be), and ``Reply-To`` is compared by address and domain, not as raw text.

Findings are short codes in ``warnings`` so a client can word them; nothing here is mail text
except filenames and a few header values, which are data like the rest of the mail.
"""

import re
from datetime import datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Literal

from pydantic import BaseModel, Field

from oaken.plugins.base import BaseEnricher
from oaken.plugins.contracts import (
    AskIntent,
    EnrichAttachment,
    EnrichInput,
    EnrichOutput,
    PluginContext,
)
from oaken_enrichers._common import bulk_reasons, header_values

LONG_PATH_HOPS = 8
SKEW = timedelta(days=2)
VALUE_CAP = 200
RISKY_EXTENSIONS = frozenset(
    "exe bat cmd com scr msi dll sh ps1 vbs vbe js jse wsf hta lnk jar docm xlsm pptm".split()
)
SPAM_HEADERS = ("x-spam-flag", "x-spam-score", "x-spam-status")
AuthResult = str  # pass, fail, softfail, neutral, none, temperror, permerror, ... or "absent"
_AUTH = re.compile(r"^\s*(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.I)
_DECEPTIVE = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


class Authentication(BaseModel):
    spf: AuthResult = "absent"
    dkim: AuthResult = "absent"
    dmarc: AuthResult = "absent"
    arc: Literal["present", "absent"] = "absent"


class AttachmentReport(BaseModel):
    filename: str | None
    content_type: str
    size: int
    risky: bool
    reasons: list[str] = Field(default_factory=list)


class InspectorResult(BaseModel):
    authentication: Authentication
    spam_headers: dict[str, str] = Field(default_factory=dict)
    hops: int = 0
    list_headers: dict[str, str] = Field(default_factory=dict)
    bulk_signals: list[str] = Field(default_factory=list)
    reply_to_mismatch: bool = False
    reply_to_domain_mismatch: bool = False
    attachments: list[AttachmentReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def authentication(headers: dict[str, tuple[str, ...]]) -> Authentication:
    """SPF, DKIM and DMARC from ``Authentication-Results``. The topmost header is the one the
    receiving server added, so it is the one believed; within it a DKIM check passes if any
    signature did (mail is often signed twice)."""
    found: dict[str, list[str]] = {}
    top = headers.get("authentication-results", ())
    for part in (top[0] if top else "").split(";"):
        m = _AUTH.match(part)
        if m:
            found.setdefault(m.group(1).lower(), []).append(m.group(2).lower())
    out = Authentication(arc="present" if headers.get("arc-seal") else "absent")
    for proto in ("spf", "dkim", "dmarc"):
        results = found.get(proto)
        if results:
            setattr(out, proto, "pass" if "pass" in results else results[0])
    return out


def hops(headers: dict[str, tuple[str, ...]]) -> int:
    """Servers the message passed through: the number of ``Received`` headers."""
    return len(headers.get("received", ()))


def _when(value: str) -> datetime | None:
    try:
        when = parsedate_to_datetime(value.rsplit(";", 1)[-1].strip())
    except (TypeError, ValueError, IndexError):
        return None
    return when if when.tzinfo else None


def _domain(address: str) -> str:
    return address.rpartition("@")[2].lower()


def reply_to_check(headers: dict[str, tuple[str, ...]]) -> tuple[bool, bool]:
    """(Reply-To names an address the From does not, Reply-To's domain is not From's)."""
    reply = {a.lower() for _, a in getaddresses(headers.get("reply-to", ())) if a}
    sender = {a.lower() for _, a in getaddresses(headers.get("from", ())) if a}
    if not reply or reply <= sender:
        return False, False
    return True, not {_domain(a) for a in reply} <= {_domain(a) for a in sender}


def attachment_report(att: EnrichAttachment) -> AttachmentReport:
    name = att.filename.unwrap() if att.filename else None
    reasons: list[str] = []
    if name:
        parts = name.lower().rsplit(".", 1)
        ext = parts[1] if len(parts) == 2 else ""
        if ext in RISKY_EXTENSIONS:
            reasons.append(f"risky-extension:{ext}")
        stem_ext = parts[0].rsplit(".", 1)[-1] if len(parts) == 2 and "." in parts[0] else ""
        if ext in RISKY_EXTENSIONS and stem_ext in {
            "pdf",
            "doc",
            "docx",
            "jpg",
            "png",
            "txt",
            "xls",
        }:
            reasons.append("double-extension")
        if any(c in _DECEPTIVE for c in name):
            reasons.append("deceptive-name")
    if att.quarantine:
        reasons.append("quarantined")
    return AttachmentReport(
        filename=name, content_type=att.content_type, size=att.size, risky=bool(reasons),
        reasons=reasons,
    )  # fmt: skip


def inspect(msg: EnrichInput, now: datetime) -> InspectorResult:
    h = header_values(msg)
    auth = authentication(h)
    warnings: list[str] = []
    for proto in ("spf", "dkim", "dmarc"):
        value = getattr(auth, proto)
        if value not in ("pass", "absent"):
            warnings.append(f"{proto}-{value}")
    spam = {k: h[k][0].strip()[:VALUE_CAP] for k in SPAM_HEADERS if h.get(k)}
    if spam.get("x-spam-flag", "").upper() == "YES":
        warnings.append("spam-flagged")
    count = hops(h)
    if count > LONG_PATH_HOPS:
        warnings.append("long-delivery-path")
    if msg.date is not None and msg.date - now > SKEW:
        warnings.append("date-in-future")
    received = [w for v in h.get("received", ()) if (w := _when(v)) is not None]
    if msg.date is not None and received and abs(msg.date - min(received)) > SKEW:
        warnings.append("date-received-skew")  # the Date header disagrees with the first hop
    elif msg.date is None and h.get("date"):
        warnings.append("date-unparseable")
    addr_diff, domain_diff = reply_to_check(h)
    if domain_diff:
        warnings.append("reply-to-other-domain")
    elif addr_diff:
        warnings.append("reply-to-differs")
    files = [attachment_report(a) for a in msg.attachments]
    if any(f.risky for f in files):
        warnings.append("risky-attachment")
    return InspectorResult(
        authentication=auth,
        spam_headers=spam,
        hops=count,
        list_headers={
            k: v[0].strip()[:VALUE_CAP] for k, v in h.items() if k.startswith("list-") and v
        },
        bulk_signals=list(bulk_reasons(msg)),
        reply_to_mismatch=addr_diff,
        reply_to_domain_mismatch=domain_diff,
        attachments=files,
        warnings=warnings,
    )


class InspectorEnricher(BaseEnricher):
    version = "1"
    needs_llm = False
    output_model = InspectorResult
    ask_intent = AskIntent(
        "inspector",
        "The reader asks whether mail is authentic, spam or suspicious.",
        ("warnings", "authentication", "spam_headers"),
    )

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        return EnrichOutput(inspect(msg, self.ctx.clock.now()).model_dump(mode="json"))


__all__ = ["InspectorEnricher", "InspectorResult", "inspect"]
