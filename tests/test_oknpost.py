# SPDX-License-Identifier: Apache-2.0
"""Only mail goes through: the rule, applied to settings and to `plugin add`."""

from pathlib import Path

import pytest

from oaken_core.errors import PluginError
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from oknpost import _clean, make, permitted

GMAIL = "oaken_plugins.provider_gmail:GmailProvider"
EXEC = "oaken_plugins.provider_exec:ExecProvider"


def test_a_provider_from_outside_core_needs_a_mail_tag_and_every_other_kind_is_open() -> None:
    assert permitted("provider", GMAIL)
    assert not permitted("provider", EXEC)
    assert not permitted("provider", "nowhere.at_all:Thing")  # its tags cannot be read, so it is not trusted
    assert permitted("provider", "oaken_core.providers.socket:SocketProvider")  # core's own always load
    assert permitted("triager", "oaken_plugins.triager_cve:CveTriager")  # only providers are limited


def test_settings_lose_extension_dirs_and_the_provider_tables_the_rule_refuses() -> None:
    tables = {"provider": {"gmail": {"class": GMAIL}, "exec": {"class": EXEC}, "socket": {}}}
    s = _clean(Settings(extensions={"dirs": ["/somewhere"]}, plugins=PluginTables(tables=tables)))
    assert s.extensions.dirs == [] and set(s.plugins.tables["provider"]) == {"gmail", "socket"}


async def test_plugin_add_by_class_path_refuses_a_provider_that_is_not_mail(tmp_path: Path) -> None:
    core = make(Settings(console={"port": 0}, plugins=PluginTables(tables={"store": {"sqlite": {"path": str(tmp_path / "core.db")}}})))
    await core.open()
    try:
        with pytest.raises(PluginError, match="not mail"):
            core.host.plan_add(EXEC)
        assert core.host.plan_add(GMAIL).kind == "provider"
    finally:
        await core.host.stop()
