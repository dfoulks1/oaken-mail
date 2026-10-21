# oaken-webui

The Oaken Mail **web UI server**: a thin client, parallel to `oaken-cli`.

It does two things and holds no business logic:

1. Serves the static browser UI (the templates in `oakenmail-templates/`) at `/`.
2. Reverse-proxies `/v1/*` to the running core daemon — the same general-API bridge the CLI
   uses (`POST /v1/operations/{name}`, `GET /v1/events`, …), never the MCP surface.

Core stays a headless pipeline: it exposes the API and does not serve the UI itself (see
`docs/adr/0007-webui-is-a-separate-client.md`). The browser talks only to `oaken-webui`, which
holds the API token server-side and attaches `X-Oaken-Client: ui` on the way through, so the
token never reaches the browser.

```
browser ──HTTP──> oaken-webui ──loopback──> oaken core (/v1/*)
            (static UI)   (reverse proxy, token + ui class)
```

## Run

Start core first (`oaken start`), then:

```
oaken-webui                 # serves http://127.0.0.1:47813 by default
oaken-webui --port 9000
oaken-webui --url http://127.0.0.1:8123   # dev override: a hand-started core
```

Loopback only, like the rest of Oaken.
