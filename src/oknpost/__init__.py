# SPDX-License-Identifier: Apache-2.0
"""Oaken Mail: Oaken Core with a coat of paint. The work is core's and its plugins'; this adds a name, a starting config and one rule.

The rule: only mail goes through. A provider loads only if it is core's own or carries a tag in ``ALLOWED_TAGS``. Every other kind of
plugin (formatters, triagers, actions) is open. The rule is applied where plugins come from (entry points, ``class =`` tables,
extension directories, the oaken-plugins catalog, a class path given to ``plugin add``), in the daemon, which is the gate. The
ingestor (``ingestor.mail``) still refuses anything that is not really mail: that is what holds when a door lets the wrong thing in.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

from oaken_core import identity, paths
from oaken_core.cli.commands import main as core_main
from oaken_core.cli.commands import take_home
from oaken_core.plugins import catalog
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


# Filtering is part of Oaken Mail: both filter plugins load in every stack, and are written to no config file.
FILTERING = {"sieve": {"class": "oaken_plugins.triager_sieve:SieveTriager"}, "filters": {"class": "oaken_plugins.triager_filters:FiltersTriager"}}


def _clean(s: Settings) -> Settings:
    s.extensions.dirs = []  # shortcut: a directory of plugins is a door no tag check reaches; drop them all, allow some when a product needs one
    for name, table in FILTERING.items():
        s.plugins.tables.setdefault("triager", {}).setdefault(name, dict(table))  # the owner's own [plugins.triager.<name>] (even enabled = false) wins
    tables = s.plugins.tables.get(LIMITED, {})
    for name in [n for n, t in tables.items() if t.get("class") and not permitted(LIMITED, str(t["class"]))]:
        del tables[name]
    return s


def _first_run() -> None:
    config = paths.config_dir() / "config.toml"
    if not config.exists():
        config.write_bytes(Path(__file__).with_name("default.toml").read_bytes())


def _make(settings: Settings):  # type: ignore[no-untyped-def]
    from oknpost.daemon import (
        make,  # the daemon's imports (the MCP library alone is a third of a second) are not paid by a client command
    )

    return make(settings)


def main(argv: list[str] | None = None) -> int:
    identity.configure(app="oaken-mail", title="Oaken Mail", prog="oknpost", daemon=[sys.executable, "-m", "oknpost"])
    real = catalog.entries
    catalog.entries = lambda kinds: [e for e in real(kinds) if e.kind != LIMITED or ALLOWED_TAGS & set(e.tags)]  # the listing and add-by-name see only mail providers
    _first_run()
    args = take_home(list(sys.argv[1:] if argv is None else argv))
    if args and args[0] in ("start", "stop", "restart") and "--fg" not in args:  # --fg keeps core alone in the terminal
        return _with_ui(args)
    return core_main(args, prog="oknpost", make=_make)


def _ui(command: str) -> int:
    """`oknpost-webui <command>`, as its own process: it is a separate server and keeps its own pid file."""
    return subprocess.run([sys.executable, "-m", "oaken_webui.main", command], check=False).returncode


def _with_ui(args: list[str]) -> int:
    """start, stop and restart cover the UI too, in order: stop the UI before core, start it after."""
    if args[0] != "start":
        _ui("stop")  # not running is fine here; core's stop is what matters
    code = core_main(args, prog="oknpost", make=_make)
    return _ui("start") if args[0] != "stop" and code == 0 else code
