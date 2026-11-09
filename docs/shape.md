# The shape of oaken-mail

Decided 2026-11-09, in conversation with the owner. Companion to `ui-needs-from-core.md` (what core and the plugins must grow).

## What oaken-mail is

An opinionated slice of Oaken that does mail well. It is guardrails and limited functionality, for the sake of a guarantee: what
it offers, it does reliably. The browser UI is the product. The core console ships turned off by default, and the UI manages
what a mail user needs of it.

The console is the engineer's tool. The UI is the user's. Anything a mail user cannot change is hidden, not greyed out.

## The pipeline is locked

Intake, Triage, Enrich, Act. Four stages, in that order. Users may adjust a stage's queue and its plugins within it. They may
not add a stage, remove one, reorder them or build a custom pipeline.

- **Intake** is core's default queue. Its id stays `default` (renaming it is a core release with a data migration); its label is
  "Intake", shown on the queue card as "Intake" with a muted "(default)" after it. The Intake page also covers the **store** step:
  the ingestor that keeps what the formatters and triagers let through.
- A triager runs in the stage its `stage` setting names, else the first triager queue (`oaken_core/daemon/core.py`). With no
  triager queue defined, a triager falls through into Intake. oaken-mail therefore ships real Triage, Enrich and Act queues and
  pins each default plugin to a stage, so placement is deliberate and not an accident of an empty list.
- A triager may run in Intake (before the store, for cheap early decisions such as spam) or in Triage. Enrichers run in Enrich and
  actions in Act. The UI offers "runs in" only where a plugin kind makes sense.
- A triager that judges by opinion (for example a model asked "is this a job request") labels. It does not reject: a reject means
  the message is never stored. A confidential account never reaches a hosted model. No such triager exists in `oaken-plugins` yet.

## A user cannot brick the system

The guard lives in core, not in the UI, because the CLI and API would otherwise bypass it.

- Each stage keeps what it needs to function. A change that would leave a stage without its required piece (the last provider,
  ingestor or formatter, the only triager doing a stage's job) is refused with a reason a person can read.
- A plugin that fails to start is refused or rolled back, not left half loaded.
- **Users cannot load arbitrary plugins.** They choose from a catalog that oaken-mail ships and versions with each release. In the
  UI, "add" means making an instance of something in the catalog (a Gmail, IMAP or SMTP account, a sieve script) and on or off for
  the rest. Core enforces the catalog, so the console cannot get around it.
- The catalog is **generated**, not hand-kept, from an `oaken-plugins` directory or its PyPI package: stage, version, config
  schema, setup help. Plugins already declare most of it.
- **Open (an ecosystem question, not oaken-mail's alone):** what marks a plugin as OK? For now, an allowlist in oaken-mail pinning
  names to the versions we tested. It should be replaceable by whatever the Oaken ecosystem decides.

## Queues

On each stage's page, a queue card offers: pause and resume, and concurrency (workers). Nothing else is exposed; the rest is fixed
to tested values.

- **Retry one message** is available wherever that message is rendered: the inbox row and the reading pane.
- **Requeue all failures** is a tab inside the stage's page: findable, not prominent, far less than the inbox.

## Stage badges on a message

Every message shows three badges with counts and tooltips:

| Stage | Icon | Count |
|---|---|---|
| Triage | Band-Aid | triagers' results on this message |
| Enrich | twinkle | enrichments on this message |
| Act | clapboard | actions taken for this message |

For example: twinkle (3), Band-Aid (1), clapboard (0), where a zero may simply not show. Clicking a badge lists the triagers,
enrichers or actions the message is configured or labelled for, and any of them can be re-run from there. A failed stage tints
its badge and offers retry.

**Open:** whether a count means results held or everything configured (including not yet run or failed); whether a zero hides.

## The masthead

Far right, five icons, in this order:

1. **Intake & Store** (database, green)
2. **Triage** (Band-Aid)
3. **Enrich** (twinkle)
4. **Act** (clapboard)
5. **Settings** (gear)

Each stage icon carries a red pip when something in that stage needs attention. A click lands on the stage's failures with
requeue in reach. Colors will be tuned later.

Removed: the pipeline strip (a concept a mail user does not need; the icons are the navigation), the theme tree (it moves into
Settings), the keyboard icon (shortcuts move into Settings), the notification bell, and the avatar with its menu. The avatar
returns only when an auth plugin is loaded; today the UI is a loopback.

## Stage pages

Each stage page, and the Settings page, is in the console's style: foldable sections, pill toggles for booleans, cards. A stage
page holds the queue card, the stage's plugins on or off with per-instance settings, a recent-failures list, and a "requeue
failures" tab. Providers is a tab of its own (the mail accounts). Triage and Enrich take the same form as Intake. Act sits on a
**Schedulers** page with schedules, because an action and a schedule for it are one idea. All of it uses the console's theme
(`oaken_core/console/static/oaken.css`), so palettes carry over. The theme is meant to become shared across the Oaken ecosystem;
where the shared layer lives is open (in `oaken-core`, or its own small package so products outside core can use it).

Settings holds: theme (palette and mode), keyboard shortcuts.

## Still open from `ui-needs-from-core.md` section 5

Nothing there has been decided yet: 5.1 (whose vocabulary), 5.2 (where state lives), 5.3 (which provider owns a message), 5.4
(what an action is). The retry, requeue and re-run actions above are the first that need an answer.
