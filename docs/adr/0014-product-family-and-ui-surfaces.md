# 14. The Oaken product family and the surfaces a UI builds on

- Status: accepted
- Date: 2026-10-22

## Context

ADR 0002 split Oaken into one core daemon and thin clients; ADR 0006 made each client its own
`apps/` distribution; Core is headless and serves no product UI. Those
decisions were made with the CLI and one UI in view. The product intent is now clearer, and
there will be **more than one** front-end: a UI today, and likely a mobile and/or a desktop UI
later (exact shape deliberately unspecified). Before a second UI exists, the boundaries need to be
written down so each new front-end consumes core through stable surfaces rather than eroding it.

This ADR records the product line and the contract a UI depends on. It does **not** design any
future UI, and it does not change code.

## Decision

### The product family

- **Oaken Core™** is the product that is the **ingestion & enrichment pipeline + the CLI + the MCP
  server**. Its purpose is to let an AI work with your mail, locally. In packaging terms it is the
  `oaken` distribution (the engine: pipeline, stores, jobs, scheduler, plugin host, the local HTTP
  API daemon, the MCP server at `/mcp`, the read-only `/stats` page) plus the `oaken-cli`
  distribution (the management surface and the stdio MCP bridge), with the first-party plugins
  (Gmail, IMAP, SMTP, Ollama, enrichers, the sqlite vector store). The CLI is part of the product,
  not optional: on a headless install it is the human control plane. The MCP server is the
  AI-facing surface and is the central offering (ADR 0004).
- **A UI is a separate product built on Core's surfaces.** A web, a mobile and a
  desktop UI are anticipated. Each is its own distribution, consumes core only through the surfaces
  below, and ships and versions independently of Core. Core never depends on a UI, and one UI never
  depends on another.

### The surfaces a UI builds on (the stable contract)

1. **The HTTP API (`/v1/*`) and its generated OpenAPI document** — the **language-neutral**
   contract. It is generated from the operation registry (ADR 0005), so it cannot drift from what
   core does, and the operation schemas are snapshot-pinned (ADR 0010). **Any non-Python UI
   (mobile, desktop) builds against this**, not against the CLI.
2. **The MCP server (`/mcp`)** — the AI-facing surface, same registry, its own token and limits.
3. **The CLI's `ApiClient` seam (`oaken_cli`)** — a convenience for a **Python** front-end that
   wants endpoint/token discovery and a typed client without re-implementing them. A Python UI can use it. A non-Python UI does not; it uses surface 1 directly.

The API, not the CLI, is the durable UI contract. The CLI client is an ergonomic shortcut for the
Python case only.

### Deferred decision: off-host access (what a mobile UI forces)

Core binds loopback only, and configuration cannot ask for another address (ADR 0005). A **desktop**
UI runs on the same machine, so loopback is enough, exactly as a local UI would assume. A **mobile** UI
runs on a different device and **cannot reach `127.0.0.1`** on the user's machine — so the first
real mobile UI forces an **authenticated, networked, off-host surface**, which ADR 0005's
loopback-only guarantee forbids today.

This is flagged, not decided. When it is taken up it gets its **own ADR**, which must keep:

- loopback as the default, any off-host bind **opt-in and default-off**, named explicitly in config;
- authentication on the networked surface (it leaves the local-only trust model the moment it is
  reachable off-host) — the current `/v1/health` and `/stats` unauthenticated-on-loopback exception
  does **not** extend off-host;
- the same per-surface token and guard model (ADR 0005) applied to whatever the networked door is.

The same door is what the "AI agents on the local network" idea (ADR 0013's deferred note) would
use; they are one decision, not two.

## Consequences

- The product name "Oaken Core™" now means exactly one thing: pipeline + CLI + MCP, the
  AI-works-with-your-mail product. "Core" in the architecture sense (core vs. clients) and `oaken`
  the Python package are related but not the same; this ADR is the product definition.
- A new UI is additive: a new `apps/` distribution against the HTTP API (and the CLI client seam if
  it is Python). It needs no change to core, and the snapshot tests and import-boundary contracts
  (ADRs 0010, 34.2) are what let it rely on the API without surprise breakage.
- The packaging that expresses this (an `oaken-core` bundle that installs `oaken` + `oaken-cli` +
  the vector extra, with each UI separate) is a release step, taken when a beta is cut; this ADR is
  the decision it implements.
- The loopback-only guarantee stands until a networked-surface ADR revisits it. A mobile UI cannot
  ship before that decision is made, and that is the intended ordering: the boundary is recorded now
  so it is a deliberate choice later, not an ad-hoc one under pressure.
