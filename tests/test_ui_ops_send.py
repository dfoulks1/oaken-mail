# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.cli.client import CallResult
from oaken_core.cli.exits import CliError
from oaken_webui_core import ui_ops


class Client:
    def __init__(self):
        self.calls = []

    async def call(self, name, payload=None, *, confirm=False, **_):
        self.calls.append((name, payload, confirm))
        return CallResult("r1", output={"message_id": "<m@x>"})


async def test_send_calls_the_accounts_own_send_operation():
    c = Client()
    out = await ui_ops.run(c, "send", {"account": "work", "to": "a@x.org, b@x.org", "bcc": ["c@x.org"], "subject": "hi", "body": "yo"})
    assert out == {"message_id": "<m@x>"}
    assert c.calls == [("work.send", {"to": ["a@x.org", "b@x.org"], "cc": [], "bcc": ["c@x.org"], "subject": "hi", "body": "yo"}, True)]


async def test_send_needs_an_account_and_a_recipient():
    for bad in ({"to": "a@x.org"}, {"account": "work"}):
        with pytest.raises(CliError):
            await ui_ops.run(Client(), "send", bad)


async def test_list_senders_is_every_instance_with_a_send_operation():
    from oaken_core.cli.client import OpInfo

    class C:
        async def list_ops(self):
            return [OpInfo("work.send", "", "mutate", template="gmail"), OpInfo("mail.list", "", "read"), OpInfo("out.send", "", "mutate", template="smtp")]

    assert await ui_ops.run(C(), "list_senders", {}) == {"senders": [{"account": "out", "provider": "smtp"}, {"account": "work", "provider": "gmail"}]}


class StageClient:
    def __init__(self):
        self.calls = []

    async def call(self, name, payload=None, *, confirm=False, **_):
        self.calls.append((name, payload))
        if name == "queue_list":
            return CallResult("r", output={"queues": [{"queue": "default"}, {"queue": "triage"}]})
        kinds = {"default": "formatter", "triage": "triager"}
        q = (payload or {}).get("queue")
        if name == "queue_get":
            return CallResult("r", output={"queue": q, "title": q.title(), "kind": kinds[q], "workers": 2, "paused": False, "queued": 3, "running": 1,
                                           "failed": 2 if q == "triage" else 0, "succeeded_in_window": 5, "last_error": None})
        return CallResult("r", output={})


async def test_stage_health_groups_queues_by_kind_and_flags_failures():
    out = await ui_ops.run(StageClient(), "stage_health", {})
    by = {s["stage"]: s for s in out["stages"]}
    assert by["intake"]["queues"][0]["default"] is True and by["intake"]["failed"] == 0
    assert by["triage"]["failed"] == 2 and by["enrich"]["queues"] == []


async def test_stage_workers_edits_only_when_changed_and_stays_in_bounds():
    c = StageClient()
    await ui_ops.run(c, "stage_set_workers", {"stage": "triage", "workers": 4})
    assert ("queue_edit", {"queue": "triage", "workers": 4}) in c.calls
    with pytest.raises(CliError):
        await ui_ops.run(StageClient(), "stage_set_workers", {"stage": "triage", "workers": 0})
