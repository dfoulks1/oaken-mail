# Summaries only for long human mail

Goal: summarise what you cannot plainly read; skip cron and other machine mail; show where a thread got hot.

1. A filter labels long mail (the filters triager's folder, a `*.filters` file). `lines:>20` counts the sender's own
   lines, not blank or quoted ones:

       lines:>20 -> label long

2. The summary enricher runs only on that label, and never on a bulk one (`when` all must hold, `unless` none may;
   a bare name is a label that is set or a tag in `sieve.labels` / `filters.labels`, `key=value` is exact):

       [plugins.enricher.summary]
       when = ["long"]
       unless = ["list"]

3. Turn on `enricher_sentiment` (no model; skips machines). Each message gets `sentiment.score`; the thread bar shows
   a face per message (0 to 1 outlined) and a click opens that message. "Enrich thread" then asks for positions,
   where it turned sharp, decisions, open items and what is asked of you.

`when`/`unless` need the oaken-core change in `stagerun.py` (`selected`), unreleased. The score is a pointer, not a verdict.
