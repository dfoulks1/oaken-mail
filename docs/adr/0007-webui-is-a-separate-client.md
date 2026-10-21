# 7. The web UI is a separate client; core stays headless

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0002 said core hosts the web UI: the daemon's API process would also serve the built UI
files, and the architecture sketch put the general API and the browser behind one core process.
The API component was built that way — `oaken.api` mounts `StaticFiles` at `/` when
`[api] static_dir` points at a folder, and shows a placeholder otherwise.

Keeping core as the thing that also serves a browser pulls two unrelated concerns into one
process: the long-running mail pipeline (sync, ingest, jobs, scheduler, the operation registry)
and the delivery of HTML/CSS/JS to a browser. It also means a UI change, or a second UI, touches
the core daemon. We want core to be a **pipeline with an API** and nothing else, matching how
`oaken-cli` already treats it: a thin client over the HTTP bridge (ADR 0005), never importing core
internals beyond the shared path/version helpers.

ADR 0006 already anticipated this — "the web UI follows the same shape when it becomes a served
application (`apps/webui`)" — so this ADR only records the reversal of 0002's "core serves the UI".

## Decision

- **Core is headless.** It exposes the general API (`/v1/*`) and the MCP surface, and serves no
  browser UI. The pipeline is its only job.
- **`oaken-webui` is a client app**, `apps/webui/`, the same shape as `apps/cli`: a workspace
  member with its own `pyproject.toml`, `src/oaken_webui/`, tests beside the code, and the
  `oaken-webui` console script. It depends on `oaken` (version and path helpers) and on
  `oaken-cli` (the endpoint/token discovery and the `ApiClient` seam), not on core internals.
- **It serves the UI and proxies the API.** `oaken-webui` serves the static files at `/` and
  reverse-proxies `/v1/*` to the running core daemon over loopback — the **general API bridge the
  CLI uses**, never the MCP surface. It holds the API token and attaches it (and
  `X-Oaken-Client: ui`) on the way through, so the token never reaches the browser and the browser
  sees one same-origin server.
- **It is loopback-only and does not start core.** If core is down the proxy answers
  `503 core_unavailable`, exactly as the CLI reports a core-down.
- The UI files are authored in `apps/webui/oakenmail-templates/` and packaged into the wheel as
  `oaken_webui/ui`. The published demo under `docs/demo/` is the same files with a `data-oaken-demo`
  marker; `oaken-webui` strips that marker when serving live, so the real UI calls the API while the
  demo keeps its dummy fixtures.

## Consequences

- Core's `static_dir` mount in `oaken.api` is now **vestigial**: nothing points it at the UI, and
  the default placeholder is what a direct hit on core returns. It is left in place for now (its
  removal is a separate, test-touching change) and should be considered deprecated; a later prompt
  may delete it.
- Running the UI is two processes: `oaken start` (core) and `oaken-webui` (the UI server). This is
  the same operational model as a CLI talking to a daemon.
- A second or replacement UI is another app under `apps/`, with no change to core.
- Because the UI reaches core only through `/v1/*`, every mail action remains a registry operation
  behind the confirm gate and the audit log; the browser cannot bypass them, and the MCP surface is
  never exposed to it.
