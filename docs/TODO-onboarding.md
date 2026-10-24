# Onboarding Improvements TODO — oaken-core 0.1.1

Status: a batch of 0.1.1 onboarding work is built on branch `feat/0.1.1-onboarding`
(uncommitted, awaiting QA). Version bumped to **0.1.1**. 299 tests pass, ruff clean.

## SHIPPED this session (on feat/0.1.1-onboarding, pending QA + commit)

### Core web console (new pages, all loopback-only, token-free, Bootstrap + shared nav)
- **`/` welcome page** — live: real version, `OAKEN_HOME`/config/data paths, bound port, and a
  short status pull. Replaces the old `PLACEHOLDER` string in `Api.placeholder`. Rendered from
  `src/oaken/api/templates/welcome.html` via `oaken.api.welcome` (mirrors the `stats.py` pattern).
- **`/config` editor** — textarea over `config.toml` with Test / Save / Reload, backed by new
  `config_*` operations. Save validates then writes a timestamped `.bak` then the file; Reload
  triggers the daemon's SIGHUP reload (validate-before-apply, auto-rollback). **Bug caught & fixed:
  mutate ops need `confirm=True` through CoreClient, or the route 200s and writes nothing.**
- **`/jobs` console** — inspect the durable queue (`list_jobs`) + control the pipeline
  (pause/resume backfill, retry parked, per-job cancel/retry). Pure UI over existing operations;
  every action is audited. e2e test proves pause *actually* pauses.
- **`/getting_started`** — in-app walkthrough of the config.toml sections (temporary scaffolding;
  see "Future direction" below). Fixed the welcome page's link that previously 404'd.
- **`/stats`** — restyled from bespoke CSS onto Bootstrap for consistency; content/`data-stat`
  hooks preserved.
- **Shared nav** — `src/oaken/api/templates/_nav.html`, included by all core pages. Home / Stats /
  Jobs / Config / Docs (new tab) + theme toggle, with `active` highlight.

### Identity (0.1.1 = Bootstrap, not a full design system yet)
- Shared Oaken palette in `_nav.html`: oak greens + acorn brown/orange + bark.
- `--oaken-accent` flips **oak green in light → acorn orange in dark**; nav, active underline,
  hero box, tile icons, `.btn-oaken`, and the welcome version badge all follow it.
- Contrast-safe badges (`.badge-oaken-warn`) replace Bootstrap's washed-out dark-mode yellow.

### New operations
- `config_get` (read), `config_validate` (read), `config_save` (mutate+audit),
  `config_reload` (mutate+audit) in `src/oaken/core/operations/builtin/config.py`.

### Start message
- `oaken start/restart` now prints `🌳 Oaken Mail started: <url>` + pointers to `/`, `/config`,
  `/stats` (in `apps/cli/.../commands/daemon_cmd.py`).

### Tests added
- `test_config_editor.py`, `test_jobs_console.py`, `test_getting_started.py`, `test_config_ops.py`;
  updated `test_read_state.py` + regenerated the operation schema snapshot for the 4 new ops;
  updated two `test_stats.py` tests for the Bootstrap/shared-nav reality.

## 0.1.1 scope (decided with Drew: no new features, make 0.1 useful)
Done: OAKEN_HOME warn-and-default plus a last-known-home pointer (`paths.remember_home`, kept
outside the home because the databases live inside it); sticky port across reloads; unconfigured
plugins report "not configured" not an error; commented default `config.toml` generated from
`Settings`. A process cannot export a variable into the shell, so the warning prints the
`export OAKEN_HOME=...` line to add to a profile.

Deferred (new features, not 0.1.x): per-plugin enable / disable / reload at runtime, and a generic
plugin `run_function` (an `oaken operation` equivalent so the CLI can call functions a plugin
exposes, e.g. a Gmail login the plugin owns end to end). Note plugin operations already exist as
`<plugin>.<name>`; the gap is that they are collected only for plugins started at boot.
Also deferred: a configurable bind host (remote mode); `[api] port` is already configurable.

## STILL OPEN (0.1.1 onboarding, not yet done)

- **Startup error if `OAKEN_HOME` is not set** — block/ warn at daemon start, suggest `~/.oaken`.
  (Design call still needed: hard-fail vs warn-and-default. Tests already set OAKEN_HOME, so a
  hard error won't break them.) Touch: `src/oaken/core/paths.py`, `src/oaken/daemon/core.py`.
- **Default `config.toml` on first run** — write a commented default into `config_dir()` if
  missing. Prefer generating from the `Settings` models (descriptions as comments) over shipping
  `config.example.toml` as package data. The `/config` editor now covers the *edit* story, so this
  is lower priority than it was.

## Future direction (noted by Drew)
- `/getting_started` is **temporary scaffolding** — to be replaced by a leaner guide + a proper
  **webui**, while **keeping the web config editor** (core owns config; editing it where config
  lives is the model worth keeping).
- Bootstrap **offline/vendoring** is parked (not a contradiction: you need the network to install
  Oaken anyway, so CDN-for-first-paint is acceptable for now).

## Implementation notes
- First-run experience + explicit data location (no surprises) + web pages reduce external-doc
  dependence. The console pages are UI over the operation registry — "one door, many surfaces".

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

## Core home page: welcome.html (DONE this session)
`welcome.html` is **oaken-core's** home page (not a webui page — the inbox stays in oaken-webui).
Shipped: it is now a live Jinja template rendered by `Api.placeholder` via `oaken.api.welcome`,
receiving the real version, status (from the `status` op), and daemon env (`OAKEN_HOME`, config/
data dirs, bound port). The `data-oaken-demo` marker and webui asset/script refs were stripped;
it is Bootstrap + the shared nav, themed via `--oaken-accent`. See `src/oaken/api/templates/
welcome.html` and `src/oaken/api/welcome.py`.


- `/home/dfoulks/local/oaken-mail/src/oaken/core/paths.py` — path resolution logic
- `/home/dfoulks/local/oaken-mail/src/oaken/daemon/core.py` — daemon startup
- `/home/dfoulks/local/oaken-mail/apps/webui/src/oaken_webui/` — web server
- `/home/dfoulks/local/oaken-mail/config.example.toml` — example config to base defaults on
- `/home/dfoulks/local/oaken-mail/src/oaken_vec_sqlite/store.py` — default vector store plugin

## Gmail authorization — make "connect Gmail" easy (FUTURE)
Setting up Gmail is the worst part of onboarding: the user has to create a Google OAuth client by
hand (Cloud Console → Desktop client → enable Gmail API → publish to Production → download JSON)
before Oaken can do anything. Goal: get this as close to "click Connect, sign in, done" as is
honestly possible, without betraying local-first.

**The flow belongs to the gmail plugin, not core.** Gmail is a default *plugin* (`oaken_gmail`,
`oaken.providers` entry point), not a core function — and the import-linter contracts already
forbid core from importing provider code. So the OAuth dance (loopback redirect server, consent
URL, code→token exchange, keyring write) lives behind the provider contract in the plugin. Core
only exposes the generic seam it already has: `start_authorization` / `authorization_status`
operations. A future IMAP-OAuth or other provider plugs into the same seam; core learns nothing
Google-specific. **Do this.**

**Build the authorization half as a web modal (over the user's OWN client).** Keep the default
plugin BYO-client. The user does the Console part once (give them deep-linked steps), drops the
client JSON into a web form on a "connect account" page, then clicks **Connect Gmail** → the
plugin opens the real Google consent popup, captures the `127.0.0.1` redirect, stores the refresh
token in the keyring, and writes the `[plugins.gmail]` block. From the second screen on it feels
like the slick "Sign in with Google" modal. Fits the web-config-editor model we're keeping.

**Validate and fail fast** at authorize time: warn on a Testing-mode app (token dies in ~7 days),
wrong client type, Gmail API not enabled, missing scope — and say exactly what to fix.

**The ceiling — do NOT re-litigate this.** The slick vendor "Sign in with Google" popup you see in
other apps is NOT a shortcut around the OAuth client; it IS a vendor-owned client the vendor
already registered and got verified. Moving the flow into the plugin changes the *code owner*, not
the *credential owner* — Google cares whose OAuth client it is, not which Python package holds the
secret. If the Oaken project ships a vendor client for `gmail.modify` (a RESTRICTED scope), it
takes on Google's OAuth verification: privacy policy, homepage, demo video, and likely the paid
annual CASA security assessment — plus the "unverified app" screen and a ~100-user cap until
verified, and a shared quota that breaks every user at once if suspended. That makes Oaken a broker
of every user's Gmail grant, which is backwards for a local-first tool. So:

- Default gmail plugin: **BYO-client**, user owns the OAuth client (no verification burden, honest
  local-first). The win is automating the *authorization half*, not removing client creation.
- Vendor-client "Sign in with Google" is a **plugin-level choice, not a core property**: because
  the plugin is a swappable artifact, a verified-vendor-client variant (a separate plugin or a
  flag) is possible later for a managed/hosted deployment that accepts the verification cost — a
  deliberate, bounded exception, never the base default.
- If vendor-client is ever seriously considered, re-check Google's current restricted-scope /
  CASA policy (it shifts); the shape (restricted scope → verification → assessment) has held for
  years but verify against current policy, not this note.

## Theme palette direction (DONE this session; full token set still FUTURE)
Applied this session via `--oaken-accent` in `_nav.html` (shared across core pages):

- **Light mode: white + green.** Oak green (`--oaken-oak`, #527e61) as the accent. (default)
- **Dark mode: brown + orange.** `--oaken-accent` flips to acorn orange (`--oaken-acorn-2`,
  #c6792e); the nav, active underline, hero box, tile icons, `.btn-oaken`, and version badge all
  follow it. (Oak above ground in the light; acorn and bark below, in the dark.)

Still FUTURE (branding pass): extend this into a **full design-token set** rather than accent-only,
as part of moving off plain Bootstrap toward a proper Oaken identity.
