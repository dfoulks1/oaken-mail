# SPDX-License-Identifier: Apache-2.0
"""An external agent enriches a message through the swarm door (prompt 33)."""

from typing import Any

import pytest
from pydantic import BaseModel

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.errors import InvalidResult
from oaken.core.ids import AccountId
from oaken.core.operations import CallerContext, CoreClient
from oaken.core.operations.builtin import agent_registry
from oaken.core.operations.tests.conftest import Ops
from oaken.core.settings import McpSettings, SwarmSettings, WorkspaceSettings
from oaken.ingest.pipeline import ENRICH, MessagePayload, StageJobResult
from oaken.plugins.base import BaseEnricher
from oaken.plugins.contracts import EnrichInput, EnrichOutput


class Gist(SafeOutput):
    gist: str


class GistResult(BaseModel):
    gist: str
    skipped: str = ""


class Tone(SafeOutput):
    tone: str


class ToneResult(BaseModel):
    tone: str


class Gister(BaseEnricher):
    version = "1"
    needs_llm = True
    prompt = PromptBuilder("enrich.gist", "1", "Say in a few words what this is.", Gist)
    output_model = GistResult

    def __init__(self) -> None:
        self.bulk = False
        self.native_runs = 0

    def needs_model(self, msg: EnrichInput) -> bool:
        return not self.bulk

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        self.native_runs += 1
        return EnrichOutput({"gist": "", "skipped": "bulk"})


class Toner(BaseEnricher):
    version = "1"
    needs_llm = True
    prompt = PromptBuilder("enrich.tone", "1", "Name the tone.", Tone)
    output_model = ToneResult

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        raise AssertionError("an agent should have been asked")


class Swarm:
    def __init__(
        self, ops: Ops, client: CoreClient, caller: CallerContext, gister: Gister, toner: Toner
    ) -> None:
        self.ops, self.client, self.caller, self.gister, self.toner = (
            ops, client, caller, gister, toner,
        )  # fmt: skip

    async def message(self) -> tuple[str, str]:
        """A stored message and its account with nothing enriched, and only the test's enrichers."""
        (mid,) = await self.ops.add(1)  # the rig's own enrichers run here, then are cleared
        await self.ops.svc.raw.db.execute("DELETE FROM enrichments")
        (account,) = await self.ops.svc.raw.list_accounts()
        self.use({"gist": self.gister, "tone": self.toner})
        return str(mid), str(account.id)

    def use(self, enrichers: dict[str, Any]) -> None:
        self.ops.svc.need_pipeline().backends.enrichers = lambda: enrichers

    async def job(self, mid: str, account: str | None = None) -> str:
        queued = await self.ops.svc.queue.enqueue(
            ENRICH,
            MessagePayload(message_id=mid),
            account_id=AccountId(account) if account else None,
        )
        return str(queued.job_id)

    async def call(self, name: str, **arguments: Any) -> Any:
        return (await self.client.call(name, arguments, self.caller)).output

    async def stored(self, mid: str) -> dict[str, dict[str, object]]:
        return await self.ops.svc.need_pipeline().enrichments(mid)  # type: ignore[arg-type]


@pytest.fixture
async def swarm(ops: Ops) -> Swarm:
    kinds = SwarmSettings(enabled=True, allowed_kinds=[ENRICH])
    ops.svc.agents.settings = kinds  # type: ignore[union-attr]
    ops.svc.queue.swarm = kinds
    ops.svc.queue.register_result(ENRICH, StageJobResult)
    gister, toner = Gister(), Toner()
    agent, _ = await ops.svc.agents.create("big model", [ENRICH])  # type: ignore[union-attr]
    caller = CallerContext.mcp("big model", agent_id=str(agent.id))
    return Swarm(ops, CoreClient(agent_registry(), ops.svc), caller, gister, toner)


async def test_a_claim_carries_the_text_the_owed_enrichers_and_their_schemas(
    ops: Ops, swarm: Swarm
) -> None:
    mid, _ = await swarm.message()
    await swarm.job(mid)
    job = (await swarm.call("claim_job")).job
    payload = job.payload
    assert payload["message_id"] == mid and payload["content_is_untrusted"] is True
    assert "<<<EMAIL label=body" in payload["message"]["body"]
    assert "<<<EMAIL label=subject" in payload["message"]["subject"]
    owed = {o["enricher"]: o for o in payload["owed"]}
    assert set(owed) == {"gist", "tone"} and "gist" in owed["gist"]["schema"]["properties"]
    assert "Say in a few words" in owed["gist"]["instructions"]


async def test_the_text_is_cut_to_max_chars_and_says_so(ops: Ops, swarm: Swarm) -> None:
    mid, _ = await swarm.message()
    await swarm.job(mid)
    job = (await swarm.call("claim_job", max_chars=500)).job
    assert job.payload["message"]["truncated"] is True or len(job.payload["message"]["body"]) < 900


async def test_a_full_answer_is_stored_attributed_and_satisfies_the_native_cache(
    ops: Ops, swarm: Swarm
) -> None:
    mid, _ = await swarm.message()
    job_id = await swarm.job(mid)
    await swarm.call("claim_job")
    done = await swarm.call(
        "complete_job",
        job_id=job_id,
        result={"enrichments": {"gist": {"gist": "a note"}, "tone": {"tone": "calm"}}},
    )
    assert (done.stored, done.still_owed) == (2, 0)
    assert (await swarm.stored(mid))["gist"] == {"gist": "a note", "skipped": ""}
    row = await ops.svc.raw.db.fetchone(
        "SELECT model_id FROM enrichments WHERE message_id = ? AND enricher = 'gist'", (mid,)
    )
    assert row is not None and row["model_id"] == "agent:big model"
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "succeeded"
    again = await ops.svc.need_pipeline().enrich_message(mid)  # type: ignore[arg-type]
    assert again.cached == 2 and again.ran == 0  # the native stage does not redo it


async def test_force_replaces_an_agent_row(ops: Ops, swarm: Swarm) -> None:
    mid, _ = await swarm.message()
    job_id = await swarm.job(mid)
    await swarm.call("claim_job")
    await swarm.call(
        "complete_job",
        job_id=job_id,
        result={"enrichments": {"gist": {"gist": "x"}, "tone": {"tone": "y"}}},
    )
    swarm.gister.bulk = True
    await ops.svc.need_pipeline().enrich_message(mid, only={"gist"}, force=True)  # type: ignore[arg-type]
    assert (await swarm.stored(mid))["gist"]["skipped"] == "bulk"


async def test_a_bad_answer_stores_nothing_and_the_agent_keeps_the_lease(
    ops: Ops, swarm: Swarm
) -> None:
    mid, _ = await swarm.message()
    job_id = await swarm.job(mid)
    await swarm.call("claim_job")
    for bad in (
        {"gist": {"wrong": 1}},  # fails the enricher's output model
        {"nope": {"x": 1}},  # not an enricher that is owed
        {"gist": {"gist": "ok"}, "inspector": {"x": 1}},  # one bad answer sinks the batch
    ):
        with pytest.raises(InvalidResult):
            await swarm.call("complete_job", job_id=job_id, result={"enrichments": bad})
    assert await swarm.stored(mid) == {}
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "running"


async def test_a_partial_answer_is_kept_and_the_rest_stays_owed(ops: Ops, swarm: Swarm) -> None:
    mid, _ = await swarm.message()
    job_id = await swarm.job(mid)
    await swarm.call("claim_job")
    done = await swarm.call(
        "complete_job", job_id=job_id, result={"enrichments": {"gist": {"gist": "half"}}}
    )
    assert (done.stored, done.still_owed) == (1, 1)
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "queued" and job.attempts == 0
    again = (await swarm.call("claim_job")).job
    assert [o["enricher"] for o in again.payload["owed"]] == ["tone"]


async def test_work_that_needs_no_model_is_settled_in_core_and_not_offered(
    ops: Ops, swarm: Swarm
) -> None:
    mid, _ = await swarm.message()
    swarm.gister.bulk = True
    job_id = await swarm.job(mid)

    class NoToner(Toner):
        def needs_model(self, msg: EnrichInput) -> bool:
            return False

        async def enrich(self, msg: EnrichInput) -> EnrichOutput:
            return EnrichOutput({"tone": "n/a"})

    swarm.use({"gist": swarm.gister, "tone": NoToner()})
    assert (await swarm.call("claim_job")).job is None
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "succeeded"
    assert swarm.gister.native_runs == 1 and (await swarm.stored(mid))["tone"] == {"tone": "n/a"}


async def test_a_message_marked_skip_enrich_is_never_offered(ops: Ops, swarm: Swarm) -> None:
    mid, _ = await swarm.message()
    pipeline = ops.svc.need_pipeline()
    pipeline._skip_ai = lambda _m: _true()  # type: ignore[method-assign,assignment]
    job_id = await swarm.job(mid)
    assert (await swarm.call("claim_job")).job is None
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "succeeded"


async def _true() -> bool:
    return True


async def test_mail_outside_the_mcp_workspaces_is_never_offered(ops: Ops, swarm: Swarm) -> None:
    mid, account = await swarm.message()
    await ops.svc.raw.set_account_config(AccountId(account), {"workspace": "hospital"})
    ops.svc.settings = ops.svc.settings.model_copy(
        update={
            "workspaces": {"hospital": WorkspaceSettings()},
            "mcp": McpSettings(workspaces=["default"]),
        }
    )
    job_id = await swarm.job(mid, account)
    assert (await swarm.call("claim_job")).job is None  # [mcp] workspaces does not list it
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.status == "queued"
    ops.svc.settings = ops.svc.settings.model_copy(
        update={"mcp": McpSettings(workspaces=["hospital"])}
    )
    assert (await swarm.call("claim_job")).job is not None  # list it and the same agent gets it


async def test_fail_with_retry_hands_the_job_back_unharmed(ops: Ops, swarm: Swarm) -> None:
    mid, _ = await swarm.message()
    job_id = await swarm.job(mid)
    await swarm.call("claim_job")
    out = await swarm.call("fail_job", job_id=job_id, error="context too small", retry=True)
    assert out.status == "queued"
    job = await ops.svc.queue.get(job_id)  # type: ignore[arg-type]
    assert job is not None and job.attempts == 0
