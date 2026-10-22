# 13. A read-only status page on loopback

- Status: accepted
- Date: 2026-10-22

## Context

There was no way to see whether core is running, or how it is doing, without the shell (`oaken
status`, `oaken pipeline status`) or an API client with a token. A person wants to glance at "is it
up, is it keeping up" in a browser. A first idea was an mDNS `oaken.local` record so the page could
be reached by a friendly name from any device; that pulls in a LAN-reachable bind (against the
loopback-only guarantee of ADR 0005) and an LGPL dependency (`zeroconf`), neither of which an
Apache, local-first project should take on for a convenience. mDNS is deferred.

## Decision

Core serves a **read-only HTML status page at `GET /stats`** on the existing loopback API
listener.

- **Loopback-only and unauthenticated**, exactly like `/v1/health`. The guard still applies its
  Host allowlist (so a web page cannot reach it by DNS rebinding) and its response headers. No
  token is required because the page is reachable only by a process on this machine and carries
  nothing token-worthy — the same trust model `/v1/health` already uses.
- **Counts, health, activity and the operation reference.** A `StatsCollector` (`oaken.api.stats`)
  gathers what the `status`, `pipeline_status`, `usage_summary`, `list_accounts`, `list_plugins`,
  `list_audit` and `list_operations` operations expose: version and uptime, account/message/index
  counts, queue depth, per-stage pipeline throughput and parked jobs, token usage and savings,
  per-account sync state, plugin health, the **recent audit log** (who changed what, when, undo
  state) and the **operation reference** (every operation, its permission, MCP exposure). It reads
  **no message bodies, no addresses, no tokens** — the audit rows are ids and metadata only. A test
  asserts a stored message's subject, body and address never appear, and neither does the API token.

  The audit log is deliberately included even though it is more sensitive than a pure health view:
  a person building Oaken (often without reading the source) needs errors and activity to be
  **visible, not silent**. Exposing it on an unauthenticated loopback page means any process running
  as the same user can read recent activity — but that is already inside the threat model (such a
  process can read the token file), and the transparency is worth more than guarding against it.
  The page still shows no mail content.
- **Rendered with Jinja2**, autoescaped, from a template packaged with core
  (`oaken/api/templates/stats.html`). `jinja2` (BSD, Apache-compatible) moves from the docs-only
  group into core's runtime dependencies.
- **Offline, and one small same-origin script.** The page loads **nothing external** (no CDN):
  styling is inline and the logo is an inline SVG, so it works with no network. It carries **one
  vendored, same-origin script** (`/stats.js`) and nothing inline — an **enhancement, not a
  requirement**: the server renders a complete, correct page without it; the script only re-fetches
  `/stats.json` every ~10s and swaps the numbers in place, so a live watch does not reload the page
  and lose the open tab, and it drives the light/dark theme toggle. The response's
  `Content-Security-Policy` is `default-src 'self'; style-src 'self' 'unsafe-inline'; script-src
  'self'; connect-src 'self'; …` — scripts and fetches are same-origin only, never inline-arbitrary,
  never external. The guard leaves a response's existing CSP in place.

  Crossing from "no script" to one script was a deliberate, reluctant choice: a full-page
  meta-refresh reset the open tab every cycle, which is the wrong experience for someone watching a
  live problem. The cost is contained to a single readable file that can do nothing the page could
  not already show (it reads one local JSON of counts/health and sets text). A no-JS fallback
  remains: the page is whole on first render and can be reloaded by hand.

## Consequences

- `http://127.0.0.1:<port>/stats` is a browser-openable dashboard. The port is the one in
  `runtime.json` (set `[api] port` to pin it). Uptime comes from when `Core.open()` ran.
- The page cannot show mail, and a LAN device or a web page still cannot reach it — the
  loopback-only guarantee of ADR 0005 is untouched, because nothing new binds off-host.
- The one residual exposure is a local process running as the same user, which can already read the
  token file; that is the existing threat model, not a new one.
- **mDNS / `oaken.local` is deferred.** If it is built later it is an **optional** extra (so the
  LGPL `zeroconf` dependency is never required by core) advertising the loopback address, and it
  does not change the bind. A web-based log view is also deferred; for now logs go to `[log] file`.
- The shipped `stats.html` is a self-contained dashboard — General, Ingest, AI & Models, Plugins,
  Audit Log and Reference tabs, with Oaken Mail branding (an inline mark and wordmark) and a
  light/dark theme toggle that persists under `oaken-theme` (the docs site's key). It can be
  restyled freely; the template context is the collector's dict and is the only contract the
  template depends on. The audit and operation-reference tabs are fed by the real `list_audit` and
  `list_operations` operations (invoked internally), so they cannot drift from those operations.

## A possible networked future (not decided here)

A LAN-reachable surface — so AI agents on the local network can connect, which some operators
want — may come later. This ADR does not build it and does not rule it out, but it fixes the
defaults it must respect:

- Loopback stays the **default**; any off-host bind is **opt-in and default-off**, named
  explicitly in config, and recorded in its own ADR that revisits ADR 0005's "configuration cannot
  ask for another address".
- A networked surface is **authenticated** (it leaves the local-only trust model the moment it is
  reachable off-host), and the **read-only, counts/health-only** nature of `/stats` is exactly what
  makes it the safest candidate to expose first — but exposing it is still a separate decision with
  its own threat model (who on the LAN, what auth, what the agent door is).
- Nothing in today's collector or template needs to change for that: the data is already free of
  mail content, so a future networked view reuses it as-is behind whatever auth that ADR chooses.

