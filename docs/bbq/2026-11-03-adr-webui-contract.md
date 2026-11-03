# ADR 0018 question record: a web UI contract for plugins

Owner: Drew Foulks. Date: 2026-11-03. Questions: 12 (owner's choice). Number: 0018 (0017 proposed).

## Ruled already (cited, not asked)

- "a webui contract that would allow you to extend the webui without writing a bunch of stuff to
  oaken-core? yes please."
- Scope the owner gave: "customizing settings modal, or adding a new wizard for your plugin."
- Data, not markup (my framing, accepted by "yes please"): a plugin declares, core renders.
- ADR 0013 (the console shows counts and health, never mail text; no script) and CORE_50 (forms, no script).

## Premises (checked 2026-11-03)

| Premise | Where |
| --- | --- |
| The settings modal is already data-driven: it renders a plugin's pydantic `config_model` JSON schema (type picks the input, `description` is the hint, the label is the field name) | `src/oaken/api/templates/_plugins.html:7-52` |
| Special cases are hard-coded in the template: `model`, `rules`, `skip_rules`, `steps` | `_plugins.html:260` |
| Plugins have no sections, labels, ordering, conditional fields or action buttons of their own | same |
| The CLI already builds flags from an operation's input schema | `apps/cli/src/oaken_cli/schema_flags.py` |
| `rules.wizard` (ADR 0016) is the first declaration of this shape | `docs/adr/0016-*.md` decision 3 |

## Round 1

1. Scope: **settings modal + wizard** (no pages or panels). Recommended, taken.
2. Declaration: **hints on the config model + an optional `ui()` method** returning plain data. Recommended, taken.
3. Actions: **only the plugin's own operations**; core routes, checks ownership, keeps permission and confirm. Recommended, taken.
4. Release: **after 0.1.1**; `rules.wizard` is promoted to this contract in the next release. Recommended, taken.

Round 2

5. Wizard shape: **one page, all fields, Test, Confirm.** Recommended, taken.
6. Field types: **what the schema has** (string, number, boolean, enum, list of strings). Recommended, taken.
7. Names: **kept**: `ui()`, `UiSpec`, `Field(json_schema_extra={"ui": ...})`.
8. Bad UI: **fall back to the plain form and log it**; never takes a plugin offline.

Owner's changes, mid-round:
- **Scope grows (amends Q1):** "you should be able to add an endpoint and serve a form or something
  locally, and customize what the modal offers." A plugin may serve its own endpoint, not only declare data.
- **Release (amends Q4):** "lets get this into 0.1.1 I'm in no rush and I've still got another thing I
  need to work on before I can really cut the release." In 0.1.1.

Round 3

9. Endpoint: **data, a form spec core renders**; no plugin HTML or script. Recommended, taken.
10. Reach: **link from the settings modal and the stage row**; no masthead entry. Recommended, taken.
11. Order: **0017, then 0018, then 0016.** Recommended, taken.
12. Make: **ADR + build prompt.** Nothing else to protect.

Defaulted (veto any): a plugin's text is escaped and length-capped; no links, HTML or CSS from a
plugin; one route family `/plugins/<name>/<page>`; additive within `PLUGIN_API_VERSION` 2; the
template's special cases stay for now.

