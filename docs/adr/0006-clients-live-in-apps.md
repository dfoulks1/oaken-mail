# 6. Clients live in apps/, each its own package

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0002 made the CLI and the web UI thin clients of one core daemon. The repository had them
split in practice (the web UI mockups in `apps/webui`, an empty `apps/cli`) but the first CLI
implementation was put inside the core package as `oaken.cli`. That let a client import core
internals by accident, put the client's dependencies (such as `rich`) on core's, and made the CLI
impossible to version or ship without core.

## Decision

- **Core** is the `oaken` package in `src/oaken`, plus the first-party plugins in `src/oaken_*`.
  It contains no client code.
- **Each client is an app** in `apps/<name>/`: a uv workspace member with its own `pyproject.toml`,
  its own `src/<package>/` and tests beside the code. The CLI is `apps/cli`, distribution
  `oaken-cli`, import package `oaken_cli`, and owns the `oaken` console script.
- **An app talks to core over the HTTP API** (ADR 0005). It depends on the `oaken` distribution
  only for what a client has to share: `oaken.__version__`, the path helpers that locate the runtime
  and token files, and (for in-process testing) the operation registry through `oaken_cli.inprocess`.
  That module is the one place the CLI imports core internals.
- The repository root is a uv workspace: `members = ["apps/cli"]`, with `oaken-cli` in the `dev`
  group so `uv sync` installs it and `uv run oaken` works from a checkout. Root `pytest`, `mypy`
  and `ruff` cover `apps/cli`; `uv run pytest apps/cli` runs only the CLI.
- The web UI follows the same shape when it becomes a served application (`apps/webui`).

## Consequences

- Core's wheel no longer ships a console script. Installing `oaken-cli` provides `oaken`.
- A change to the registry cannot break the CLI at import time, because its commands go through
  the API and the generic `operation` fallback. `inprocess` is the exception and is covered by tests.
- Documentation generators that describe a client (`gen_cli_reference.py`) import it by its own
  package name and need the workspace environment (`uv run`), as the other generators do.
