# 20 — Open-source documentation and release readiness

> **Status: built (0.1.1).** `README.md`, `ROADMAP.md`, `.github` templates, `CODEOWNERS` and
> `labels.md`, and `docs/tools/check_commands.py` exist; `docs/releasing.md` is the release
> checklist (the prompt called it `docs/release.md`). Still by hand: verify the Gmail walkthrough
> on a clean Google account, regenerate `docs/evals/latest.md` against a real model, and fill the
> `TODO(user)` contacts (`security`, `code-of-conduct`) that `build_site.py --strict` reports.

**Read first:** `docs/prompts/00-context.md`. **Depends on:** all previous.

## Goal

Make the repository understandable and contributable by a stranger, and **verify the
docs against reality** — every command in them must run.

## Deliverables

1. **`README.md`:** what Oaken Mail is and isn't, status (pre-alpha), a 60-second
   architecture picture, privacy stance (local-only by default; what leaves the machine
   and when), quick start (install with uv, create Google OAuth client, `oaken start`,
   `oaken auth gmail`, `oaken sync`, `oaken search`, connect MCP).
2. **`docs/architecture/`:** `overview.md` (components, data flow, the two databases and
   what is rebuildable), `ingest-pipeline.md` (stages, idempotency, failure policy),
   `read-state.md` (policy table and state machine), `retrieval.md` (hybrid search,
   context builder, token accounting, eval results), the HTTP API reference is **generated** (prompt 16), not hand-written.
3. **`docs/plugins.md`** expanded into a plugin author guide with a complete worked
   example (a toy provider) that is itself tested in CI via the contract suites.
4. **`docs/providers/gmail.md`** reviewed and verified step by step on a clean config.
5. **`docs/security/`** linked from README; `docs/mcp.md` linked; `docs/evals/latest.md`
   regenerated.
6. **ADRs** in `docs/adr/` for every significant decision made during the series
   (DB access layer, FTS5 external-content choice, vector store, HTTP framework,
   cron library, HTML converter, OAuth flow approach, CLI framework). Template + index.
7. **`ROADMAP.md`:** the deferred list from `docs/prompts/README.md` (UI, send, IMAP
   provider, importer, packaging, encryption at rest, plugin sandboxing, CPU embedding
   throughput), each with a short problem statement and open questions.
8. **GitHub metadata:** issue templates (bug, feature, plugin proposal), PR template
   (checklist: tests, types, docs, no real mail in fixtures), `CODEOWNERS` placeholder,
   labels list as a markdown file.
9. **Release checklist** (`docs/release.md`): version policy (0.x, API-version of
   plugin contracts), changelog process (`CHANGELOG.md`, Keep a Changelog), tag/publish
   steps (no publishing performed).

## Website (GitHub Pages — `https://dfoulks1.github.io/oaken-mail/`)

10. **Site content and polish.** The site tooling exists (01a) and pages were added along
    the way; now make it a coherent project website: finish the home page (lead, *What it
    is for*, *Where things stand* callout, document cards in a sensible order; leave **The
    name** as the user's `TODO(user)`), a *Start* path (install → connect Gmail → first
    sync → search → connect MCP), *Concepts*, *Reference*, *Project* (roadmap, release
    process, security, contributing, ADR index). Every page has a `lead` and
    `description`. Decide, and record in the report, whether `docs/prompts/` should be
    published (default: no).
11. **Strict build:** `build_site.py --strict` must pass **except** for the explicit
    `TODO(user)` items, which the report lists (name story, security contact). Verify the
    Pages workflow file with `actionlint` if available; confirm the deploy needs only the
    manual *Settings → Pages → Source: GitHub Actions* step.
12. **Look check against Libra** (light, dark, 390 px) with screenshots in the scratchpad if
    a headless browser is available; otherwise say it wasn't visually verified.

## Verification (required)

- A script `docs/tools/check_commands.py` (wired into `docs/tools/check_docs.py` at the hook
  left in 01a) extracts fenced `bash` blocks marked
  `<!-- check -->` from the docs and runs them in a temp `OAKEN_HOME` against the
  fakes where possible; CI runs it. Commands that need real Gmail/Ollama are marked
  `<!-- manual -->` and excluded.
- Link check (no dead relative links) and a spell/consistency pass on the project name.
- A fresh clone → `uv sync` → documented dev commands all pass on Linux in CI.
- `docs/tools/check_docs.py` passes (orphans, links, anchors, determinism, allowed hosts).
- Final audit: `git log --stat` and `git ls-files` contain no secrets, databases, real
  email, or references to the legacy tree's paths.

## Out of scope

Publishing to PyPI, a website, UI documentation, marketing.

## Checkpoint

Standard report. Include any doc claim you could not verify and why, plus a list of
things the user must do by hand before making the repo public (rotate legacy
credentials, fill `SECURITY.md` contact, choose a final name, create the GitHub repo).
