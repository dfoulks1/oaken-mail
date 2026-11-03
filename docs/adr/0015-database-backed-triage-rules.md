# 15. The reader's triage rules: `config.toml` is trusted, the database is the last known good

- Status: accepted (ratified 2026-11-03 by the owner, who verified the built implementation for 0.2.0)
- Date: 2026-11-03
- Amends: `docs/prompts/CORE_51-triage-labels-and-automations.md`, Q2 (where rules live: both places,
  the file wins)
- Built: 2026-11-03: the file section and writer, the working copy and snapshot in the database,
  the `rules` triager, staging with `apply` and `discard`, drift with `ack` and `revert`, a dry run
  (`triage_rule_test`), the `oaken rule` wizard and `oaken level`, and, on the console, the rules in the
  `rules` plugin's settings dialog plus a new-rule page that works with that plugin only (CORE_54). Written and tested, not committed. Not built: prompt overrides, labels and
  automations (see Not built).

## Context

Triage rates mail 0 to 3 and names a category. The only deterministic rules were the built-in
header checks (bulk, no-reply, cron, auto-reply, spam, empty), which a reader could switch off by
name but not extend. A reader needs their own rules, and a level of their own (a "5" for mail from
people who matter) that the checks cannot override. CORE_51 (a draft) planned user rules but left
their home as `[[triage.rules]]` in `config.toml`, "not a UI yet" (its Q2), and said a change to the
plugin contract needs an ADR first.

Premises, each checked on 2026-11-03:

- Triagers run in `rank` order and the first verdict wins (`Pipeline._triagers`); the built-in
  checks are rank 10, the model rank 90.
- A plugin gets no database handle (ADR 0012). Core hands it its config.
- `plugin_reload` restarts one plugin without a core reload (`operations/builtin/system.py`).
- `PLUGIN_API_VERSION` is 2 and the plugin contract snapshot test passed with these changes.
- The highest ADR is 0014; no other ADR rules on triage.

## Decision

1. **The reader's levels and rules live in both `config.toml` and core's database, and
   `config.toml` is the source of truth.** The file holds `[[triage.levels]]` and `[[triage.rules]]`
   (CORE_51 Q2's form); array order is precedence. The database (migration `0018_triage_rules.sql`)
   holds three things a file cannot: each level's and rule's hidden `uid` (so a level can be
   renumbered and a rule moved without losing what points at it); the **working copy** the CLI and
   console edit; and the **snapshot**, the last copy that was written to or accepted from the file,
   which is what runs ("last known good"). In the file, a rule is joined to its row by name.
   *Cited:* "rules should be in both spots. the config.toml is a source of truth." Earlier:
   "edit, save (write to db), reload (load from db), precedence, numbers should get uids that
   preserve over but aren't shown. we should be able to re-order them from the ui." And: a wizard
   makes the rule, it is TOML, the database is the "last known good" and `config.toml` is "ultimate
   trust, with a verification step" (decision 8).
2a. **Edits are staged; a reload writes the batch.** An add, change, move or removal in the CLI or
   console changes the working copy only: nothing is written to the file and nothing running
   changes. `oaken rule apply`, or any `oaken reload`, validates the working copy, writes it to
   `config.toml` once (one backup, whatever the number of edits), makes it the snapshot and reloads
   the `rules` plugin. `oaken rule discard` throws the staged edits away. `oaken rule` says how many
   are staged.
   *Cited:* "when you make changes in the UI (add a rule or whatever) you update the config
   database, then a reload would write the whole batch to disk. you'd get one backup then."
2. **A new triager plugin, `rules`, runs at rank 5, before the built-in checks (rank 10).** The
   first matching rule settles triage with no model, so a reader's rule overrides a spam, bulk or
   no-reply designation. Mail no rule matches falls through as before.
   *Cited:* "If detected spam matches my custom rules, I can override its spam designation?"
3. **Levels are numbers 0 to 99.** 0 to 3 are built in and cannot be renumbered or deleted; a
   reader adds 4 and up. The model still answers 0 to 3 only. `priority` in the stored triage
   result and every `min_priority` bar accept 0 to 99. A level gives a queued job the urgency of
   level 3 at most (`max(0, 3 - priority)`).
   *Cited:* "I want to be able to add a new triage level (something other than 1-4, lets call it 5)."
4. **A rule is a closed set of conditions, all of which must match:** `from`, `to`, `subject`,
   `subject_contains`, `body_contains`, `header_present`, `has_attachment`, `is_reply`; plus the
   level and category it gives. Text and glob matching ignore case. It is written as TOML-shaped
   data and made with a CLI wizard. No sieve and no Python in the base.
   *Cited:* "the config is already in toml perhaps we can just make a rule builder wizard for this
   and let it be toml." (The owner also said: "I'm still not sold on either sieve or python.")
5. **Core gives the saved rules to the `rules` plugin as its config** at load and reload
   (`PluginHost.runtime_config`, merged over `[plugins]`), and applies a change by reloading that
   one plugin. A rule is checked against the plugin's config model before it is kept; one the
   plugin refuses is removed again. This follows CORE_51 Q5.
   *Cited:* "we already have a parser and the plugin level reload commands. this shouldn't be a
   heavy lift."
6. **The built-in checks are renamed `spamcheck`** (`SpamCheckTriager`; `[plugins.triager.spamcheck]`
   holds `skip_rules` and the rest). The name `rules` goes to the reader's set. A leftover
   `[plugins.triager.rules]` with the old keys is reported by the plugin's strict config, not
   ignored and not aliased.
   *Cited:* "plugins.triagers.spamcheck or spamkit is cleaner than spamdetection. lets rename those
   this round."
7. **Core operations, none over MCP:** the eight below, plus `triage_rule_test` (a dry run that
   returns counts and ids, never mail text), `triage_apply`, `triage_discard`, `triage_drift`,
   `triage_ack` and `triage_revert` (decisions 2a and 8): `triage_levels`, `triage_level_set`,
   `triage_level_delete`, `triage_rules`, `triage_rule_set`, `triage_rule_delete`,
   `triage_rule_move`, `triage_rule_order` (the whole order at once, for a drag in a UI). The CLI
   is `oaken rule` and `oaken level`; `oaken rule add NAME` asks for what is missing when there is
   a terminal. The operation schemas are in the ADR 0010 snapshot.

8. **Drift is surfaced and acknowledged; until then the last acknowledged rules run.** At start-up
   and at every `config_reload`, core hashes the file's `triage` section and compares it with the
   snapshot (the last copy written to or accepted from the file). If
   they match, nothing happens. If they differ ("this config has changed"):
   - Core logs it (at info: running the last known good copy is the normal safe outcome, not a
     fault) and shows it in `oaken status`, `oaken rule` and the
     console as **needs acknowledgement**, with a diff (added, removed, changed, reordered).
   - Core **does not load the modified rules**. The `rules` triager runs the last acknowledged copy
     from the database, so mail is rated as the reader last agreed, not by an edit nobody has
     looked at. Nothing else is affected.
   - The reader **acknowledges** (`oaken rule ack`, or the console button): the file's section
     becomes the snapshot and the working copy, new uids are made for new rules, and the plugin
     reloads. It is refused while edits are staged (apply or discard them first). Or **reverts**
     (`oaken rule revert`, or the console): the snapshot is written back over the file's section.
     `apply` is refused while the file differs from the snapshot, so a staged batch never writes
     over a hand edit. The console can show the snapshot beside the file's.
   - First boot is not drift: with no acknowledged copy, the file's rules are adopted at once.
   - A file that does not validate is also drift: the acknowledged copy keeps running and the error
     is shown with the diff.
   *Cited:* "if that gets updated and the database differs, the user should be made to acknowledge
   that at start up time... perhaps a boot into safe mode, not loading the part that was modified?
   I'm not sure... optionally if you're connected to the database and have the old config, you
   should have the option of loading it and looking at the config through the ui." The owner was
   unsure of the mechanism; "run the last acknowledged copy" is the recommendation, for ratification.

## Alternatives

- **Rules in `config.toml` only (CORE_51 Q2 as written).** Overruled: the owner wants hidden uids,
  reordering from a UI and a copy to fall back on, which a file alone does badly.
- **The database only.** The first draft of this ADR. Overruled by the owner: the file is the source
  of truth, and a rule set no one can read or version in a file is not.
- **On drift, load the file's rules at once, or load none.** Loading the edit unseen puts an
  unreviewed rule in charge of mail; loading none can let mail a reader marked important be
  skipped as spam. Neither chosen.
- **Sieve test expressions.** Safe, standard and the interpreter exists, but it cannot see
  attachments and has no notion of a level. Not chosen; left as a later alternative.
- **A Python function per rule.** Most power, but arbitrary code in the daemon that holds all the
  mail, with no sandbox. Not in the base; if added, off by default.
- **A one-line search syntax** (`from:*@boss.com subject:urgent -> 5 work`). Cheap to build on the
  search tokenizer; set aside for the wizard and TOML-shaped data.
- **Plain English compiled once by the local model.** Easiest to use; later, as a way to draft a
  rule the reader then checks.

## Consequences

- A migration, a store (`oaken.core.triage_store`), a host hook and eight operations are added to
  core. The operation schema snapshot grew by about 700 lines.
- Renaming the built-in set changes the config key. No tracked config uses `[plugins.triager.rules]`
  (the live one does not); a CHANGELOG entry is owed. Entry points change, so an editable install
  needs a refresh, and a running core needs a restart for the migration and the new plugin.
- A matching rule is a full verdict: `needs_reply` is false and the model is not asked. There is no
  way yet to say "not spam, let the model rate it".
- A batch of edits costs one write and one backup of `config.toml`, not one per edit.
- Rules are in version control, through the file. An unacknowledged edit changes nothing until it
  is acknowledged, so a boot after a hand edit is quiet by design but never silent: the status
  and the console say it.
- A rename in the file looks like a removal and an addition (the join is by name), so the rule gets
  a new uid; the diff says so. A rename through the CLI or console keeps the uid.
- The hook is internal to the host; it adds nothing to the plugin contract.

**Revisit when:** the owner chooses sieve, Python or a one-line syntax as the language for rules.

## Not built

- Dragging rules to reorder in the console (it needs script; up and down buttons do the same).
- Prompt overrides and `oaken prompts` (the owner's last request in the session).
- Labels a rule applies, and rules that launch automations (CORE_51 goals 2, 3 and 6), and the
  triage dry run over MCP (CORE_51 goal 4).
- Tests for the `triage_gate` setting (still open in `TODO.md`).

## History

- 2026-11-03: drafted from the owner's words in a working session. Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner, who verified the built implementation works for 0.2.0.
- 2026-11-03 (same session, before ratification): the owner ruled `config.toml` is a source of truth
  and the rules live in both places; drift must be acknowledged. Decision 1 rewritten, decision 8
  added, the database-only draft moved to Alternatives. The first draft said "The reader's levels
  and rules live in core's database ... not in `config.toml`."
- 2026-11-03 (same session, before ratification): the owner ruled that edits are staged in the
  database and a reload writes the batch to the file once, so there is one backup, not one per
  edit. Decision 2a added; decisions 1 and 8 reworded around a working copy and a snapshot. The
  previous draft wrote the file on every edit.
- 2026-11-03 (same session, before ratification): the owner ruled the rule and level operations and
  the wizard belong to the `rules` plugin, not core. ADR 0016 (proposed) moves them; decisions 1,
  2a and 8 stand, and only where they run changes.
