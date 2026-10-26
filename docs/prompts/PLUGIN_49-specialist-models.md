# 49 — Specialist models: small, fast enrichers, and workers to farm them to

**Read first:** `docs/prompts/00-context.md`, `ENRICHER_14-enrichers.md`, `CORE_47-split-enrich.md`
(each enricher is its own job kind), `CORE_48-ask-router.md` (an enricher can declare an
`AskIntent`) and ADR 0010.
**Depends on:** 14, 33, 47, 48. 47 must be built first.
**Track:** PLUGIN. A new first-party package `src/oaken_onnx/`, an optional extra, and docs. Core
changes are limited to what 47 already adds.

## Why

A language model is the wrong tool for much of what Oaken derives from mail. Triage, sentiment,
language and spam are classifications: a small encoder answers in milliseconds on a CPU, where a
generative model takes seconds and drifts (a 3B model rated junk "normal"). Specialist models are
small, fast and cheap enough to run on a spare machine.

## Goal

A person can add a specialist enricher backed by a small ONNX model, and run workers (on this
machine or another) that claim just that enricher's jobs. The first proof is one specialist.

## Decisions already taken (do not re-litigate)

- **It is a plugin, not core.** A specialist is an `enricher` with `needs_llm = False`. Core stays
  headless and gets no ML dependency.
- **ONNX Runtime on CPU, not `torch`.** Small, no GPU, predictable. `torch` and `transformers` are
  out of scope.
- **An optional extra** (`oaken-mail[onnx]`). Without it nothing changes and nothing is imported.
- **A specialist teaches `ask` and the gate for free.** It declares an `AskIntent` (48) and may carry
  a `min_priority` bar like any enricher.
- **Workers are ordinary agents.** A specialist worker claims `ingest.enrich.<name>` (47) over the
  agent door, with the same token, quota, audit row and `[mcp] workspaces` scope as any agent.
- **Model files are data, never code.** Only `.onnx` weights and a tokenizer file are loaded, no
  pickles, no `trust_remote_code`, and only after a sha256 pin matches.
- **No network at runtime.** Models are fetched by an explicit command, once.

## Not in scope

Generation, GPUs, fine-tuning, training on a person's corrections (a good later prompt), and
bundling model files in the repository.

## Requirements

1. **`oaken_onnx`** with an `OnnxEnricher` base: loads a model from a path, runs inference in a
   thread (`asyncio.to_thread`) with batching, truncates to a token limit, reports a model
   that failed to load as an unhealthy plugin, and exposes `config_model` settings (`model_path`,
   `threads`, `max_tokens`) so the console's plugin dialog edits them.
2. **An `Engine` protocol** (`classify(texts) -> list[scores]`) with an ONNX implementation and a fake
   for tests, so the plumbing is tested with no model file and no extra installed.
3. **One specialist** to prove it, chosen in the report-back (sentiment, language or junk), with its
   `output_model`, `ask_intent`, and a model whose license is Apache-2.0 or MIT, where the license
   was checked is stated in the report.
4. **A fetch command**, `oaken onnx fetch NAME`: downloads the pinned file, checks the sha256, and
   refuses on a mismatch or a file that is not plain ONNX. It is the only network use.
5. **A worker recipe.** `docs/plugins.md` gets a short section and a script showing a specialist worker:
   claim a split kind, run the model, `complete_job` with the enricher's result.
6. **Health and docs.** `plugin_health` shows the model, its size and the thread count. Add the extra
   to the install docs and to `CONTRIBUTING.md`'s status table.

## Acceptance criteria

- With the extra not installed, importing Oaken and running the pipeline imports nothing from
  `oaken_onnx`, and the plugin reports itself unavailable with a clear message.
- With the fake engine, the specialist stores a valid result, appears in the `ask` menu, honors a
  `min_priority` bar, and its job can be completed by an agent allowed only that kind.
- `oaken onnx fetch` refuses a wrong checksum and a non-ONNX file, and writes nothing on refusal.
- A model that fails to load makes the plugin unhealthy and never fails an unrelated enrich job.
- Tests never use the network or a real model file (one optional test, skipped by default, may).
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- Q1: which specialist first: **language** (a tiny, clear win on mail like the Russian junk), then
  junk, then sentiment.
- Q2: where models live: **the data directory**, `models/`, never the repository.
- Q3: a worker's result for a deterministic enricher is validated by core: **yes**, against its
  `output_model`, like any agent answer.

## Report-back checkpoint

End with the six sections from `00-context.md`, naming the specialist and model, its license and
where it was checked. Then stop.
