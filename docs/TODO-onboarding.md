# Onboarding Improvements TODO — oaken-core 0.1.1

## Required startup error
- **Startup error if `OAKEN_HOME` is not set**
  - Block daemon/webui startup with a clear error message
  - Guide user to set `OAKEN_HOME` environment variable
  - Suggest a default location (e.g., `~/.oaken` or `~/oaken`)

## Default config initialization
- **Create default empty `config.toml` in `OAKEN_HOME/config/`** on first run
  - Include comments documenting available settings
  - Reference example config from repo (`config.example.toml`)
  - Only create if missing (don't overwrite existing config)

## Friendly welcome pages in webserver
Add these endpoints to the core webserver (webui):

### `/` (root)
- **Friendly index page**
  - Brief introduction to Oaken Mail
  - Navigation to key sections: `/stats`, `/getting_started`, `/docs`, settings
  - Modern, clean design (Bootstrap or similar)
  - **Note:** `/stats` remains its own dedicated page (already exists)

### `/getting_started`
- **Getting Started guide**
  - Step-by-step setup flow
  - Add your first account (link to `/accounts`)
  - Configure sync schedules
  - Set up Sieve filters
  - Enable enrichment plugins
  - How to use MCP tools

### `/docs`
- **Documentation hub**
  - Link to architecture docs (already built)
  - Provider setup guides (Gmail OAuth, IMAP, SMTP)
  - Sieve scripting reference
  - Plugin development
  - API reference
  - MCP tools reference
  - Could serve built HTML docs from `docs/` directory

## Startup message improvements
- **CLI/daemon startup should print:**
  - Welcome message
  - Detected `OAKEN_HOME` location
  - Config file path
  - Database location
  - Webserver URL (with clickable links to getting started)
  - Example: `🌳 Oaken Mail starting at http://localhost:8080 — Visit http://localhost:8080/getting_started`

## Implementation notes
- These changes improve first-run experience and reduce setup friction
- The `OAKEN_HOME` requirement ensures explicit data location (no surprises)
- Friendly web pages reduce need for external documentation during setup
- Aligns with existing architecture (webui serves HTML, daemon coordinates)

## Default vector store must work out of the box (DONE — verification deferred)
The default `sqlite-vec` vector store was optional (behind the `oaken[vec]` extra), so a plain
`uv tool install oaken-cli` started with the plugin failing: `sqlite-vec is not installed`. A tool's
packaging philosophy must not get in the way of its use, so `sqlite-vec` is now a **core dependency**
of `oaken`. The `vec` extra is kept as a backward-compatible alias (the `oaken-core` bundle and
`tests/test_version_lockstep.py` still reference `oaken[vec]`), so nothing downstream breaks.

Changed in this commit:
- `pyproject.toml` — `sqlite-vec>=0.1.9` added to `[project].dependencies`; `vec` extra kept as an alias
- `src/oaken_vec_sqlite/store.py` — error message is now install-method-aware (uv tool + pip) and
  notes the lib normally ships with core
- `docs/architecture/vectors.md` — "Installing" section updated (nothing to install)

**Deferred (pick up when credit limits roll over):**
- [ ] Run `uv sync` and regenerate `uv.lock`
- [ ] `uv run pytest tests/test_version_lockstep.py` and the `oaken_vec_sqlite` tests
- [ ] `uv run lint-imports` and `uv run ruff check`
- [ ] Rebuild generated docs so `docs/architecture/vectors.html` and `docs/architecture/software-map.html`
      (and `docs/softmap.toml`) reflect the new wording
- [ ] Optional hardening: add a user-path install smoke test (install the published artifact the way
      users do, start the daemon, assert the default vector store loads) so this class of regression
      is caught before release

## Related files
- `/home/dfoulks/local/oaken-mail/src/oaken/core/paths.py` — path resolution logic
- `/home/dfoulks/local/oaken-mail/src/oaken/daemon/core.py` — daemon startup
- `/home/dfoulks/local/oaken-mail/apps/webui/src/oaken_webui/` — web server
- `/home/dfoulks/local/oaken-mail/config.example.toml` — example config to base defaults on
- `/home/dfoulks/local/oaken-mail/src/oaken_vec_sqlite/store.py` — default vector store plugin
