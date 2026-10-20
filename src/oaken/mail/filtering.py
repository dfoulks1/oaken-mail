# SPDX-License-Identifier: Apache-2.0
"""Run the user's Sieve scripts over stored mail and keep the result in ``raw.db``.

``FilterEngine`` reads the raw bytes, parses them, runs the scripts and replaces the message's
``sieve`` attributes in one transaction. It never calls a model and never opens an attachment.
A message with no filter run, or one made by a different script set, is stale; ``refilter`` walks
those. Changing a script therefore re-derives organization without touching the mail.
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from oaken.core import paths
from oaken.core.attrs import Attr, AttrStore
from oaken.core.clock import Clock
from oaken.core.events import EventBus, MessageFiltered
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.settings import MailSettings
from oaken.mail.parse import parse
from oaken.mail.sieve import Script, SieveError, SieveResult, compile_script, run, view_of

log = get_logger("oaken.filtering")

SOURCE = "sieve"


@dataclass(frozen=True)
class LoadReport:
    scripts: tuple[Script, ...]
    problems: tuple[tuple[str, str], ...]  # (file name, what is wrong); these scripts are skipped
    sha256: str


@dataclass
class RefilterResult:
    filtered: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def scripts_dir(settings: MailSettings) -> Path:
    d = Path(settings.sieve.dir).expanduser()
    return d if d.is_absolute() else paths.config_dir() / d


def load_scripts(directory: Path, settings: MailSettings) -> LoadReport:
    """Compile every ``*.sieve`` in name order. A script with an error is reported and skipped, so
    one typo never switches off the rest."""
    cfg = settings.sieve
    scripts: list[Script] = []
    problems: list[tuple[str, str]] = []
    if cfg.enabled and directory.is_dir():
        for f in sorted(directory.glob("*.sieve")):
            if not f.is_file():
                continue
            try:
                if f.stat().st_size > cfg.max_script_bytes:
                    raise SieveError(f"script is larger than {cfg.max_script_bytes} bytes")
                scripts.append(
                    compile_script(
                        f.name,
                        f.read_text(encoding="utf-8"),
                        max_bytes=cfg.max_script_bytes,
                    )
                )
            except (SieveError, OSError, UnicodeDecodeError) as exc:
                problems.append((f.name, getattr(exc, "message", str(exc))))
    digest = hashlib.sha256()
    for s in scripts:
        digest.update(f"{s.name}\0{s.sha256}\0".encode())
    return LoadReport(tuple(scripts), tuple(problems), digest.hexdigest())


class FilterEngine:
    def __init__(
        self,
        store: RawStore,
        attrs: AttrStore,
        settings: MailSettings,
        clock: Clock,
        *,
        directory: Path | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.store, self.attrs, self.settings, self.clock, self.bus = (
            store,
            attrs,
            settings,
            clock,
            bus,
        )
        self.directory = directory or scripts_dir(settings)
        self.report = self.reload()

    def reload(self) -> LoadReport:
        """Read the scripts again (after the user edited them)."""
        self.report = load_scripts(self.directory, self.settings)
        for name, problem in self.report.problems:
            log.warning("sieve script skipped", script=name, problem=problem)
        return self.report

    async def filter_message(self, message_id: MessageId) -> SieveResult:
        """Run the scripts on one message and store the outcome. No scripts means the message is
        simply in the inbox, and that is recorded too (so it is not stale)."""
        msg = await self.store.get_message(message_id)
        raw = await self.store.get_raw(message_id)
        report = self.report
        parsed = await asyncio.to_thread(parse, raw, self.settings)
        view = view_of(parsed, size=len(raw), now=self.clock.now())
        result = await asyncio.to_thread(run, report.scripts, view, self.settings.sieve)
        attrs = [Attr("disposition", result.disposition, SOURCE, result.disposition_rule)]
        attrs += [Attr(e.kind, e.value, SOURCE, e.rule) for e in result.effects]
        await self.attrs.replace(
            message_id,
            SOURCE,
            attrs,
            scripts_sha256=report.sha256,
            errors=result.errors,
        )
        log.info(
            "message filtered",
            message_id=message_id,
            disposition=result.disposition,
            actions=len(result.effects),
            errors=len(result.errors),
        )
        if self.bus:
            await self.bus.publish(
                MessageFiltered(
                    ts=self.clock.now(),
                    account_id=msg.account_id,
                    message_id=message_id,
                    disposition=result.disposition,
                )
            )
        return result

    async def refilter(
        self, account_id: AccountId | None = None, *, limit: int | None = None
    ) -> RefilterResult:
        """Filter every stale message (oldest first). Resumable: it only ever picks up messages
        whose last run is missing or made by other scripts."""
        out = RefilterResult()
        after: MessageId | None = None
        sha = self.report.sha256
        while limit is None or out.filtered + out.failed < limit:
            batch = await self.attrs.stale(sha, account_id=account_id, after=after, limit=200)
            if not batch:
                break
            for mid in batch:
                after = mid
                try:
                    await self.filter_message(mid)
                    out.filtered += 1
                except Exception as exc:  # noqa: BLE001 - one bad message must not stop the sweep
                    out.failed += 1
                    out.errors.append(f"{mid}: {type(exc).__name__}")
                    log.warning("filtering failed", message_id=mid, error=type(exc).__name__)
                if limit is not None and out.filtered + out.failed >= limit:
                    break
        return out
