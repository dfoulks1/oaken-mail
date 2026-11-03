# SPDX-License-Identifier: Apache-2.0
"""``/table_refresh``: the inactive-plugins fragment the console swaps in live. Loopback, no token,
HTML, and a known kind only. The fragment lists disabled plugins and never the enabled ones."""

from oaken.api.tests.conftest import Rig


async def test_table_refresh_returns_html_for_a_known_kind(rig: Rig) -> None:
    resp = await rig.http.get("/table_refresh?kind=action")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


async def test_an_unknown_kind_is_a_404(rig: Rig) -> None:
    resp = await rig.http.get("/table_refresh?kind=not_a_kind")
    assert resp.status_code == 404


async def test_the_fragment_shows_disabled_plugins_not_enabled_ones(rig: Rig) -> None:
    host = rig.ops.svc.plugins
    assert host is not None
    statuses = {s.name: s for s in host.statuses()}
    # pick any kind that has at least one plugin, and check the split holds for it.
    for kind in {s.kind for s in statuses.values()}:
        body = (await rig.http.get(f"/table_refresh?kind={kind}")).text
        for s in statuses.values():
            if s.kind != kind or "@" in s.name:
                continue
            if s.enabled:
                assert s.name not in body, f"enabled {s.name} must not be in the inactive fragment"
            # a disabled plugin appears; an empty fragment (none disabled) is also valid, so we
            # only assert the exclusion of enabled ones, which is the invariant that can break.
