# SPDX-License-Identifier: Apache-2.0
"""Flags, labels, trash, archive, star and send: the right requests, idempotent, per-id failures."""

import base64
from typing import Any

import pytest

from oaken.core.errors import PluginError, ProviderError
from oaken_gmail import PartialFailure
from oaken_gmail.tests.conftest import FakeGoogle, Rig, make_rig, raw_mail


async def rig_with(n: int = 3, **config: Any) -> Rig:
    google = FakeGoogle()
    google.fill(n)
    rig = make_rig(google, **config)
    await rig.provider.start(rig.ctx)
    return rig


async def test_marking_read_removes_the_unread_label_in_one_batch_modify() -> None:
    rig = await rig_with()
    await rig.provider.set_flags(["m00001", "m00002"], {"\\Seen"}, set())
    (body,) = rig.google.bodies("/messages/batchModify")
    assert body == {"ids": ["m00001", "m00002"], "addLabelIds": [], "removeLabelIds": ["UNREAD"]}
    await rig.provider.stop()


async def test_unread_star_and_unstar_map_to_labels() -> None:
    rig = await rig_with()
    await rig.provider.set_flags(["m00001"], {"\\Flagged"}, {"\\Seen"})
    await rig.provider.star(["m00001"], False)
    first, second = rig.google.bodies("/messages/batchModify")
    assert first["addLabelIds"] == ["STARRED", "UNREAD"] and first["removeLabelIds"] == []
    assert second["removeLabelIds"] == ["STARRED"]
    await rig.provider.stop()


async def test_setting_flags_twice_is_the_same_as_once() -> None:
    rig = await rig_with()
    for _ in range(2):
        await rig.provider.set_flags(["m00001"], {"\\Seen"}, set())
    assert "UNREAD" not in rig.google.messages["m00001"]["labelIds"]
    await rig.provider.stop()


async def test_nothing_to_do_makes_no_request() -> None:
    rig = await rig_with()
    before = len(rig.google.calls)
    await rig.provider.set_flags([], {"\\Seen"}, set())
    await rig.provider.set_flags(["m00001"], set(), set())
    await rig.provider.set_flags(["m00001"], {"\\Answered"}, set())  # no Gmail meaning
    assert len(rig.google.calls) == before
    await rig.provider.stop()


async def test_a_label_name_is_resolved_to_its_id_and_system_ids_pass_through() -> None:
    rig = await rig_with()
    rig.google.labels["Label_9"] = {"id": "Label_9", "name": "Receipts", "type": "user"}
    await rig.provider.set_flags(["m00001"], {"receipts"}, {"INBOX"})
    (body,) = rig.google.bodies("/messages/batchModify")
    assert body["addLabelIds"] == ["Label_9"] and body["removeLabelIds"] == ["INBOX"]
    await rig.provider.stop()


async def test_a_bad_id_is_reported_and_the_others_still_change() -> None:
    rig = await rig_with()
    with pytest.raises(PartialFailure) as e:
        await rig.provider.set_flags(["m00001", "nope", "m00002"], {"\\Seen"}, set())
    assert set(e.value.failed) == {"nope"}
    for pid in ("m00001", "m00002"):
        assert "UNREAD" not in rig.google.messages[pid]["labelIds"]
    await rig.provider.stop()


async def test_more_than_a_thousand_ids_are_split_into_several_calls() -> None:
    rig = await rig_with(1001)
    await rig.provider.set_flags(sorted(rig.google.messages), {"\\Seen"}, set())
    sizes = [len(b["ids"]) for b in rig.google.bodies("/messages/batchModify")]
    assert sizes == [1000, 1]
    await rig.provider.stop()


async def test_archive_removes_inbox() -> None:
    rig = await rig_with()
    await rig.provider.archive(["m00001"])
    assert rig.google.messages["m00001"]["labelIds"] == []
    await rig.provider.stop()


async def test_trash_and_untrash_use_the_dedicated_calls() -> None:
    rig = await rig_with()
    await rig.provider.trash(["m00001", "m00002"])
    assert rig.google.paths("POST").count("/messages/m00001/trash") == 1
    assert "TRASH" in rig.google.messages["m00002"]["labelIds"]
    await rig.provider.untrash(["m00001"])
    assert "TRASH" not in rig.google.messages["m00001"]["labelIds"]
    with pytest.raises(PartialFailure) as e:
        await rig.provider.trash(["m00003", "gone"])
    assert set(e.value.failed) == {"gone"} and "TRASH" in rig.google.messages["m00003"]["labelIds"]
    await rig.provider.stop()


async def test_labels_can_be_created_renamed_and_deleted_but_not_system_ones() -> None:
    rig = await rig_with()
    lid = await rig.provider.create_label("Receipts")
    assert rig.google.labels[lid]["name"] == "Receipts"
    await rig.provider.rename_label(lid, "Bills")
    assert rig.google.labels[lid]["name"] == "Bills"
    await rig.provider.set_flags(["m00001"], {"bills"}, set())  # the name cache was refreshed
    assert lid in rig.google.messages["m00001"]["labelIds"]
    await rig.provider.delete_label(lid)
    assert lid not in rig.google.labels
    for call in (rig.provider.delete_label("INBOX"), rig.provider.rename_label("INBOX", "x")):
        with pytest.raises(ProviderError, match="system label"):
            await call
    assert "INBOX" in rig.google.labels
    await rig.provider.stop()


async def test_send_posts_the_raw_message_and_returns_its_id() -> None:
    rig = await rig_with()
    mail = raw_mail(99)
    assert await rig.provider.send(mail) == "sent1"
    (body,) = rig.google.bodies("/messages/send")
    assert base64.urlsafe_b64decode(body["raw"]) == mail and rig.google.sent == [mail]
    await rig.provider.stop()


async def test_send_is_not_repeated_after_a_server_error_but_is_after_a_rate_limit() -> None:
    rig = await rig_with()
    rig.google.api_script = [500]
    with pytest.raises(ProviderError):
        await rig.provider.send(raw_mail(1))
    assert rig.google.sent == [] and rig.google.paths("POST").count("/messages/send") == 1
    rig.google.api_script = [429]
    assert await rig.provider.send(raw_mail(1)) == "sent1"
    await rig.provider.stop()


async def test_send_as_aliases_can_be_listed() -> None:
    rig = await rig_with()
    aliases = await rig.provider.list_send_as()
    assert aliases == [
        {"email": "me@example.com", "default": True, "verified": True},
        {"email": "alias@example.org", "default": False, "verified": False},
    ]
    await rig.provider.stop()


async def test_the_provider_refuses_to_work_before_it_is_started() -> None:
    from oaken_gmail import GmailProvider

    with pytest.raises(PluginError, match="start"):
        await GmailProvider().set_flags(["x"], {"\\Seen"}, set())
