+++
title = "The daemon and the local API"
subtitle = "One process, one loopback port, three ways in"
lead = "Core runs as one process that owns the databases, the plugins and the job queue, and serves a small HTTP API on 127.0.0.1. The CLI and other local clients are clients of it; the MCP server is on the same port. This page covers how it starts and stops, and how it keeps a web page or another program from using it."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 27
description = "The core daemon: startup and shutdown order, the runtime file, the API token, the Host, Origin and content-type guards, event streaming, and the threat model."
+++

## One process

`oaken.daemon` is the composition root. It opens every store and service once and wires them in a
fixed order:

1. settings;
2. the databases, running migrations;
3. the plugin host (providers, embedder, model backends, enrichers);
4. the job runner and the sync schedule;
5. the API, last, so nothing is asked of a half-started core.

Stopping runs the other way: the API stops accepting and ends its event streams, jobs get
`[api] shutdown_grace_seconds` to finish or hand their leases back, the sync schedule and plugins
stop, then the databases close. A process that is killed instead is covered by lease expiry, as for
any worker (see [jobs](jobs.md)).

```text
python -m oaken.daemon      # the foreground process: Ctrl-C or SIGTERM stops it cleanly
```

The same lifecycle is available to Python as `oaken.daemon.serve`, `detach`, `status`, `stop` and
`restart`. The CLI's `oaken start`, `stop`, `status` and `restart` are thin wrappers over them.
`status` reads only the runtime file, so it works with no token and never starts anything.

`oaken start` and `oaken restart` run core in the background by default, in a new session on Linux
and macOS (closing the terminal does not stop it) and as a detached process in its own group on
Windows. Windows has no console for it, and it stops on `CTRL_BREAK`; `stop` sends that. Standard
input and output go to the null device either way. `--fg` keeps core attached to the terminal,
logging to it (`--detach` is the old spelling of the default and is still accepted).

`oaken stop` asks core to stop and gives it `--grace` seconds (40 by default: the 30-second job
drain above and a margin) while it says what it is waiting for. If core is still running then, it is
killed, and its runtime file is cleared so the next start is clean. A killed core loses nothing: its
jobs are retried when their leases expire. `--no-kill` reports an error instead, and `--grace 0`
kills it at once. `oaken restart` takes the same two flags.

## Logs

Core logs structured lines to stderr. A detached core has no stderr (it goes to the null device),
so set `[log] file` to keep its logs: lines are then written to that file **as well as** stderr.
A relative path sits under the data directory; an absolute one is used as given. The file is
created owner-only (`0600`) and appended to, and it rotates by size — at `[log] file_max_bytes` it
is rolled to `oaken.log.1`, the previous `.1` to `.2`, and so on, keeping `[log] file_backups`
copies. A file that cannot be written never costs the process its log or stops it: the write is
dropped and stderr still gets the line. `[log] level` and `[log] format` (`console` or `json`)
apply to both destinations, and a `reload` (SIGHUP) picks up a change to any of them at once.
Secrets are redacted and long values truncated before a line is rendered, so mail content and
tokens do not reach a log by accident.

## Finding it: the runtime file

While core runs, `runtime.json` in the runtime directory holds `pid`, `host`, `port`, `version` and
`started_at`. It is created owner-only (`0600`), and it is the single-instance lock: it is created
exclusively, with port 0 meaning "starting", before anything is opened. If it already exists and its
process is alive, the second start stops with "core is already running". If that process is gone the
file is stale and is replaced. A clean shutdown removes it, and only the process that wrote it
removes it.

The API port defaults to 0, a free port the kernel chooses, and the file is how clients find it.
Set `[api] port` for a fixed one.

## The token

On first start core makes a random 256-bit token and stores it in `api_token` in the config
directory, mode `0600`. Every request but one needs `Authorization: Bearer <token>`. The token is
compared in constant time. `GET /v1/health` is open and answers `{"status": "ok"}`, nothing more.

Agents have tokens of their own (`oaken_agent_...`, made by `create_agent`) that open only
`/v1/agent/`. See [the swarm](swarm.md).

## What a request passes through

A browser can send a request to `127.0.0.1:PORT` from any page the user has open, so being
on loopback is not protection. Each request is checked in this order, by one guard that wraps every
route:

| Check | Refusal | Stops |
|---|---|---|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` | 400 | DNS rebinding |
| Preflight answered only for the one configured dev origin | no CORS headers | a page reading responses |
| A mutating request with a foreign `Origin` or `Sec-Fetch-Site: cross-site` | 403 | cross-site request forgery, even with a leaked token |
| A `POST` has `Content-Type: application/json` | 415 | form and `text/plain` posts a page can send without a preflight |
| A bearer token for the surface being addressed | 401 | another process or page without the token |
| A per-client token bucket on mutating requests | 429 | a loop, a runaway agent |
| Body size | 413 | memory exhaustion |
| Concurrent requests per surface | 429 | one door starving another |

Responses carry `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`Cache-Control: no-store` on the API, and a strict `Content-Security-Policy` on static pages. Requests
are never logged, because a URL can hold a search.

## Who is calling

The registry has a caller class, which decides whether a person's `confirm` is accepted (see
[operations](operations.md)). Over HTTP:

- a request is `cli` unless it says otherwise;
- `X-Oaken-Client: ui` is an interactive UI client, the same privileges;
- `X-Oaken-Client: mcp` makes the request an agent's: it cannot confirm by saying so, only with the
  token from its own proposal, and operations closed to MCP are hidden and refused;
- anything else, including `internal`, is a 400. A header can lower privilege and never raise it.

The MCP server mounts at `/mcp` on this listener as its own surface with its own token (`mcp_token`,
mode 0600) and limits, and the mount, not a header, sets its class
([ADR 0004](repo:docs/adr/0004-mcp-server-in-core.md)). It is off until `[api] mcp = true`, and then
`/mcp` exists; with it off the path is a 404. With the swarm on as well, a second door at `/mcp/agent`
takes agent tokens and offers only the job tools. A door is exactly its own path. See
[Connect an AI client](../mcp.html).

## The routes

Operations are not routed by hand. `GET /v1/operations` lists the registry with JSON Schemas and
`POST /v1/operations/{name}` calls one through the same client the CLI uses in-process. The
[HTTP API reference](../reference/api.html) is generated from the OpenAPI document, which is served at
`/v1/openapi.json`. Everything else is small:

- `GET /v1/events`: server-sent events. Each has an id, a type and a JSON body of ids and counts, never
  message text. Send `Last-Event-ID` to resume from the in-memory ring (`[api] event_ring_size`). An id
  older than the ring, or from before core restarted, gets a `gap` event, which means "fetch again".
  A quiet stream sends a `: heartbeat` comment.
- `/v1/agent/jobs/...`: claim, heartbeat, complete and fail, for the swarm.
- `/`: files from `[api] static_dir` when set, otherwise a placeholder page.
- `GET /stats`: a read-only status page, HTML (see below). Like `/v1/health` it needs no token —
  it is loopback-only and shows counts and health, never mail.

## The status page

`GET /stats` is a browser-openable dashboard: version and uptime, account/message/index counts,
queue depth, per-stage pipeline throughput and parked jobs, token usage and savings, per-account
sync state, and plugin health. It is **unauthenticated on purpose** — it is reachable only from this
machine (the Host guard still refuses a forged name), and it carries **counts and health only**: no
message text, no addresses, no tokens, and not the audit log. Open `http://127.0.0.1:<port>/stats`
(the port is in `runtime.json`; set `[api] port` to pin it). The page is self-contained — inline
styling, no external assets, no scripts — so it works offline. See
[ADR 0013](repo:docs/adr/0013-read-only-stats-page.md); a friendly `oaken.local` name and a
networked, authenticated variant for AI agents are possible later and deliberately not built here.

## Threat model

| Attacker | Vector | Mitigation | Test |
|---|---|---|---|
| A web page the user visits | `fetch` to `127.0.0.1:PORT` | No CORS headers, so it cannot read a response; JSON-only `POST`; Origin and `Sec-Fetch-Site` refused; the token is not in the page's reach | `test_guard.py` |
| A web page, by DNS rebinding | A name that resolves to 127.0.0.1 | Host allowlist, checked first | `test_a_wrong_host_is_refused_before_anything_else` |
| A web page, by a plain form | `text/plain` or form `POST` | 415, and Origin refused | `test_a_post_must_be_json` |
| Another machine | Connecting to the port | Binds 127.0.0.1 only; configuration cannot say otherwise; the server refuses another address | `test_a_non_loopback_bind_cannot_be_configured` |
| Another local user | Reading the token or runtime file | Both `0600` from creation; the directories are `0700` | `test_the_token_file_is_private`, `test_the_file_is_private_from_the_first_byte` |
| Anything on the machine without the token | Guessing it | 256 random bits; constant-time compare; the rate limit | `test_the_token_is_compared_in_constant_time` |
| A compromised or confused agent | Using its token on the general API | Agent tokens open `/v1/agent/` only, and the API token opens no agent route | `test_an_agent_token_is_accepted_by_no_other_route` |
| An MCP client | Claiming to be a person | `X-Oaken-Client: mcp` only lowers; a confirmation token is bound to the class that was given it | `test_the_header_can_only_lower_privilege` |
| An agent | Revoked but still holding a lease | The lease goes back to the queue; its next call is a 401 | `test_a_revoked_agent_is_refused_at_once` |
| A caller | Huge bodies, floods, many parallel calls | Size, rate and concurrency limits per surface | `test_a_body_over_the_limit_is_refused`, `test_mutating_requests_are_rate_limited_per_client` |
| A second core | Starting on the same data | Runtime file as an exclusive lock | `test_a_second_process_exits_with_a_clear_error` |
| A crash | A stale runtime file | Replaced when its pid is gone | `test_a_stale_runtime_file_is_replaced_by_a_start` |
| A log reader | Message text in logs or events | Events and errors carry ids only; requests are not logged | `test_event_payloads_carry_ids_and_never_message_text` |

Not covered: a process running as the same user can read the token file and do what the user can.
The audit log records what it did.

## Not built yet

- The ring is in memory: a restart is a `gap` for every client.
- Per-agent rate limits beyond the lease and hourly quotas in [the swarm](swarm.md).
