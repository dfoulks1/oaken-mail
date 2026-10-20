+++
title = "Prompt safety and workspaces"
subtitle = "How mail reaches a language model without ever becoming an instruction"
lead = "Mail is data. It enters a prompt only inside a fenced block with a fresh random nonce, the instructions are constants written in this codebase, and the answer is JSON that is checked, never interpreted."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 38
description = "UntrustedText and delimiters, the prompt builder, structured output and its single repair retry, the per-workspace LLM cache, the secure and insecure workspaces, and the Ollama plugin's rules."
+++

## The rule

A message can say anything, including "ignore your instructions". So the code never lets mail text
near the instructions:

- The **system prompt** is a constant. A test reads the source of every module and fails if an
  `LLMRequest` is built from an f-string, a concatenation or any raw string that could hold mail.
- Mail enters only as `UntrustedText` in the **user turn**. It has no `str()`; the one way out is
  `render(label=...)`, which fences it:

  ```text
  <<<EMAIL label=body nonce=3f9c2a71d04be856>>>
  ...the message text...
  <<<END nonce=3f9c2a71d04be856>>>
  ```

- The nonce is new for every call, so a message cannot know it in advance. Text that looks like a
  delimiter is neutralized first: runs of two or more `<` or `>` (including full-width and other
  look-alikes) become `‹` and `›`, and invisible characters (zero-width, bidirectional controls)
  are removed. A single `<`, as in `<alex@example.com>`, is left alone.
- The system prompt tells the model that the fenced text is data, to ignore instructions in it and
  to set `injection_suspected` when it sees any. That is a request, not a guarantee: see below.

## Structured answers

Each task is a `PromptBuilder`: literal instructions, a pydantic answer model and a version. The
JSON schema goes in the request, and the reply is validated. A reply that is not valid JSON, does
not match, or is longer than `ai.max_output_chars` gets **one** repair attempt (the same request
plus a plain note naming the fields that were wrong, never the input). If that fails too, the call
raises `AIBackendError`. Tokens spent on the failed attempt are still counted in `ai_usage`.
The builder's version includes a hash of its own text, so editing a prompt without bumping the
version cannot return an answer made by the old one.

## The cache and workspaces

`generate_cached()` is the only function enrichers call. It looks up `llm_cache` in `raw.db` by
**(workspace, task, content hash, model, prompt version)**. A hit makes no backend call and is
recorded in `ai_usage` as cached.

A **workspace** is a named profile with a trust level, an LLM backend, and its own slice of the
cache and usage:

| Trust | Backend must be | Meant for |
| --- | --- | --- |
| `secure` | local (`locality = "local"`) | Mail that must not leave this machine. The default. |
| `insecure` | anything | Mail the user is happy to send to a hosted model. |

Because the workspace is part of the cache key, a secure workspace's results are never served to
an insecure one, and the reverse. A backend that does not say where it runs counts as remote. An
account picks its workspace with `config["workspace"]` (non-secret); Sieve-based routing comes in a
later prompt. Only the language model is partitioned: embeddings use the single configured embedder.

```toml
[workspaces.private]
trust = "secure"
llm_backend = "ollama"

[workspaces.cloud]
trust = "insecure"
llm_backend = "ollama-remote"
```

## The Ollama plugin

`oaken_ollama` is the first-party embedder and LLM backend.

- It talks to port 11434 on `127.0.0.1` by default. Only `localhost`, `127.0.0.0/8` and `::1` count
  as loopback; any other address stops the plugin at start unless `ai.allow_remote = true`, and a
  one-time warning is logged when it is. The LLM backend reports `locality` from the address.
- At start it asks `/api/tags` whether the model exists. If not, the error is the exact command,
  for example `ollama pull qwen2.5:3b`. It never pulls anything itself.
- It reads the context window from `/api/show` and learns the embedding size by embedding a probe.
- It retries only connection errors and 5xx answers, a few times with a growing pause. Error
  messages never include what was sent or received, because both are mail.
- Structured output uses Ollama's `format` field with the task's JSON schema.

```toml
[plugins.ollama]
# base_url defaults to this machine
keep_alive = "5m"
batch_size = 32
```

Model names come from `ai.embed_model` and `ai.llm_model` (defaults `nomic-embed-text` and
`qwen2.5:3b`, both placeholders until tried) and can be overridden per plugin with `model`.

## What this does not prove

Delimiters, nonces and a schema make an attack harder and make a success visible; they do not make
a model obey. A model can still be talked into a wrong summary, which is why the answers are
validated, never executed, and never allowed to trigger an action by themselves. The unit tests
check what is sent and what is refused. They cannot show how a particular model behaves when it
reads a hostile message; that needs a live run against a real model, which is still to do.
The cache may contain text derived from mail, so it is as sensitive as `raw.db` and is cleared
per workspace.
