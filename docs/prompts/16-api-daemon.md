# 16 — Local API server and the core daemon

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 13.

## Goal

Run core as the one long-running process, expose the operation registry over a local
HTTP API with an SSE event stream, and make it safe to run on a developer machine
that also browses the web.

## Requirements

1. **Server:** ASGI app (Starlette or FastAPI — pick the smaller footprint that still
   gives OpenAPI from pydantic; record in an ADR) served by `uvicorn` programmatically
   inside core's event loop. Binds **`127.0.0.1` only** (never `0.0.0.0`; reject config
   attempts), port from settings (`0` = free port). On start, writes the **runtime file**
   (`~/.config/oaken/runtime.json`: pid, port, started_at, version) with mode `0600`
   and removes it on clean shutdown; stale runtime files are detected (pid dead) and replaced.
2. **Generated routes:** `POST /v1/ops/{name}` for every operation from the registry
   (body = Input, response = Output); `GET /v1/ops` lists them with JSON Schema;
   `GET /v1/openapi.json`. No hand-written per-operation route.
3. **Events:** `GET /v1/events` — Server-Sent Events from `EventBus.stream()`, with
   event ids and `Last-Event-ID` resume from a small in-memory ring buffer; heartbeat comments.
4. **Auth & browser safety (required, not optional):**
   - A random API token generated at first start, stored in
     `~/.config/oaken/api_token` (`0600`); every request needs
     `Authorization: Bearer …` (constant-time compare). `/v1/health` is the only
     unauthenticated route and reveals nothing but liveness.
   - **Host header allowlist** (`127.0.0.1:<port>`, `localhost:<port>`) to defeat DNS
     rebinding; **Origin check** on state-changing requests; no CORS headers except an
     explicit configured dev origin; `Content-Type: application/json` required for POST.
   - Request size limits and per-client rate limiting on mutating routes.
   - Caller class for the registry is derived from a **client-declared header** that can
     only *lower* privileges (e.g. `X-Oaken-Client: mcp` forces the two-phase confirm
     flow); the token alone never grants the "pre-confirmed" human class for `mcp`.
5. **Daemon lifecycle (`oaken start|stop|status|restart`):** `start` runs in the
   foreground by default (good for systemd/launchd/dev); `--detach` backgrounds it
   portably (subprocess; document the Windows behavior). Single-instance guarantee via
   the runtime file + port bind. Graceful shutdown on SIGINT/SIGTERM (and Windows
   `CTRL_BREAK`): stop accepting requests, let in-flight jobs finish or release leases,
   close DBs, remove the runtime file. Startup order: settings → DBs/migrations → plugin
   host → job runner/scheduler → API.
6. **Static UI hook:** `/` serves files from a configured directory if present (empty
   placeholder page for now). The UI itself is a later phase.

## Acceptance criteria

- Integration tests run core **in-process** with fakes via `httpx.ASGITransport`
  (no real sockets for most tests) plus one real-socket test that boots the daemon on a
  free port.
- Auth tests: missing/wrong token → 401; wrong Host → 400; cross-origin POST → 403;
  token comparison constant-time; `0.0.0.0` bind refused; runtime/token files are `0600`
  (skip the mode assertion on Windows, assert ACL-equivalent best effort and note it).
- SSE test: events published after connect arrive in order; reconnect with
  `Last-Event-ID` replays missed ones within the ring buffer.
- Every registry operation is reachable and its response validates against the generated schema.
- Kill -TERM test (or equivalent) shuts down cleanly with no leaked lease/runtime file.

## Out of scope

The CLI, MCP shim, UI, TLS (loopback only), multi-user.

## Docs site

Register `docs/tools/gen_api_reference.py`, generating a *Reference → HTTP API* page from the
OpenAPI document (auth, Host/Origin rules, each operation route with schemas, the SSE
event stream and event types). This generated page is the API documentation; there is no hand-written `api.md`. `check_docs.py` must pass.

## Checkpoint

Standard report. Include the threat-model table (attacker → vector → mitigation → test)
you implemented against.
