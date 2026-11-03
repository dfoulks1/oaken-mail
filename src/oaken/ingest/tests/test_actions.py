# SPDX-License-Identifier: Apache-2.0
"""The action kind (ADR 0019): triage routes a message, the action runs, an action that waits for a
yes parks until it is given, and a route nothing answers parks."""

from typing import Any, cast

import pytest

from oaken.ingest.pipeline import ACTION, action_kind
from oaken.ingest.tests.conftest import Options, mail
from oaken.plugins.base import BaseAction, BaseTriager
from oaken.plugins.contracts import ActionResult, CleanMessage, Triager, TriageVerdict
from oaken.testing.contracts import ActionContract


class Router(BaseTriager):
    """Rates every message and routes it by ``route``."""

    def __init__(self, route: list[str]) -> None:
        self.route = route

    async def triage(self, msg: CleanMessage) -> TriageVerdict | None:
        return TriageVerdict(
            {
                "category": "work", "priority": 2, "needs_reply": False,
                "waiting_on_others": False, "reason": "", "source": "custom", "route": self.route,
            }
        )  # fmt: skip


class Saver(BaseAction):
    routes = ("save",)

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def act(self, msg: CleanMessage, route: str) -> ActionResult | None:
        self.seen.append(msg.message_id)
        return ActionResult({"saved": True})


async def start(rig: Any, route: list[str], saver: Saver | None = None) -> tuple[Saver, str]:
    saver = saver or Saver()
    rig.pipeline.backends.triagers = lambda: {"router": cast(Triager, Router(route))}
    rig.pipeline.backends.actions = lambda: {"saver": saver}
    rig.pipeline.register_actions(rig.runner)
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain(rounds=4)
    return saver, await rig.mid(pid)


async def test_an_action_waits_for_a_yes_by_default_and_runs_when_approved(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        saver, mid = await start(rig, ["save"])
        assert saver.seen == []  # nothing runs without a yes
        (parked,) = await rig.queue.query(kind=action_kind("saver"), status="failed")
        assert "awaiting approval" in (parked.last_error or "")
        assert [w["kind"] for w in await rig.pipeline.waiting_actions()] == ["saver"]
        assert await rig.pipeline.approve_action(mid, "saver", "save")
        await rig.drain(rounds=3)
        assert saver.seen == [mid]
        (row,) = await rig.pipeline.action_rows(mid)
        assert (row["action"], row["route"], row["status"]) == ("saver", "save", "done")


async def test_an_auto_action_runs_without_waiting(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rig.pipeline.settings.plugins["saver"] = {"approval": "auto"}
        saver, mid = await start(rig, ["save"])
        assert saver.seen == [mid]
        assert await rig.queue.query(kind=action_kind("saver"), status="failed") == []


async def test_a_route_nothing_answers_parks_and_a_later_install_releases_it(
    make_rig: Any,
) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rig.pipeline.settings.plugins["saver"] = {"approval": "auto"}
        saver, mid = await start(rig, ["elsewhere"])
        (parked,) = await rig.queue.query(kind=ACTION, status="failed")
        assert "no action answers" in (parked.last_error or "")
        Saver.routes = ("elsewhere",)  # the action that answers it is installed later
        try:
            await rig.queue.retry(parked.id)
            await rig.drain(rounds=3)
        finally:
            Saver.routes = ("save",)
        assert saver.seen == [mid]


class TestSaverContract(ActionContract):
    @pytest.fixture
    def action(self) -> Saver:
        return Saver()
