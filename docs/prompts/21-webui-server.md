# 21 — Web UI server (oaken-webui)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 16, 17.

## Goal

A web UI that is a **thin client**, in the same mould as the CLI: it serves the static browser
UI and reaches core only through the general HTTP API (`/v1/*`). Core stays a **headless
pipeline** — it exposes the API and does not serve a browser (reversing the earlier "core serves
the UI" sketch). This prompt establishes the server and the client/server split every later UI
prompt builds on.

This is the first prompt of the Web UI phase. The pages themselves are wired feature by feature in
22–27; here we build the thing that serves and proxies them.

## Decisions already taken (do not re-litigate; see ADR 0007)

- The web UI is its own app, `apps/webui/`, distribution **`oaken-webui`**, import package
  `oaken_webui`, console script `oaken-webui` — the same shape as `apps/cli` (ADR 0006).
- It **serves the static UI** at `/` and **reverse-proxies `/v1/*`** to the running core over
  loopback. It never calls the MCP surface.
- It holds the API token server-side (discovered the same way the CLI does) and adds it, plus
  `X-Oaken-Client: ui`, when proxying. The token never reaches the browser.
- Loopback only. It does not start core. Core down → `503 core_unavailable`, like the CLI.
- The UI files are authored in `apps/webui/oakenmail-templates/` and packaged into the wheel as
  `oaken_webui/ui`. The published demo under `docs/demo/` is the same files with
  `data-oaken-demo`; the server strips that marker so the live UI calls the API while the demo
  keeps its fixtures.

## Requirements

1. **Package** `apps/webui` as a workspace member with its own `pyproject.toml`, `src/oaken_webui/`
   and tests beside the code. Register it in the root workspace (members, sources, dev group,
   testpaths, ruff `src`, mypy `files`/`mypy_path`). It depends on `oaken` (version + path helpers)
   and `oaken-cli` (endpoint/token discovery, the `ApiClient` seam) — never on core internals.
2. **Serve + proxy** (`oaken_webui.app`): a Starlette app with
   - `Route("/v1/{path:path}", …)` for all methods → a reverse proxy to core. Resolve the endpoint
     **per request** via `oaken_cli.discovery` (so the UI server can start before core and survive
     a core restart on a new port), or a fixed endpoint for a dev override. Forward the body
     verbatim; drop hop-by-hop headers; add `Authorization: Bearer <token>` and
     `X-Oaken-Client: ui`; **stream** responses so `/v1/events` (SSE) is not buffered.
   - `Mount("/", StaticFiles(...))` serving the UI directory, with `data-oaken-demo` stripped from
     HTML as it is served (non-blocking read).
3. **Entry point** (`oaken_webui.main`): argparse front end + a loopback-only uvicorn runner.
   Flags: `--port` (obscure high-numbered default), `--url`/`--token-file` (dev overrides, passed
   to discovery, loopback enforced), `-v/-vv` (logs to stderr; stdout stays clean). Binding is
   `127.0.0.1` only and cannot be configured otherwise.
4. **Core stays headless.** Do not add new UI-serving to core. Core's existing `static_dir` mount
   in `oaken.api` is now vestigial; leave it (its removal is a separate change) and record it as
   deprecated. Add **ADR 0007** if it is not already present.
5. **Docs & map.** Update `docs/softmap.toml`: the `webui` component is `partial` with
   `path = "apps/webui/src/oaken_webui"` and a summary matching this prompt. Add the CLI/ops audit
   note to 17's table if you add any operator-facing command.

## Acceptance criteria

- Tests (no sockets, no real core): a fake-core ASGI app behind an in-memory transport proves the
  static index is served with the demo marker stripped, non-HTML served unchanged, `/v1/*` proxied
  with the token + `X-Oaken-Client: ui` + method + body + query, SSE streamed, and core-down →
  `503 core_unavailable`.
- `uv run oaken-webui --help` works; `ruff`, `ruff format --check`, `mypy`, the scoped `pytest` and
  the docs check are green.
- The browser reaches core **only** through `/v1/*`; a test or a documented check shows the MCP
  surface is never proxied.

## Report-back checkpoint

End with the six sections from `00-context.md` (Done, Files, How I verified, Deviations, Open
questions, Next prompt readiness). Then stop.
