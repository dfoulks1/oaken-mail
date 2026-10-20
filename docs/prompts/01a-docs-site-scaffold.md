# 01a — Docs site scaffold (GitHub Pages)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 01.

## Goal

A **generated** HTML documentation site for Oaken Mail, published to
`https://dfoulks1.github.io/oaken-mail/`, that looks like the Libra site
(`https://voltpop.github.io/Libra/`). It is also the project's website. Every later
prompt adds or updates pages through this tooling, so build the *mechanism* well now and
seed only a few pages.

## Reference (read-only; port the design, not the content)

Libra's source is at `/home/dfoulks/local/Libra/docs/` — read `index.html`, and
`tools/pagebase.py`, `tools/docnav.py`, `tools/build_index.py`, `tools/check_docs.py`.
Do **not** copy Libra content, hardware/wiring code (`wiring.py`, `softmap_data.py`,
`build_hardware.py`, …) or any Libra-specific text. Keep the SPDX header on all files.

## The look to reproduce

- **Framework:** Bootstrap **5.3.3** from jsDelivr, pinned, with **SRI** hashes
  (compute them from the pinned files at scaffold time; record how). This is the *only*
  external request a page makes. No analytics, no web fonts, no cookies.
- **Palette:** exactly two accent tokens, defined in one place (`pagebase.py`):
  `--accent: #1f4e79; --accent-soft: #e8f0f8;` and for dark
  `[data-bs-theme="dark"] { --accent: #7fb2e5; --accent-soft: #1b2733; }`. Re-theming
  later must be a two-line change.
- **Home page:** cover block (badge row: *Pre-alpha*, *Local-first*, *Open source*; theme
  toggle top-right; `display-5` title "Oaken Mail"; GitHub button row — a yellow
  "★ Star this repo on GitHub" primary, then outline buttons *View on GitHub*, *Report
  an issue*, *Apache-2.0 license*; a `lead` paragraph), then sections **The name**,
  **What it is for**, a **"Where things stand"** callout, and **The documents** — a
  responsive card grid (`.doc` cards: small uppercase kicker, accent title, one-line
  blurb, left accent border, hover tint) generated from the page registry.
  - Content comes from `docs/index.md`. **The name** is left as a clearly marked
    `TODO(user)` paragraph (the user will write it); "What it is for" and the lead are
    drafted from `00-context.md` in plain, public-facing language (no internal rules).
- **Content pages:** fixed left-hand pane (280 px) with the "OAKEN" brand, theme button,
  page subtitle, *Home*, grouped document list (current page highlighted), and this page's
  section links with **scroll-spy**; main column max ~1100 px; cover block (badges, `display-6`
  title, lead); every `##` heading becomes a **numbered section** (accent-filled number
  badge + heading, top border between sections); tables with accent-tinted header;
  callouts (left accent border); footer. Below 992 px the pane becomes a Bootstrap
  offcanvas menu behind a top bar button. Print stylesheet (no pane, no break inside
  tables/figures).
- **Theme:** light/dark via `data-bs-theme`; initial value from `prefers-color-scheme`,
  persisted in `localStorage` key `oaken-theme` (wrapped in try/catch — it must still
  render when storage is blocked). Code highlighting (Pygments) must have a light and a
  dark style scoped by `[data-bs-theme]`.
- **Extras:** copy button on code blocks; ```` ```mermaid ```` blocks rendered
  client-side with a pinned, SRI-protected mermaid script loaded **only on pages that
  contain one**, following the page theme, with a `<noscript>` fallback showing the source.
  Inline SVG favicon; `<meta name="description">` and Open Graph title/description; a
  skip-to-content link; visible focus rings; AA contrast in both themes.

## Tooling to build (`docs/tools/`, Python, deterministic output)

1. **Sources:** Markdown under `docs/` with **TOML front matter** fenced by `+++`
   (stdlib `tomllib`, no YAML dependency): `title`, `subtitle`, `lead`, `badges`
   (list of `[class, text]`), `group`, `order`, `description` (card blurb). Render with
   `markdown-it-py` (GFM-like preset: tables, strikethrough) + `mdit-py-plugins`
   (container fences `::: callout`), Pygments, Jinja2 (autoescape on for templates; the
   Markdown output is trusted repo content). Add a dependency group `docs` in
   `pyproject.toml`; the core runtime must not depend on it.
2. **Registry `docs/site.toml`:** site title, repo URL
   (`https://github.com/dfoulks1/oaken-mail`), site URL, branch, ordered nav groups
   (e.g. *Start*, *Concepts*, *Reference*, *Project*), the list of pages, optional
   `source` for pages that render repo-root files (`README`-style: `CONTRIBUTING.md`,
   `SECURITY.md`, `CODE_OF_CONDUCT.md`), and `exclude` globs. **`docs/prompts/**` and
   `docs/tools/**` are excluded by default** (the build prompts are development
   scaffolding; the user decides later whether to publish them — mention in the report).
3. **Generated pages:** a small plugin mechanism, `docs/tools/generated.py`, where a
   module registers `(page_id, builder)` and returns the same page structure as a Markdown
   page. Implement now: **ADR index** (from `docs/adr/*.md`, status + title + date) and
   **CLI/API/plugin/operations reference stubs that are *not listed* until their prompts
   exist** — i.e. do not create empty pages. Later prompts (04, 09, 11–13, 15–19) register
   their own generators.
4. **`build_site.py [--out site] [--serve] [--strict]`:** writes `site/` (git-ignored):
   `index.html`, pages, `assets/`, `404.html`, `sitemap.xml`, `robots.txt`, `.nojekyll`.
   **All internal links are relative** so it works under `/oaken-mail/`, from `file://`, and from
   `--serve` (binds `127.0.0.1` only). Footer shows version + git short SHA + commit date
   (or `dev`) — no wall-clock timestamps, so output is **byte-identical** across runs.
   `--strict` additionally fails on any `TODO(user)` left in site content (used for release).
   Support `repo:path/to/file` link syntax in Markdown that expands to a GitHub blob/tree
   URL **and fails the build if the path doesn't exist**.
5. **`check_docs.py`** (mirrors Libra's guard; exit 1 on any failure): build to a temp
   dir and fail on — Markdown file under `docs/` that is neither registered nor excluded
   (orphan), registered page with no source, front-matter errors, broken internal links and
   anchors, duplicate heading ids, heading-level skips, images without `alt`, any `http://`
   URL, any external host other than the allowlist (`cdn.jsdelivr.net`, plus
   `github.com` for outbound links), unbalanced HTML, and non-deterministic output (build
   twice, compare). Leave a documented hook where prompt 20 plugs in the "run the fenced
   `<!-- check -->` shell blocks" verifier (`docs/tools/check_commands.py`, not built here).
6. **Seed pages:** `index.md`; `architecture/overview.md` (public-facing summary of the
   architecture diagram and the two databases — not the internal prompt rules);
   `adr/` index (generated) with the two ADRs from prompt 01; `contributing` / `security` /
   `code-of-conduct` pages rendered from the root files.

## CI / deployment

- `.github/workflows/pages.yml`: on pull requests → run `check_docs.py` (build only, no
  deploy); on push to `main` → build, then `actions/configure-pages`,
  `actions/upload-pages-artifact`, `actions/deploy-pages` with the minimal
  `permissions` (`contents: read`, `pages: write`, `id-token: write`), a `pages`
  concurrency group, and actions **pinned by full commit SHA** (tag in a comment).
- This **deliberately differs from Libra**, which commits the generated HTML into
  `docs/`. Here generated output stays out of git. State this in the report and give the
  one-line change (`--out docs-out` + serve from a branch) if the user prefers Libra's way.
- **Manual step for the user (do not attempt it):** repository *Settings → Pages →
  Build and deployment → Source: GitHub Actions*. Put this in the report.
- Add `site/` to `.gitignore` (and its test from prompt 01 stays green).

## Acceptance criteria

- `uv run --group docs python docs/tools/build_site.py` produces a site whose home and
  one content page render correctly; `check_docs.py` passes; running it twice gives
  byte-identical output.
- Tests in `tests/docs/` over a **fixture content tree** (not the real docs) prove: nav
  and numbering, scroll-spy markup, theme attribute and `localStorage` guard present,
  SRI attributes on every CDN tag, mermaid script only on pages with a mermaid block,
  `repo:` link validation, and that `check_docs.py` **fails** on each seeded defect
  (orphan page, broken link, broken anchor, missing alt, external host, `http://`,
  heading skip, `TODO(user)` under `--strict`).
- No page makes a network request other than the pinned jsDelivr assets (assert by
  parsing every generated page).
- **Visual verification:** if a headless browser is available, take light, dark and
  390 px-wide screenshots of the home page and one content page into the scratchpad and
  describe how they compare with the Libra pages; if none is available, say plainly that
  the look was **not** visually verified and list the CSS values copied from Libra.
- `uv run ruff check`, `mypy` (tools included where practical), and `pytest` pass.

## Out of scope

Real content beyond the seed pages (later prompts add their own), a custom domain, search,
versioned docs, i18n, analytics.

## Checkpoint

Standard report. Include: the file tree of `docs/tools/`, the SRI computation method,
the exact manual GitHub step, the list of excluded paths, and anything about the Libra
look you chose **not** to reproduce and why.
