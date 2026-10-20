# Contributing to Oaken Mail

Pull requests are welcome. There is no DCO or CLA yet.

## Development setup

You need Python 3.11 or newer and uv.

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

## Documentation

The project website is generated from `docs/` and published to GitHub Pages. If you change
user-visible behaviour, add or change a page, then check it before committing:

```bash
uv run --group docs python docs/tools/check_docs.py      # build + link check
uv run --group docs python docs/tools/build_site.py --serve   # preview on 127.0.0.1:8000
```

Register new pages in `docs/site.toml`. Keep the software map current: edit `docs/softmap.toml` in the same change that builds or alters a component (the build fails if it disagrees with the repository). Generated output goes to `site/` and is not
committed.
