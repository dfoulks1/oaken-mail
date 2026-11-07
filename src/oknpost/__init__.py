# SPDX-License-Identifier: Apache-2.0
"""Oaken Mail: Oaken Core with a coat of paint. The work is core's and its plugins'; this adds a name, a starting config and one rule.

The rule: only mail goes through. A provider loads only if it is core's own or carries a tag in ``ALLOWED_TAGS``. Every other kind of
plugin (formatters, triagers, actions) is open. The rule is applied where plugins come from (entry points, ``class =`` tables,
extension directories, the oaken-plugins catalog, a class path given to ``plugin add``), in the daemon, which is the gate. The
ingestor (``ingestor.mail``) still refuses anything that is not really mail: that is what holds when a door lets the wrong thing in.
"""

import importlib.util
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from oaken_core import identity, paths
from oaken_core.cli.commands import main as core_main
from oaken_core.daemon.core import Core
from oaken_core.errors import PluginError
from oaken_core.plugins import catalog
from oaken_core.plugins.host import discover_entry_points
from oaken_core.plugins.kinds import KindRegistry
from oaken_core.settings import Settings

ALLOWED_TAGS = frozenset({"mail"})  # what a provider must carry (any one of these) to load: the registry of what Oaken Mail takes in
LIMITED = "provider"  # the kind the rule applies to


def permitted(kind: str, value: str) -> bool:
    """May the plugin at ``module:Class`` (or ``module``) load? Core's own, always; a provider needs an allowed tag; other kinds are open.
    The tags are read from source, so a plugin that is refused is never imported."""
    if kind != LIMITED or value.startswith("oaken_core."):
        return True
    try:
        spec = importlib.util.find_spec(value.partition(":")[0])
    except (ImportError, ValueError):  # a dotted name whose parent package is missing raises instead of returning None
        return False
    if spec is None or not spec.origin:
        return False
    where = Path(spec.origin).parent if spec.submodule_search_locations else Path(spec.origin)
    return bool(ALLOWED_TAGS & set(catalog.declared(where).get("tags", "").split(",")))


def _discover(group: str) -> Iterable[Any]:
    kind = next((k.name for k in KindRegistry.shipped() if k.group == group), "")
    return [ep for ep in discover_entry_points(group) if permitted(kind, ep.value)]


def _clean(s: Settings) -> Settings:
    s.extensions.dirs = []  # shortcut: a directory of plugins is a door no tag check reaches; drop them all, allow some when a product needs one
    tables = s.plugins.tables.get(LIMITED, {})
    for name in [n for n, t in tables.items() if t.get("class") and not permitted(LIMITED, str(t["class"]))]:
        del tables[name]
    return s


class MailCore(Core):
    async def open(self) -> None:
        await super().open()
        add = self.host.plan_add

        def guarded(target: str, **kw: Any) -> Any:  # `plugin add` by a class path or a catalog name resolves outside discovery
            plan = add(target, **kw)
            cls = plan.table.get("class") or (plan.base[1].get("class") if plan.base else None)
            if plan.kind == LIMITED and cls and not permitted(LIMITED, str(cls)):
                raise PluginError(f"{plan.kind} {plan.name!r} is not mail: Oaken Mail takes providers tagged {', '.join(sorted(ALLOWED_TAGS))}")
            return plan

        self.host.plan_add = guarded  # type: ignore[method-assign]


def make(settings: Settings) -> Core:
    return MailCore(_clean(settings), discover=_discover)


def _first_run() -> None:
    config = paths.config_dir() / "config.toml"
    if not config.exists():
        config.write_bytes(Path(__file__).with_name("default.toml").read_bytes())


def main(argv: list[str] | None = None) -> int:
    identity.configure(app="oaken-mail", title="Oaken Mail", prog="oknpost")
    real = catalog.entries
    catalog.entries = lambda kinds: [e for e in real(kinds) if e.kind != LIMITED or ALLOWED_TAGS & set(e.tags)]  # the listing and add-by-name see only mail providers
    _first_run()
    return core_main(argv, prog="oknpost", make=make)
