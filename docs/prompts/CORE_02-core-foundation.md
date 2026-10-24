# 02 — Core foundation

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 01.

## Goal

The small, boring base everything else stands on: configuration, paths, logging,
errors, identifiers, an injectable clock, and an async-safe SQLite access layer.

## Requirements

Package layout under `src/oaken/core/`:

1. **`paths.py`** — `platformdirs`-based `data_dir()`, `config_dir()`, `runtime_file()`.
   All overridable by `OAKEN_HOME` (a single root, used by tests via `tmp_path`).
   Creating dirs sets mode `0700` where the OS supports it.
2. **`settings.py`** — one `Settings` model (pydantic v2 + `pydantic-settings`):
   precedence = defaults < `config.toml` in `config_dir()` < `OAKEN_*` env vars.
   No secrets in this file's defaults. Sections: `storage`, `ai` (backend names,
   model names, `smallest viable` defaults as strings only — no model is called here),
   `sync`, `api` (host fixed to `127.0.0.1`, port `0` = pick free), `log`.
   Unknown keys are an error. `Settings.load()` and a pure `Settings(...)` for tests.
3. **`logging.py`** — `structlog` JSON/console renderer, stderr only, a processor
   that **redacts** anything named like `token|secret|password|authorization` and
   truncates any field longer than 200 chars (mail content must not leak into logs).
4. **`errors.py`** — base `OakenError` and a small taxonomy: `ConfigError`,
   `StorageError`, `PluginError`, `ProviderError`, `AIBackendError`,
   `PermissionDenied`, `NotFound`, `Conflict`. Each carries a stable machine `code`.
5. **`ids.py`** — typed IDs (`NewType` or small frozen dataclasses): `AccountId`,
   `MessageId` (internal, ULID/UUIDv7 string), `ThreadId`, `JobId`. Provider-native IDs
   are opaque strings and never used as internal keys.
6. **`clock.py`** — `Clock` protocol with `SystemClock` and `FakeClock` for tests.
7. **`db.py`** — an async-safe SQLite wrapper (decide `aiosqlite` vs. a single
   dedicated worker thread; write ADR `0003`). Required: WAL mode, `foreign_keys=ON`,
   `busy_timeout`, `synchronous=NORMAL`, a `transaction()` async context manager,
   `execute/fetchone/fetchall`, ability to `ATTACH` a second DB read-only, and a
   `load_extension` hook (used by prompt 08) that fails gracefully when unsupported.
   Include a tiny forward-only **migration runner** (`migrations/NNNN_name.sql`,
   `schema_version` table, checksum verification, refuses to run a DB from a newer version).

## Acceptance criteria

- Unit tests (no network): settings precedence and unknown-key rejection; redaction
  and truncation in logs; migration runner applies in order, is idempotent, detects a
  tampered applied migration, and refuses a newer DB; concurrent readers during a
  write succeed under WAL; a failed transaction rolls back.
- `OAKEN_HOME=<tmp>` fully relocates every path (test asserts nothing is written
  outside it).
- `mypy --strict` clean; coverage of `core/` ≥ 90%.

## Out of scope

Any mail-specific schema (prompt 05), events/jobs (03), plugins (04).

## Checkpoint

Standard report. Include the ADR decision (aiosqlite vs. worker thread) and why.
