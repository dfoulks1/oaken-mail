+++
title = "Writing a plugin"
subtitle = "Providers, embedders, models, enrichers and vector stores"
lead = "Everything Oaken Mail can be extended with is a plugin: a small class, registered with a Python entry point. Start from a base class, override what your plugin does, and run the contract suite."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 30
description = "How to write a provider, embedder, LLM backend, enricher or vector store plugin, register it, test it, and what the host guarantees."
+++

## The five kinds

| Kind | Entry-point group | What it does |
|---|---|---|
| Provider | `oaken.providers` | Brings mail in from a service and writes flags back. |
| Embedder | `oaken.embedders` | Turns text into vectors. |
| LLM backend | `oaken.llm_backends` | Runs a language model. |
| Enricher | `oaken.enrichers` | Derives something from one message (a summary, a category, actions). |
| Vector store | `oaken.vector_stores` | Stores vectors and finds the nearest ones. |

Providers are the most wanted: each one lets Oaken Mail work with another mail service. Enrichers
are the ones people will most often write for themselves. Both are meant to be short. The exact
signatures are on the [Plugin contracts](reference/plugin-contracts.html) page, which is generated
from the code.

## A provider in one screen

Subclass `BaseProvider` and override `pull`. That is a working, read-only provider.

```python
from oaken.plugins.base import BaseProvider
from oaken.plugins.contracts import PullBatch, RawMessage


class MyProvider(BaseProvider):
    version = "0.1"

    async def pull(self, cursor):
        # cursor is whatever you returned last time (None the first time). Keep it opaque.
        for page in my_service.list_changes(since=cursor):
            yield PullBatch(
                messages=[RawMessage(m.id, m.rfc822_bytes) for m in page.new],
                deleted_provider_ids=page.deleted_ids,
                next_cursor=page.token,
            )
```

Add methods to add abilities. The host works out your capabilities from what you wrote, so there
is no second list to keep in step:

| You write | You gain |
|---|---|
| `pull(cursor)` | `sync.pull` (required for a provider) |
| `authenticate(ctx)` | `auth` |
| `set_flags(provider_ids, add, remove)` | `flags.write`, so a read in Oaken becomes a read on the server |
| `send(raw_bytes)` | `send`, declared for the future. Nothing calls it in v1, and sending is never exposed over MCP |

`ctx` (also on `self.ctx` once started) carries your settings, a logger, the clock, and `config`,
the table `[plugins.<name>]` from the user's `config.toml`. Credentials belong in the OS keyring,
never in config.

You never touch the databases. You return data and core stores it.

## Be accepting in what you return

You may return the contract's dataclasses, but you do not have to. A dict, a list, a tuple or a
plain string is coerced into the right type, and mistakes come back as a readable message:

```text
gmail: pull() returned something invalid (messages.0.raw_bytes: Input should be a valid bytes)
embedder 'mine': embed() returned 3 vectors for 4 texts
```

An error fails that one job and is logged. It never stops core. What core passes on to the rest of
the system is always the strict, frozen type.

## An embedder

```python
from oaken.plugins.base import BaseEmbedder


class MyEmbedder(BaseEmbedder):
    model_id = "my-model"
    dimensions = 384

    async def embed(self, texts):
        return [my_model.encode(t) for t in texts]
```

An LLM backend is the same shape: set `model_id` and `context_window`, override
`generate(request)`. The request carries a system instruction and user parts. Mail content is
always an `UntrustedText`, never part of the system text.

## An enricher in one function

```python
from pydantic import BaseModel
from oaken.plugins.base import enricher


class Result(BaseModel):
    words: int


@enricher("word-count", output=Result)
async def word_count(msg):
    return {"words": len(msg.body.unwrap().split())}
```

The decorator builds the class. Core validates the result against `Result` and publishes its JSON
schema. Change the `version` whenever the output could change: it is part of the cache key, so a
new version re-runs the enricher and an unchanged one never does. An enricher that needs a model
sets `needs_llm=True`.

## Registering it

A plugin is found through an entry point in its package's `pyproject.toml`. First-party plugins
use exactly the same mechanism. The entry-point name is the plugin's name.

```toml
[project.entry-points."oaken.providers"]
myservice = "my_package.provider:MyProvider"

[project.entry-points."oaken.enrichers"]
word-count = "my_package.enrichers:word_count"
```

Install the package into the same environment as Oaken Mail and it is found. Config chooses
which plugin fills a slot (`[sync] provider = "myservice"`, `[ai] embedder = "ollama"`);
enrichers are all used.

## What the host does

- **Load.** It imports the plugin and checks it: the `api_version`, that every capability you
  declare has a real method behind it, and that the members of its kind are present. It reports
  every problem it finds at once.
- **Start.** `start(ctx)` is called, with a timeout.
- **Watch.** `health()` is polled on an interval. A plugin that fails to load, fails to start or
  fails a health check is marked unhealthy, logged, announced as a `PluginHealthChanged` event, and
  left out of lookups. A plugin whose `start()` failed is retried at each health check, so a service
  that was down comes back by itself.
- **Stop.** `stop()` is called once at shutdown.

Core never stops because a plugin did. `start`, `stop` and `health` are all optional to define.

## Testing it

Run the contract suite against your plugin. It is a set of pytest tests that check the behavior
the host and the rest of Oaken rely on.

```python
import pytest
from oaken.testing.contracts import EmbedderContract


class TestMyEmbedder(EmbedderContract):
    @pytest.fixture
    def embedder(self):
        return MyEmbedder()
```

There is a suite for each kind: `ProviderContract`, `EmbedderContract`, `LLMContract`,
`EnricherContract` and `VectorStoreContract`. A provider test also supplies `mailbox_ids`, the ids
of the mail in its test mailbox. If you prefer one call, `await verify_embedder(MyEmbedder)` runs
every rule and raises a single error listing everything that failed.

`oaken.testing.fakes` has an in-memory fake of every kind (`FakeProvider` with a scriptable
mailbox and failure injection, `FakeEmbedder`, `FakeLLM` that records every request,
`FakeEnricher`, `InMemoryVectorStore`). Use them to test code that uses a plugin, with no network,
no model and no real mail.

## Plugin events

Core announces what happens on an event bus, and a plugin can announce its own. Subclass
`PluginEvent`, add plain-data fields, and publish it from `start()` or anywhere you hold the context:

```python
from dataclasses import dataclass
from oaken.plugins import PluginEvent


@dataclass(frozen=True, kw_only=True)
class QuotaLow(PluginEvent):
    remaining: int


await self.ctx.publish(QuotaLow(ts=self.ctx.clock.now(), remaining=12))
```

The host fills in `plugin` with your plugin's name, so a listener can tell who sent it, and
`publish` accepts only `PluginEvent` subclasses, so a plugin can't forge a core event. Clients that
stream events (the web UI, an MCP client) receive yours alongside core's.

## Not sandboxed

Plugins run in the same process as core, with full access to the machine and to whatever core can
reach. They are not sandboxed in version 1. The contract keeps the code tidy and the data
boundaries clear; it does not contain a hostile plugin. Only install plugins you trust, as you
would any Python package.
