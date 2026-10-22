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
| `pull(cursor, folder=None)` | `sync.pull` (a provider needs this or `send`). `folder` is passed only to a provider that sets `supports_folders = True` |
| `authenticate(ctx)` | `auth` |
| `set_flags(provider_ids, add, remove)` | `flags.write`, so a read in Oaken becomes a read on the server |
| `send(raw_bytes)` | `send`, declared for the future. Nothing calls it in v1, and sending is never exposed over MCP |

`ctx` (also on `self.ctx` once started) carries your settings, a logger, the clock, and `config`,
the table `[plugins.<name>]` from the user's `config.toml`. Credentials belong in the OS keyring,
never in config.

Raise `oaken.core.errors.AuthError` (or a subclass) when credentials are missing or refused. Core
pauses that account's schedule instead of retrying, and the other accounts carry on. Core starts one
instance of your class per account, so `ctx.account_id` tells you which one you are and is the natural
name for your keyring entry.

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

### What an enricher is given

An `EnrichInput` has the message's cleaned text (`body`), `subject`, a few display `headers`, every
header in `raw_headers` (lower-case names, all values), the attachments' metadata (`attachments`:
name, type, size, whether core flagged it; never the bytes), the `date`, whether it `is_reply`, and,
for a reply, the stored `parent_summary` of the message before it. Everything derived from the mail
is `UntrustedText`.

### Reaching a model

An enricher never gets a backend. It is handed `ctx.llm` in `start()` and asks through it:

```python
class Answer(SafeOutput):
    title: str


TITLE = PromptBuilder("my.title", "1", "Give the email a three-word title.", Answer)

answer = await self.ctx.llm.generate_cached(
    TITLE,
    msg.subject,
    msg.body,
    content_hash=msg.message_id,
    account_id=msg.account_id,
    message_id=msg.message_id,
)
```

Core picks the workspace from the account (a `secure` one only reaches a local model), answers a
repeated question from its cache, validates the reply against `Answer` and records the tokens.
The instructions are a literal in your source; the mail goes in as `UntrustedText` parts, and a
lint test fails a module that builds a request any other way. The first-party enrichers
(`oaken_enrichers`: `summary`, `triage`, `actions`, `inspector`) are the worked examples. Turn one
off with `[plugins.<name>] enabled = false`; `summary` and `actions` skip bulk mail unless you set
`skip_bulk = false`.

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
- **Offer operations.** If the plugin has an `operations()` method, each `PluginOperation` it
  returns becomes `<plugin>.<name>` in the [operation registry](architecture/operations.md), under
  the same confirm gate. They start off over MCP and cannot mark mail read.
- **Stop.** `stop()` is called once at shutdown.

Core never stops because a plugin did. `start`, `stop` and `health` are all optional to define.

## Extending core: operations, job kinds, pipeline stages and scheduled tasks

Beyond the five kinds, a plugin of **any** kind can add capabilities with four optional methods.
Each is collected from the healthy plugins at start-up, validated like the rest of the contract,
and given a plugin-scoped context with **no database handle** — you return data, core stores it.
See [ADR 0012](repo:docs/adr/0012-plugin-extension-hooks.md).

### Add an operation

Define `operations()` returning `PluginOperation` values. Each becomes `<plugin>.<name>` in the
[operation registry](architecture/operations.md), under the same confirm gate and audit as core's
own; a plugin's operations start **off** over MCP and can never mark mail read.

### Add a job kind

Define `job_handlers()` to run your own durable background work. Core registers each as
`plugin.<name>.<kind>` on the job runner — the same queue, retries, leases and per-kind
concurrency as core's own kinds.

```python
from pydantic import BaseModel
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import PluginJobHandler


class Recount(BaseModel):
    text: str


class Count(BaseModel):
    words: int


async def recount(ctx, payload: Recount) -> Count:  # ctx: PluginJobContext (config, clock, log)
    return Count(words=len(payload.text.split()))


class MyPlugin(BaseEmbedder):
    model_id, dimensions = "mine", 384

    async def embed(self, texts): ...

    def job_handlers(self):
        return [PluginJobHandler("recount", Recount, recount, result_model=Count)]
```

The handler gets its payload (validated against `payload_model`) and a `PluginJobContext`; a
returned result is validated against `result_model` and stored on the job.

### Add a pipeline stage

Define `pipeline_stages()` to derive something **after the enrichers** for each message. Core stores
the result under `plugin.<name>.<stage>` with the same content-hash idempotency as an enricher, so a
rerun or a `reprocess` is a cache hit.

```python
from oaken.plugins.contracts import PluginStage, PluginStageContext, PluginStageInput


async def tag(ctx: PluginStageContext, msg: PluginStageInput) -> dict:
    # msg.enrichments holds this message's enricher results (summary, triage, ...)
    priority = msg.enrichments.get("triage", {}).get("priority", 3)
    return {"tag": "urgent" if priority == 0 else "normal"}


class MyPlugin(BaseEnricher):
    version = "1"

    async def enrich(self, msg): ...

    def pipeline_stages(self):
        return [PluginStage("tag", tag, version="1")]
```

Bump a stage's `version` when its output could change; the old result is recomputed, an unchanged
version is a cache hit. A stage failure is logged and never fails the message's enrichment.

### Add a scheduled task

Define `scheduled_tasks()` to run work on a schedule. Core seeds each into the scheduler as
`plugin.<name>.<task>` **only if absent**, so a schedule you later edit or remove is left alone
(the same rule as the sync schedules). The task fires a `job_kind` — usually your own from
`job_handlers()` — with a fixed payload.

```python
from oaken.plugins.contracts import PluginScheduledTask


class MyPlugin(BaseEmbedder):
    model_id, dimensions = "mine", 384

    async def embed(self, texts): ...

    def job_handlers(self):
        return [PluginJobHandler("recount", Recount, recount, result_model=Count)]

    def scheduled_tasks(self):
        return [
            PluginScheduledTask("nightly", "plugin.mine.recount", Recount(text=""), every=86400.0)
        ]  # once a day; or cron="0 3 * * *"
```

### Add a provider, embedder, model backend, enricher or vector store

These are the five kinds above — subclass the base class, set a few attributes, override the method
your kind needs, and register an entry point. The earlier sections are the worked examples.

Every hook has a conformance suite (next section): `JobHandlerContract`, `PipelineStageContract`
and `ScheduledTaskContract`, plus the five kind suites.

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

For the extension hooks there are `JobHandlerContract`, `PipelineStageContract` and
`ScheduledTaskContract` (and `verify_job_handlers`, `verify_pipeline_stages`,
`verify_scheduled_tasks`): give one your plugin instance and it checks the declarations the way the
host does at load.

`oaken.testing.fakes` has an in-memory fake of every kind (`FakeProvider` with a scriptable
mailbox and failure injection, `FakeEmbedder`, `FakeLLM` that records every request,
`FakeEnricher`, `InMemoryVectorStore`). Use them to test code that uses a plugin, with no network,
no model and no real mail.

## A first-party example: Ollama

`oaken_ollama` (an embedder and an LLM backend) is the reference for anything that talks to a
model server. It reads its own `[plugins.<entry-point-name>]` table through `ctx.config` (here
`base_url`, `model`, `keep_alive`, `timeout`, `max_retries`, `backoff`, `batch_size`), sets
`model_id`, `dimensions` and `context_window` in `start()`, and an LLM backend sets `locality` to
`"local"` or `"remote"` so a secure workspace can refuse it. Register one class under two entry
points to offer two differently configured instances. See [prompt safety and
workspaces](architecture/ai-safety.html).

## A first-party example: Gmail

`oaken_gmail` is the reference for a provider that has its own auth. It keeps its credentials in the
OS keyring (never in `ctx.config`), renews them through a `TokenManager`, reports a dead
credential as an unhealthy `health()` plus a plugin event instead of crashing, and overrides
`authenticate`, `pull`, `set_flags` and `send`, so it declares all four capabilities. Its tests run
the shared `ProviderContract` against an in-memory fake Google. See [Connect
Gmail](providers/gmail.html).

A provider does not have to read mail. `oaken_smtp` declares only `auth` and `send`: it is a provider
that sends through your own SMTP server, with the password in the keyring. A provider must implement
`pull` or `send`, and the contract suite checks only the capabilities a plugin declares. See [Send
through your own SMTP server](providers/smtp.html).

`oaken_imap` is the generic reader: any IMAP server, with no knowledge of one service. It declares
`auth`, `sync.pull` and `flags.write`, keeps what it has delivered per folder in its cursor so an
interrupted sync resumes and a renumbered folder is read again, and refuses any connection that is
not encrypted. See [Connect any IMAP server](providers/imap.html).

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

### Listening

A plugin can also listen. `ctx.subscribe(event_type, handler)` calls `handler(event)` for every core
or plugin event of that type (`Event` for all of them) and returns a function that ends the
subscription:

```python
async def start(self, ctx):
    async def on_indexed(event):
        self.pending.append(event.message_id)  # queue real work; keep the handler quick

    ctx.subscribe(MessageIndexed, on_indexed)
```

Events are frozen, so a handler can read but not change what happened, and one that raises is logged
and skipped without affecting core or other listeners. The host ends every subscription when the
plugin stops.

## Not sandboxed

Plugins run in the same process as core, with full access to the machine and to whatever core can
reach. They are not sandboxed in version 1. The contract keeps the code tidy and the data
boundaries clear; it does not contain a hostile plugin. Only install plugins you trust, as you
would any Python package.
