# Oaken Core

**Oaken Core** is the ingestion & enrichment pipeline, the CLI, and the MCP server — the product
that lets an AI work with your mail, locally, with nothing leaving your machine unless you
configure it to.

Installing this bundle gives you the whole non-UI product:

- the **pipeline**: mail is pulled to local disk, filtered by your Sieve rules, parsed, indexed
  (keyword + vectors) and enriched (summary, triage, actions, header/MIME inspector) by a local
  model at ingest time, so search and question-answering are cheap in tokens;
- the local **API daemon** on `127.0.0.1` and the read-only `/stats` status page;
- the **MCP server** (`/mcp`) — the AI-facing surface, so a stronger model can analyse and act on
  your mail through a narrow, audited, confirm-gated door while the mail stays on your machine;
- the **`oaken` command line** to start, configure and drive it (`oaken start`, `oaken gmail auth`,
  `oaken sync`, `oaken search`, `oaken ask`, `oaken pipeline`, `oaken mcp`, …);
- the first-party plugins: Gmail, IMAP, SMTP, Ollama, the enrichers, and the sqlite-vec store.

```bash
pip install oaken-core
```

The web UI is a **separate product**, built on the same API and CLI framework — install it on its
own with `pip install oaken-webui`. Other front-ends (desktop, mobile) are anticipated and are
likewise separate products that build on Oaken Core's HTTP API and MCP surfaces. See
[ADR 0014](../../docs/adr/0014-product-family-and-ui-surfaces.md) for the product family and the
surfaces a UI builds on.

License: Apache-2.0.
