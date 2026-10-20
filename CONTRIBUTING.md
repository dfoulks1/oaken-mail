# Contributing to Oaken Mail

Pull requests are welcome. There is no DCO or CLA yet.

## Development setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

Install the git hooks (lint plus secret scan) with `uv run pre-commit install` if you use
pre-commit, or run `uv run detect-secrets-hook --baseline .secrets.baseline <files>`.

## Ground rules

- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.
- Tests never use the network, Ollama, Gmail, Docker or real mail. Use synthetic fixtures.
- Never commit real email content, addresses, databases or tokens.
- Use Conventional Commits for commit messages.
