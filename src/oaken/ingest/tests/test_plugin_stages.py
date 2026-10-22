# SPDX-License-Identifier: Apache-2.0
"""A plugin adds a pipeline stage that runs after the enrichers (step 34.5b).

The stage sees the message and its enrichment results, returns JSON-serializable data, and core
stores it with the same content-hash idempotency as an enricher — so a reprocess is a cache hit.
"""

from typing import Any

from oaken.core.ids import MessageId
from oaken.ingest.tests.conftest import Options, mail
from oaken.plugins.contracts import PluginStage, PluginStageContext, PluginStageInput
from oaken.plugins.host import OfferedPipelineStage


def _tag_stage() -> tuple[list[OfferedPipelineStage], Any, list[PluginStageInput]]:
    """A 'tag' stage that records what it was called with and derives one tag from the body."""
    seen: list[PluginStageInput] = []

    async def handler(ctx: PluginStageContext, msg: PluginStageInput) -> dict[str, Any]:
        seen.append(msg)
        return {"tag": "quokka" if "quokka" in msg.body.unwrap().lower() else "other"}

    stage = PluginStage("tag", handler, version="1")
    offered = [OfferedPipelineStage("tagger", stage)]

    def stage_context(plugin: str, name: str) -> PluginStageContext:
        from oaken.core.clock import FakeClock

        return PluginStageContext(plugin, name, config={}, clock=FakeClock(), log=ctx_log())

    return offered, stage_context, seen


def ctx_log() -> Any:
    from oaken.core.logging import get_logger

    return get_logger("test.stage")


async def _stored_stage(rig: Any, mid: MessageId) -> dict[str, Any] | None:
    row = await rig.raw.db.fetchone(
        "SELECT data_json FROM enrichments WHERE message_id = ? AND enricher = ?",
        (mid, "plugin.tagger.tag"),
    )
    if row is None:
        return None
    import json

    return dict(json.loads(row["data_json"]))


async def test_a_plugin_stage_runs_after_enrich_and_is_cached_on_rerun(make_rig: Any) -> None:
    offered, stage_context, seen = _tag_stage()
    async with make_rig(Options(embedder=False)) as rig:
        # Inject the plugin stage into the pipeline's backends (the host does this live).
        rig.pipeline.backends.pipeline_stages = lambda: offered
        rig.pipeline.backends.stage_context = stage_context

        pid = rig.provider.add_message(mail(1))  # body mentions quokkas
        await rig.run_sync()
        await rig.drain(rounds=2)
        mid = await rig.mid(pid)

        stored = await _stored_stage(rig, mid)
        assert stored == {"tag": "quokka"}, "the stage did not run after enrich"
        assert len(seen) == 1 and seen[0].message_id == mid
        # the stage saw the enricher's result too (FakeEnricher ran under 'words')
        assert "words" in seen[0].enrichments

        # Rerun enrich: the stage is a cache hit, the handler is not called again.
        await rig.pipeline.enrich_message(mid)
        assert len(seen) == 1, "the stage recomputed instead of hitting the cache"


async def test_no_plugin_stages_is_a_no_op(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        pid = rig.provider.add_message(mail(2))
        await rig.run_sync()
        await rig.drain(rounds=2)
        mid = await rig.mid(pid)
        # nothing stored under a plugin stage, and enrich still succeeded
        assert await _stored_stage(rig, mid) is None
