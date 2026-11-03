# ADR 0017 question record: separate triagers from enrichers

Owner: Drew Foulks. Date: 2026-11-03. Questions: 20 (owner's choice). Number: 0017 (0016 is proposed).

## Ruled already (cited, not asked)

- Triagers are not enrichers "at any level"; shared definitions go in `BasePlugin`; the two concepts
  are detangled. (owner, this session)
- This is "a small re-architecture, our first in oaken-mail": ADR and a hardened prompt before code.
- Levels are numerical labels the enrichers read; `BaseTriager` coordinates them (ADR 0016).

## Premises (checked 2026-11-03)

| Premise | Where |
| --- | --- |
| `BaseEnricher` and `BaseTriager` repeat `version`, `needs_llm`, `output_model`, `ask_intent`, `output_schema` | `src/oaken/plugins/base.py:438-487` |
| A triager's signature uses `EnrichInput` and `EnrichOutput`; so does the `Triager` protocol | `src/oaken/plugins/contracts.py:535-615` |
| `LLMTriager` takes `EnricherConfig` as its config model | `src/oaken_enrichers/_common.py` |
| All three triagers live in `oaken_enrichers` | `pyproject.toml` entry points |
| The verdict is stored as an enrichment named `triage` (`GATE`) | `src/oaken/ingest/pipeline.py:100` |
| Usage stage `llm.enrich.triage`; `enrich_message` runs triage | `src/oaken/api/app.py`, `builtin/enrichment.py` |
| `PLUGIN_API_VERSION` is 2; additive changes need no bump (ADR 0010) | `contracts.py:31` |

## Defaulted (veto any)

| Default | Why | Veto |
| --- | --- | --- |
| `version`, `needs_llm`, `output_model`, `ask_intent`, `output_schema` move to `BasePlugin` | identical in both bases | `veto:` |

## Round 1

1. Triager input: **shared neutral `CleanMessage`** both kinds read; the triager gets its own verdict
   type (`TriageVerdict`). Recommended, taken.
2. Package: **`oaken_triagers`**. Recommended, taken.
3. Stored verdict: **own table now** (not the recommended "defer"): a `triage_verdicts` table, a
   migration that moves existing rows, every reader re-pointed.
4. Order: **0017 first, then 0016.** Recommended, taken.

Round 2

5. Read op: **new `get_triage`; `get_enrichment` drops the verdict.** Recommended, taken.
6. Old rows: **moved** by the migration (no model calls). Recommended, taken.
7. Names: **kept**: `CleanMessage`, `TriageVerdict`, `triage_verdicts`, usage stage `llm.triage`.
8. `enrich_message`: **enrichers only**; triage runs through its own `triage_message`; `try_triage` stays.

Owner's note, for the record (mid-round): **the wizard belongs only to the rules triager.**
(Tension to settle: ADR 0016 decision 3 made `wizard` a generic declaration any plugin may carry.)

Premise found: triagers are new in 0.1.1 (`v0.1.0` has no `BaseTriager`; `PLUGIN_API_VERSION` was 1), so no
out-of-tree triager exists and the split needs no aliases or deprecation.

Defaulted (added, veto any): no aliases for the old names | one conformance suite per kind | the shared
LLM-access helper moves to the plugin layer, neutral | entry-point names stay, so `[plugins.triager.*]`
config is unchanged | migration is a new `0019` | one build prompt per ADR (0017, then 0016).

Round 3

9. Wizard scope: **rules triager only, no contract** (a `rules.wizard` read operation). Recommended, taken.
10. 0.1.1 includes: **0017 and 0016, both built.** Recommended, taken.
11. Anything else to protect: **nothing else.** Make: **ADR + build prompt.**

Owner's idea, mid-round (not decided): plugins extending the web UI, so one plugin extends everything.
Noted in ADR 0016's Revisit-when; needs its own ADR.

