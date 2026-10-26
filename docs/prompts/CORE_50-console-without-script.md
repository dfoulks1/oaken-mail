# 50 — The console without script: one local stylesheet, palettes, and forms

**Read first:** `docs/prompts/00-context.md`, `CORE_16-api-daemon.md` (the console pages and their
guard), `CORE_23-plugin-settings.md`, `CORE_41-stats-controls.md`, `MCP_32-mcp-settings.md`, and
ADR 0013 (the console's rules: loopback, no token, counts and health only).
**Depends on:** 16, 23, 32, 41. Nothing unbuilt.
**Track:** CORE (Core's own console). Touches `src/oaken/api/templates/`,
`apps/core/oakencore-templates/`, the console routes in `src/oaken/api/app.py`, and the page CSP.
It does **not** touch the JSON API (`/v1/*`), the CLI, or Oaken-WebUI (a separate product).

## Why

The console loads Bootstrap's CSS and JS and its icon font from a CDN. That is a third-party request
from a local-first mail client, it needs a network for the console to look right, and it forces a
loose page security policy. The pages also lean on script for things HTML can do: menus, dialogs,
switches, toasts and every save. Each script is more surface to secure and more to break.

## Goal

The console is plain HTML and one local stylesheet. It works with script switched off, loads nothing
from another origin, and can be re-themed with a handful of named palettes.

## Decisions already taken (do not re-litigate)

- **No script in the pages.** The only permitted script is an optional live-refresh file
  (`refresh.js`) that re-reads `/stats.json`-style data and patches the numbers. With it blocked the
  page is still complete and correct; it just does not tick.
- **One stylesheet, `oaken.css`,** served by core from its own package data, replacing Bootstrap's CSS
  and the icon font. It is Oaken's design: an oak-and-acorn theme, not a Bootstrap clone.
- **Palettes are CSS custom properties,** in two axes: a palette (maple, birch, oak, acorn, moss,
  stone, sky, stream, contrast) and a mode (light or dark). A look is a named block of variables;
  components never hard-code a colour.
- **Every action is a form.** A save, a switch or a menu choice is an HTML `<form method="post">` that
  answers with a redirect back to the page (Post/Redirect/Get) and shows its result there. The JSON
  endpoints stay for the CLI and other clients; the console gets its own form handlers over the same
  operations, so the audit log and the rules are the same.
- **Declarative widgets.** Menus and dialogs use the HTML `popover` attribute and `<details>`; tabs
  stay the radio-input pattern; icons are an inline SVG sprite. Supported browsers are the current
  Chrome, Firefox and Safari releases.
- **Config is the source of truth.** The look is `[ui] palette` and `[ui] mode`, changed through the
  same settings operation as any other setting.
- **No external request, ever.** The page security policy drops every external source, and a test
  fails on any `http(s)://` in a served page or the stylesheet.

## Not in scope

A component library for other products, drag and drop, charts (a table or a plain bar made of
HTML and CSS is enough), per-user themes, and rewriting the JSON API.

## Requirements

1. **`oaken.css`** with a layout (page, nav, tabs, cards, tables), controls (buttons, inputs, selects,
   switches, badges), alerts and a toast, a menu and a dialog built on `popover`, and a small icon
   sprite. Served from `/oaken.css` with a long cache lifetime and a content hash in its URL.
2. **Palettes and modes.** Two separate choices. A **palette** is a hue family, and each has a
   **light** and a **dark** version: **Oak** (tan and brown, the default), **Maple** (autumn
   red-orange), **Birch** (warm white and ink), **Acorn** (chestnut and amber), **Moss** (green),
   **Stone** (neutral grey), **Sky** (blue), **Stream** (teal) and **Contrast** (black and white with
   strong outlines and focus rings, text at 7:1, WCAG AAA). That is 18 looks. `[ui] palette` takes a
   name or `auto` (Oak, or Contrast when the system sets `prefers-contrast: more`), and `[ui] mode`
   takes `light`, `dark` or `auto` (follows `prefers-color-scheme`, the default). The stylesheet also
   honours `forced-colors` (it yields to the system colours) and `prefers-reduced-motion`.
3. **Switching** is a form in the page footer with two selects (palette and mode) and a Set button:
   it sets `[ui] palette` and `[ui] mode` through the settings operation and reloads core's
   settings, then redirects back.
4. **Form handlers.** Each console control posts to a console route and redirects back. Messages
   appear on the next page from a short-lived, one-time server-side message store (an id in the
   redirect, no cookies), as a fixed toast that fades by itself with CSS and can be closed with a
   plain link.
5. **Every existing control is covered:** the stats page (clear usage, revoke an agent, the MCP
   operation switches and workspace picker, each model dropdown, the `ask` model, the plugin on and
   off switches, reload, and the settings dialog for a plugin), the config editor (load, test, save,
   reload), and the jobs page (pause, resume, limit, retry, cancel). A model dropdown is a select
   with a small Set button.
6. **CSRF.** Console form posts are accepted only from the console's own origin, and each form carries
   a token bound to the page and a server secret. A cross-origin post, or a missing or wrong token,
   is refused and changes nothing.
7. **Accessibility.** Every control works from the keyboard, focus is always visible, status is never
   colour alone (a badge says its state), headings are in order, and there is a skip link.
8. **The refresh script** is a single same-origin file with no dependencies, off when the page has no
   live data, and the only `<script>` in any console page.
9. **Retire Bootstrap.** Remove the CDN links, the old per-page scripts and the stylesheet overrides,
   and update the docs.

## Acceptance criteria

- With script blocked, every console action works end to end and ends on the same page showing its
  result.
- No served console page or the stylesheet contains an external URL; the page policy has no external
  source; the only `<script>` is `refresh.js`.
- Each of the 18 looks' text, link, badge and focus colours meets WCAG AA contrast against its
  surfaces (both Contrast looks meet AAA, 7:1), and a unit test computes it from the variable table.
- A form post from another origin, or without a valid token, is refused with no change and no audit
  row.
- Changing the palette or the mode writes `[ui]` and the next page renders in it; `auto` follows the
  system setting.
- The JSON API, the CLI and the MCP server are unchanged; their tests pass untouched.
- A page with a long table (the operation reference) stays usable at phone width.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- Q1: the two template sets (`src/oaken/api/templates/` and `apps/core/oakencore-templates/`):
  **one set, owned by core**, with the Oaken Core bundle reusing it.
- Q2: a model dropdown saves on change: **no, a visible Set button**, since saving on change needs
  script.
- Q3: the dialog for a plugin's settings: **a `popover` dialog on the same page**, with the form already
  rendered, so no extra page.
- Q4: how long a flash message lives: **60 seconds**, shown once.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
