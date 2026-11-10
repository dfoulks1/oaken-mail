# SPDX-License-Identifier: Apache-2.0
from oaken_core.cli.client import CallResult
from oaken_core.cli.exits import CliError
from oaken_webui_core import ui_ops

ROW = {"subject_id": "s1", "thread_id": "t", "sent_at": "2026-11-01T10:00:00+00:00", "from_addr": "ann@example.org", "subject": "Invoice", "is_read": False, "attachments": 1}
GET = {**ROW, "source": "gmail:me", "item_key": "k", "to": ["bob@example.org", "cc@example.org"], "parts": {
    "headers": {"from": ["Ann Lee <ann@example.org>"], "to": ["Bob <bob@example.org>"], "cc": ["cc@example.org"], "message-id": ["<1@x>"]},
    "text": "Please   pay\nby Friday.", "html": "<p>Please pay</p>", "attachment-1": {"filename": "i.pdf", "content_type": "application/pdf", "size": 2048}}}
LABELS = {"labels": [{"key": "mail.starred", "effective": "true"}, {"key": "mail.folder", "effective": "Work"}, {"key": "sieve.labels", "effective": "a, b"},
                     {"key": "spamfilter.verdict", "effective": "allowed"}, {"key": "summary.text", "effective": "Invoice due"}]}
PLUGINS = {"plugins": [{"name": "spamfilter", "kind": "triager"}, {"name": "sieve", "kind": "triager"}, {"name": "summary", "kind": "enricher"}, {"name": "mail", "kind": "ingestor"}]}


class Client:
    async def call(self, name, payload=None, **_):
        out = {"mail.get": GET, "label_get": LABELS, "plugin_describe": PLUGINS, "mail.list": {"total": 1, "messages": [ROW]}}[name]
        return CallResult("r", output=out)


async def test_get_message_fills_names_cc_labels_attachments_and_html():
    m = await ui_ops.run(Client(), "get_message", {"message_id": "s1"})
    assert m["from_name"] == "Ann Lee"
    assert m["to_addrs"] == [{"name": "Bob", "address": "bob@example.org"}]
    assert m["cc_addrs"] == [{"name": "", "address": "cc@example.org"}]
    assert (m["starred"], m["location"], m["labels"]) == (True, "Work", ["a", "b"])
    assert m["attachments"] == [{"filename": "i.pdf", "icon": "bi-file-earmark-pdf", "size": "2.0 KB", "content_type": "application/pdf"}]
    assert m["html"] == "<p>Please pay</p>"


async def test_the_list_carries_display_name_snippet_star_and_labels():
    row = (await ui_ops.run(Client(), "list_messages", {}))["items"][0]
    assert (row["from_name"], row["snippet"], row["starred"], row["labels"]) == ("Ann Lee", "Please pay by Friday.", True, ["a", "b"])


async def test_stage_results_are_the_labels_each_stage_plugin_wrote():
    m = await ui_ops.run(Client(), "get_message", {"message_id": "s1"})
    assert m["stages"] == {"triage": [{"key": "spamfilter.verdict", "value": "allowed"}], "enrich": [{"key": "summary.text", "value": "Invoice due"}], "act": []}
    row = (await ui_ops.run(Client(), "list_messages", {}))["items"][0]
    assert [len(row["stages"][k]) for k in ("triage", "enrich", "act")] == [1, 1, 0]


async def test_exposure_is_the_registry_as_mcp_sees_it_and_set_confirms():
    calls = []

    class C:
        async def call(self, name, payload=None, **kw):
            calls.append((name, payload, kw.get("confirm")))
            return CallResult("r", output={"operations": [{"name": "mail.get", "allowed": True, "exposed": False}]} if name == "exposure_list" else {"before": None})

    assert (await ui_ops.run(C(), "get_exposure", {}))["operations"][0]["name"] == "mail.get"
    assert await ui_ops.run(C(), "set_exposure", {"name": "mail.get", "enabled": True}) == {"before": None}
    assert calls[-1] == ("exposure_set", {"name": "mail.get", "kind": "op", "enabled": True}, True)


async def test_create_account_adds_a_provider_instance_and_returns_it():
    calls = []

    class C:
        async def call(self, name, payload=None, **_):
            calls.append((name, payload))
            if name == "plugin_add":
                return CallResult("r", output={"kind": "provider", "name": "me", "started": True, "detail": ""})
            return CallResult("r", output={"plugins": [{"name": "me", "plugin": "gmail", "kind": "provider", "template": False, "enabled": True,
                                                        "healthy": True, "warning": "", "hint": "", "commands": []}]})

    a = await ui_ops.run(C(), "create_account", {"account_id": "me", "provider": "gmail", "config": {}})
    assert calls[0] == ("plugin_add", {"plugin": "gmail", "kind": "provider", "name": "me", "config": {}})
    assert a["account_id"] == "me"


async def test_mark_read_answers_with_id_lists_the_page_can_iterate():
    class C:
        async def call(self, name, payload=None, **_):
            return CallResult("r", output={"mail.get": {**GET, "item_key": ""}}.get(name, {}))

    out = await ui_ops.run(C(), "mark_read", {"ids": ["s1", "s2"]})
    assert out["changed"] == ["s1", "s2"] and out["unchanged"] == []


async def test_create_folder_files_the_selected_messages_under_a_dotted_label():
    calls = []

    class C:
        async def call(self, name, payload=None, **_):
            calls.append((name, payload))
            return CallResult("r", output={})

    out = await ui_ops.run(C(), "create_folder", {"path": "ASF/noise", "ids": ["s1"]})
    assert out["path"] == "ASF.noise"
    assert calls == [("label_set", {"subject_id": "s1", "key": "mail.folder", "value": "ASF.noise"})]


async def test_list_messages_for_a_folder_or_tag_asks_across_views():
    asked = []

    class C:
        async def call(self, name, payload=None, **_):
            if name == "mail.index":
                asked.append(payload)
                return CallResult("r", output={"rows": [], "total": 0})
            return CallResult("r", output={"plugins": []})

    await ui_ops.run(C(), "list_messages", {"location": "ASF.infra"})
    await ui_ops.run(C(), "list_messages", {"label": "noise"})
    assert [a["view"] for a in asked] == ["all", "all"]


async def test_get_plugin_config_gives_wiring_with_choices_and_hides_capabilities():
    class C:
        async def call(self, name, payload=None, **_):
            if name == "plugin_describe":
                return CallResult("r", output={"plugins": [{"name": "ollama", "kind": "llm_backend", "template": False, "settings": [{"name": "instructions", "type": "string", "required": True, "description": "d"}]}]})
            if name == "queue_list":
                return CallResult("r", output={"queues": [{"queue": "analyze"}]})
            if name == "plugin_choices":
                return CallResult("r", output={"choices": {"model": ["m", "n"]}})
            return CallResult("r", output={"config": {"stage": "analyze", "instructions": "sum", "route": {"model": "m"}, "capabilities": ["x"], "odd": 3}})

    out = await ui_ops.run(C(), "get_plugin_config", {"plugin": "enricher.summary"})
    w = {x["name"]: x for x in out["wiring"]}
    assert w["stage"]["choices"] == ["analyze"] and w["route.backend"]["choices"] == ["", "ollama"]
    assert w["route.model"]["choices"] == ["", "m", "n"] and w["route.model"]["value"] == "m" and w["enabled"]["value"] is True and "capabilities" not in w and w["odd"]["type"] == "integer"


def test_nature_tells_model_plugins_from_rule_plugins_and_leaves_the_rest_blank():
    assert ui_ops._nature("llm_backend", "ollama", []) == "ai"
    assert ui_ops._nature("enricher", "llm", []) == "ai"
    assert ui_ops._nature("triager", "x", ["model"]) == "ai"
    assert ui_ops._nature("triager", "sieve", ["mail"]) == "deterministic"
    assert ui_ops._nature("gateway", "x", []) == ""


def test_icon_keeps_only_bootstrap_class_names_and_an_account_says_it_is_a_mail_identity():
    assert ui_ops._icon("bi-google") == "bi-google" and ui_ops._icon('bi-x" onclick="y') == "" and ui_ops._icon(None) == ""
    a = ui_ops._account({"name": "me", "plugin": "gmail", "enabled": True, "healthy": True, "warning": "", "hint": "", "commands": [], "icon": "bi-google", "tags": ["mail"]})
    assert (a["icon"], a["role"]) == ("bi-google", "mail identity")


def test_icon_falls_back_to_the_kind_and_socket_is_not_listed():
    assert ui_ops._icon(None, "store") == "bi-database" and ui_ops._icon("bi-envelope", "store") == "bi-envelope" and ui_ops._icon(None, "triager") == ""
    assert ("provider", "socket") in ui_ops._HIDDEN


def test_stored_enrichment_data_becomes_blocks_with_lists_kept_and_confidence_dropped():
    rows = [{"name": "summary", "data": {"summary": "Dave proposes recording IP clearance.", "facts": ["a", "b"], "confidence": 0.95, "empty": ""}, "labels": {}, "duration_ms": 12400}]
    assert ui_ops._enrichment_blocks(rows) == [{"name": "summary", "fields": [{"name": "summary", "items": ["Dave proposes recording IP clearance."]}, {"name": "facts", "items": ["a", "b"]}],
                                                "duration_ms": 12400}]


class ThreadClient:
    def __init__(self, confidential: bool = False) -> None:
        self.calls, self.confidential = [], confidential

    async def call(self, name, payload=None, **_):
        self.calls.append((name, payload))
        out = {
            "mail.thread": {"items": [{"subject_id": "a", "subject": "T", "sent_at": "d1", "from_addr": "x@y"}, {"subject_id": "b", "subject": "T", "sent_at": "d2", "from_addr": "z@y"}]},
            "mail.get": {"source": "gmail:me"},
            "item_rerun": {},
            "item_enrichments": {"enrichments": [{"name": "summary", "data": {"summary": "said hi"}}] if payload and payload.get("id") == "a" else []},
            "label_get": {"labels": [{"key": "confidential", "effective": "true"}] if self.confidential else []},
            "plugin_describe": {"plugins": [{"name": "summary", "kind": "enricher", "plugin": "llm", "template": False}]},
            "summary.try": {"summary": "They greeted each other.", "facts": ["a", "b"], "confidence": 1.0},
        }[name]
        return CallResult("r", output=out)


async def test_enrich_thread_queues_every_message_and_progress_counts_the_enriched():
    c = ThreadClient()
    assert (await ui_ops.run(c, "enrich_thread", {"message_id": "a"}))["queued"] == ["a", "b"]
    reruns = [x[1] for x in c.calls if x[0] == "item_rerun"]
    assert len(reruns) == 1 and reruns[0]["ids"] == ["a", "b"] and reruns[0]["stage"] == "enrich"  # one batch, not one call per message
    prog = await ui_ops.run(c, "thread_progress", {"message_id": "a"})
    assert (prog["done"], prog["total"]) == (1, 2) and len(prog["enriched"]) == 1


async def test_summarize_thread_asks_the_enricher_once_over_the_stored_summaries_and_refuses_confidential_mail():
    c = ThreadClient()
    out = await ui_ops.run(c, "summarize_thread", {"message_id": "a"})
    asked = [x for x in c.calls if x[0] == "summary.try"]
    assert len(asked) == 1 and "said hi" in asked[0][1]["text"] and out["without_summary"] == 1
    assert out["blocks"][0]["fields"][0]["items"] == ["They greeted each other."]
    import pytest

    with pytest.raises(ui_ops.CliError):
        await ui_ops.run(ThreadClient(confidential=True), "summarize_thread", {"message_id": "a"})


class MarkClient:
    """What core's mail.mark_read answers: the ids it knows, the ones it does not, and each source's keys. `gone` is unknown, s3 has no
    provider key, and me.set_read can be made to fail."""

    def __init__(self, fail_write_back: bool = False) -> None:
        self.calls, self.fail = [], fail_write_back

    async def call(self, name, payload=None, **_):
        self.calls.append((name, payload))
        if name in ("mail.mark_read", "mail.mark_unread"):
            ids = payload["subject_ids"]
            found = [i for i in ids if i != "gone"]
            keyed = {i: f"k-{i}" for i in found if i != "s3"}
            return CallResult("r", output={"marked": len(found), "found": found, "missing": [i for i in ids if i == "gone"], "sources": {"gmail:me": keyed} if keyed else {}})
        if name == "me.set_read" and self.fail:
            raise CliError("provider down")
        return CallResult("r", output={})


async def test_mark_read_dedupes_reports_missing_ids_and_writes_back_only_what_has_a_key():
    c = MarkClient()
    out = await ui_ops.run(c, "mark_read", {"ids": ["s1", "s1", "gone", "s3"]})
    assert out["changed"] == ["s1", "s3"] and out["missing"] == ["gone"] and out["local_only"] == []
    (local,) = [p for n, p in c.calls if n == "mail.mark_read"]
    assert local == {"subject_ids": ["s1", "gone", "s3"]}  # the duplicate once; core says which are not stored
    (remote,) = [p for n, p in c.calls if n == "me.set_read"]
    assert remote == {"item_keys": ["k-s1"], "read": True}  # s3 has no item key: nothing to write back


async def test_mark_unread_goes_the_other_way_locally_and_at_the_source():
    c = MarkClient()
    await ui_ops.run(c, "mark_unread", {"ids": ["s1", "s2"]})
    assert [n for n, _ in c.calls if n in ("mail.mark_read", "mail.mark_unread")] == ["mail.mark_unread"]
    assert [p for n, p in c.calls if n == "me.set_read"] == [{"item_keys": ["k-s1", "k-s2"], "read": False}]


async def test_a_failed_write_back_is_reported_not_raised_so_the_page_still_updates_its_rows():
    out = await ui_ops.run(MarkClient(fail_write_back=True), "mark_read", {"ids": ["s1", "s2", "s3"]})
    assert out["changed"] == ["s1", "s2", "s3"] and out["local_only"] == ["s1", "s2"]  # marked here; the two with a source key were not written back


async def test_a_select_all_sized_mark_is_one_core_call_and_the_write_back_comes_in_chunks():
    c = MarkClient()
    out = await ui_ops.run(c, "mark_read", {"ids": [f"m{i}" for i in range(2460)]})
    assert len(out["changed"]) == 2460
    assert len([1 for n, _ in c.calls if n == "mail.mark_read"]) == 1  # the API is rate limited: never a call per message
    writes = [p for n, p in c.calls if n == "me.set_read"]
    assert len(writes) == 5 and max(len(w["item_keys"]) for w in writes) == 500
    assert len(c.calls) == 6


async def test_mark_read_with_nothing_selected_calls_nothing():
    c = MarkClient()
    assert (await ui_ops.run(c, "mark_read", {"ids": []}))["changed"] == [] and c.calls == []
