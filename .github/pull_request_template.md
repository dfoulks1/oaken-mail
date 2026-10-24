## What and why

## Checklist

- [ ] Tests added or updated, next to the code, with no network
- [ ] `uv run ruff check`, `ruff format --check`, `mypy`, `lint-imports` and `pytest` pass
- [ ] Docs updated; `uv run --group docs python docs/tools/check_docs.py` passes
- [ ] The prompt status table in `CONTRIBUTING.md` is updated if a prompt was built
- [ ] No real mail, tokens or personal addresses in fixtures or logs
- [ ] A change to an operation or plugin contract regenerated its snapshot and says why (ADR 0010)
