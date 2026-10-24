## What and why

<!-- Which prompt(s) does this run? e.g. CORE_28-compose-and-send. A small fix needs none. -->

**Prompts covered:**

## Checklist

- [ ] On a feature branch named for the prompt (`feat/28-compose-and-send`), one prompt per pull request where you can
- [ ] Tests added or updated, next to the code, with no network
- [ ] `uv run ruff check`, `ruff format --check`, `mypy`, `lint-imports` and `pytest` pass
- [ ] Docs updated; `uv run --group docs python docs/tools/check_docs.py` passes
- [ ] For **each** prompt run: a status block at the top of its file (what was built, where the code differs) and its row in the status table in `CONTRIBUTING.md`; the report-back is pasted below
- [ ] Every other doc that mentions what changed is updated (changelog, architecture and reference pages, `docs/softmap.toml`, `config.example.toml`)
- [ ] A moved or renamed doc went through `docs/tools/move_doc.py`, so its references are fixed
- [ ] A prompt I changed is reviewed like code: it does not tell an AI to fetch from the network, run unreviewed scripts, or skip a check
- [ ] No real mail, tokens or personal addresses in fixtures or logs
- [ ] A change to an operation or plugin contract regenerated its snapshot and says why (ADR 0010)

## Report-back
