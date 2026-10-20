# 01 — Bootstrap the repository

**Read first:** `docs/prompts/00-context.md`.

## Goal

A clean, public-ready, empty-but-green project: licensing, tooling, CI and
secret-scanning in place, with **no code or data carried over** from the legacy tree.

## Steps

1. **Project skeleton** in the repository root (your `oaken-mail` checkout):
   - `git init` (default branch `main`); no remotes.
   - `uv init --package` style layout: `src/oaken/__init__.py` (version string only),
     `tests/`, `pyproject.toml` (name `oaken`, `requires-python >=3.11`,
     hatchling build, console script `oaken = "oaken.cli:main"` stubbed to print a version).
   - Dev group: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `ruff`, `mypy`, `coverage`.
   - `ruff` line length 100; rules `E,F,I,UP,B,ASYNC,S` (allow `S101` in tests); `mypy --strict`.
2. **Legal/community files:** `LICENSE` (Apache-2.0 full text), `NOTICE`
   (project name + year + "The Oaken Mail contributors"), `CONTRIBUTING.md`
   (PRs welcome; how to run lint/type/tests; no DCO/CLA yet), `CODE_OF_CONDUCT.md`
   (Contributor Covenant 2.1), `SECURITY.md` (private reporting instructions; a
   placeholder contact the user must fill in — leave an obvious `TODO(user)`).
   Add SPDX headers (`# SPDX-License-Identifier: Apache-2.0`) to every Python file;
   add a ruff or pre-commit check that enforces them.
3. **`.gitignore`:** ignore `*.db`, `*.db-*`, `*.sqlite*`, `*.eml`, `*.mbox`, `.env*`
   (except `.env.example`), `token*.json`, `credentials*.json`, `client_secret*.json`,
   `.venv/`, caches, `dist/`, `node_modules/`. Add a test that fails if any such
   pattern is *tracked* (`git ls-files`).
4. **Secret scanning:** pre-commit config with `ruff`, `mypy` (optional), and a secret
   scanner (`gitleaks` or `detect-secrets`; pick one that installs via pip/uv, record why).
   Also add the same scan to CI.
5. **CI:** `.github/workflows/ci.yml` running on Linux, macOS and Windows with the
   Python version matrix 3.11 and 3.12: `uv sync`, `ruff check`, `ruff format --check`,
   `mypy`, `pytest`. Also a job for the secret scan. Pin action versions by tag.
6. **Docs stubs:** `README.md` (what it is, status: pre-alpha, no setup yet),
   `docs/adr/0001-record-architecture-decisions.md` and
   `docs/adr/0002-core-daemon-and-thin-clients.md` summarising the architecture in
   `00-context.md` (do not duplicate the whole file; reference it).
7. **Legacy-data safeguard (needs the user's confirmation before running):**
   print and ask before executing these, then verify with checksums:
   ```bash
   install -d -m 700 ~/.local/share/mailmind-legacy
   cp -p /home/dfoulks/local/OllMCP/Memories/mailmind.db ~/.local/share/mailmind-legacy/mailmind.legacy.db
   chmod 600 ~/.local/share/mailmind-legacy/mailmind.legacy.db
   sha256sum <original> <copy>
   ```
   Do **not** open, query, or move the original. Do **not** touch any other legacy file.
   Record in the report that the user must rotate the OAuth client secret and token
   found in the legacy `.env`/`token.json` before any public push of anything.
   (You must not read those files; just state the recommendation.)

## Acceptance criteria

- `uv sync && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` all pass.
- `uv run oaken --version` prints the version.
- `git ls-files` contains no database, mail, token or `.env` files; the tracked-files test passes.
- Every `.py` file has the SPDX header and the check fails if one is missing.
- One initial commit (plus follow-ups if needed) with a clean tree.

## Out of scope

Any application code beyond the version stub; any dependency not listed above.

## Docs site hook

Add `site/` to `.gitignore` and a `docs` dependency group placeholder in `pyproject.toml`
(populated in prompt 01a). Prompt 01a builds the site tooling.

## Checkpoint

Use the report format from `00-context.md`. Include the exact scanner chosen and why.
