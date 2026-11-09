# SPDX-License-Identifier: Apache-2.0
from oaken_core.cli.client import CallResult
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
    rows = [{"name": "summary", "data": {"summary": "Dave proposes recording IP clearance.", "facts": ["a", "b"], "confidence": 0.95, "empty": ""}, "labels": {}}]
    assert ui_ops._enrichment_blocks(rows) == [{"name": "summary", "fields": [{"name": "summary", "items": ["Dave proposes recording IP clearance."]}, {"name": "facts", "items": ["a", "b"]}]}]
