# NN — Short title: what this builds

<!-- Copy this file to docs/prompts/TRACK_NN-name.md (CORE_, CLI_, MCP_, PLUGIN_, ENRICHER_,
DOCS_; NN is the next free number; a maintainer assigns it when the prompt is approved). Write for
someone, or an AI, who has read 00-context.md and nothing else. Plain words. Delete these comments. -->

**Read first:** `docs/prompts/00-context.md`, then the prompts and ADRs this builds on.
**Depends on:** the numbers of prompts that must be built first, or "nothing unbuilt".
**Track:** which track this is, and what it must not touch (a `PLUGIN_` prompt lists its core touch
points).

## Goal

What a person can do afterwards that they cannot do now, in a few sentences.

## Decisions already taken (do not re-litigate)

The choices that are made, and who made them. Anything not listed here is an open question.

## Not in scope

What a reader might assume is included and is not, and where it is planned instead.

## Requirements

Numbered, each one a thing that can be checked. Operations come first: a feature is a registry
operation before it is a command or a page, so the CLI and MCP stay in step.

## Acceptance criteria

Each one a test that passes or fails. Include the failure cases (what is refused, and with what
message), not only the happy path, and what must never appear in output, logs or the audit log.

## Open questions (defaults in bold)

Each with the answer used if nobody says otherwise.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
