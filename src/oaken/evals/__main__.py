# SPDX-License-Identifier: Apache-2.0
"""``uv run python -m oaken.evals enrich [--enricher NAME] [--fixtures DIR]``
``uv run python -m oaken.evals retrieval [--messages N] [--seed S] [--live] [--out DIR]``

``enrich`` scores the enrichers' eval fixtures against the language model configured in
``config.toml``. Manual: it needs a running model (Ollama, say) and is never part of CI.

``retrieval`` builds a synthetic mailbox, runs keyword, vector and hybrid search and ``ask`` over
planted questions, and writes ``latest.json`` and ``latest.md``. Offline (the default) it uses
deterministic stand-ins for the models and is what CI runs; ``--live`` uses the configured
embedder and language model instead. Nothing is stored outside a throwaway database and no real
mail is involved.
"""

import argparse
import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.core.clock import SystemClock
from oaken.core.db import Database, migrate
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.evals import Fixture, Score, build_input, load_fixtures, score
from oaken.ingest.usage import UsageStore
from oaken.plugins.contracts import PluginContext
from oaken.plugins.host import PluginHost

MIGRATIONS = Path(__file__).resolve().parents[1] / "core" / "migrations"


class _Access:
    """``ctx.llm`` for an eval: the real service, the default workspace, no account lookup."""

    def __init__(self, service: LLMService) -> None:
        self.service = service

    async def generate_cached(self, builder: Any, *parts: Any, **kw: Any) -> Any:
        return await self.service.generate_cached(
            builder, *parts, content_hash=kw["content_hash"], account_id=None, message_id=None
        )


def default_fixture_root() -> Path:
    spec = importlib.util.find_spec("oaken_enrichers")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("oaken_enrichers is not installed")
    return Path(next(iter(spec.submodule_search_locations))) / "tests" / "evals"


async def run(root: Path, only: str | None, settings: Settings) -> list[Score]:
    clock = SystemClock()
    host = PluginHost(settings, clock, health_interval=3600)
    await host.start()
    try:
        backend = host.llm_backend()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"no language model is available ({exc})") from exc
    scores: list[Score] = []
    with tempfile.TemporaryDirectory() as tmp:
        async with await Database.open(Path(tmp) / "raw.db", read_pool_size=1) as db:
            await migrate(db, MIGRATIONS)
            service = LLMService(
                LLMCache(db, clock), UsageStore(db, clock), Workspaces(settings),
                lambda _name: backend, settings,
            )  # fmt: skip
            enrichers = host.named_enrichers()
            for fx in load_fixtures(root, only):
                enricher = enrichers.get(fx.enricher)
                if enricher is None:
                    scores.append(Score(fx, error=f"enricher {fx.enricher} is not installed"))
                    continue
                scores.append(await _one(fx, enricher, settings, clock, _Access(service)))
    await host.stop()
    return scores


async def _one(fx: Fixture, enricher: Any, settings: Settings, clock: Any, access: Any) -> Score:
    await enricher.start(
        PluginContext(
            name=fx.enricher,
            kind="enricher",
            settings=settings,
            clock=clock,
            log=get_logger("oaken.evals"),
            llm=access,
        )  # fmt: skip
    )
    msg = build_input(fx)
    try:
        result = (await enricher.enrich(msg)).data
    except Exception as exc:  # noqa: BLE001 - one bad answer must not stop the run
        return Score(fx, error=f"{type(exc).__name__}: {exc}")
    return score(fx, result, msg.body.unwrap())


async def run_retrieval(args: argparse.Namespace) -> int:
    from oaken.evals.retrieval import report
    from oaken.evals.retrieval.corpus import generate
    from oaken.evals.retrieval.embedder import ConceptEmbedder
    from oaken.evals.retrieval.reader import read
    from oaken.evals.retrieval.run import run as run_eval
    from oaken.testing.fakes import FakeLLM

    corpus = generate(args.messages, seed=args.seed, per_kind=args.per_kind)
    if args.live:
        settings = Settings.load()
        host = PluginHost(settings, SystemClock(), health_interval=3600)
        await host.start()
        try:
            embedder, llm = host.embedder(), host.llm_backend()
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"no embedder or language model is available ({exc})") from exc
        results = await run_eval(
            corpus, embedder, llm, mode="live", reader=llm.model_id, settings=settings,
            date=args.date,
        )  # fmt: skip
        await host.stop()
    else:
        results = await run_eval(corpus, ConceptEmbedder(), FakeLLM(responder=read), date=args.date)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "latest.json").write_text(report.dumps(results))
    (args.out / "latest.md").write_text(report.markdown(results))
    print(report.markdown(results))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oaken.evals")
    sub = ap.add_subparsers(dest="command", required=True)
    rt = sub.add_parser("retrieval", help="measure hybrid search and ask on a synthetic mailbox")
    rt.add_argument("--messages", type=int, default=1000)
    rt.add_argument("--seed", type=int, default=0)
    rt.add_argument("--per-kind", type=int, default=12, help="questions of each kind")
    rt.add_argument("--live", action="store_true", help="use the configured embedder and model")
    rt.add_argument("--out", type=Path, default=Path("docs/evals"))
    rt.add_argument("--date", help="date to print in the report (default: today)")
    en = sub.add_parser("enrich", help="score the enricher fixtures against the configured model")
    en.add_argument("--enricher", help="only this enricher (summary, triage, actions, inspector)")
    en.add_argument("--fixtures", type=Path, help="fixture folder (default: the installed package)")
    args = ap.parse_args(argv)
    if args.command == "retrieval":
        return asyncio.run(run_retrieval(args))
    root = args.fixtures or default_fixture_root()
    scores = asyncio.run(run(root, args.enricher, Settings.load()))
    for s in scores:
        mark = "ok  " if s.passed else "FAIL"
        print(f"{mark} {s.fixture.enricher}/{s.fixture.name}")
        for line in [*s.failures, *([s.error] if s.error else [])]:
            print(f"       {line}")
    passed = sum(s.passed for s in scores)
    print(f"\n{passed}/{len(scores)} fixtures passed")
    return 0 if passed == len(scores) else 1


if __name__ == "__main__":
    sys.exit(main())
