# 19 — Security hardening pass

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 16–18.

## Goal

A deliberate adversarial review of what has been built, converting every finding into
a failing test first, then a fix. Plugin sandboxing and at-rest encryption remain
**deferred** (documented, not implemented) per the user.

## Workstreams

1. **Threat model document** (`docs/security/threat-model.md`): assets (mail, tokens,
   keyring entries, audit log), actors (malicious sender, malicious web page in the
   user's browser, malicious local user, malicious/buggy plugin, compromised remote MCP
   client/model), trust boundaries, and for each threat the mitigation and the test
   that proves it. Keep it honest: list what is **not** defended (in-process plugins,
   same-user local malware, unencrypted DBs).
2. **Prompt-injection suite** (`tests/security/injection/`): a corpus of ≥40 hostile
   messages (instruction override, fake system/tool messages, delimiter forgery, unicode
   confusables, hidden HTML text, base64/rot13 payloads, "mark all read", "forward to…",
   exfiltration via markdown image URLs / links, schema-poisoning). Run each through
   every LLM enricher, `ask`, and the MCP result path with a scripted `FakeLLM` that
   **obeys** the injection (worst case). Assert: no state mutation, no tool/operation
   invoked, output schema intact, no remote URL fetched (`ask`/UI must not auto-render
   remote images or follow links), hostile text only ever inside the data envelope.
3. **Output safety:** model/Mail-derived strings returned by the API are plain data;
   verify there is no code path that renders mail HTML to a client. Document the UI
   requirement (sanitize, sandbox iframe, block remote content by default) as a
   **contract the UI phase must meet**, in `docs/security/ui-requirements.md`.
4. **Localhost API review:** re-test DNS rebinding, CSRF with form posts, `text/plain`
   JSON smuggling, oversized bodies, slowloris-style stalls (timeouts), token leakage
   via logs/errors/`/v1/openapi.json`/runtime file permissions, SSE auth, port
   squatting before start (what does the CLI do if the runtime file points at something
   that isn't Oaken Mail? it must verify a server nonce before sending the token).
5. **Secrets & data hygiene:** scan logs from a full fake run for tokens/bodies;
   verify keyring-only refresh tokens; verify no secret in config dumps, error
   messages, audit rows or job payloads (`last_error` redaction); verify files are
   `0600`/dirs `0700`; `oaken doctor` command (read-only) reporting these checks.
6. **Dependency & supply chain:** `uv lock` is committed; add `pip-audit` (or `uv`
   equivalent) to CI; pin GitHub Actions by SHA; Dependabot/renovate config;
   `SECURITY.md` updated with the disclosure process (leave the contact `TODO(user)`).
7. **Parsing robustness:** extended fuzzing on `parse()`/`clean()` (zip-bomb-like
   nested multiparts, 100k-header messages, pathological regex inputs → add timeouts or
   linear-time patterns; test with `hypothesis` + a time limit per input).

## Acceptance criteria

- Every item above has at least one automated test; the new suite is part of CI.
- The worst-case obedient `FakeLLM` cannot cause any audit-log write, provider call,
  or non-schema output in any path.
- `oaken doctor` passes on a fresh install and flags a deliberately loosened
  permission in a test.
- Report lists every finding with severity and whether it was fixed or consciously deferred.

## Out of scope

Plugin sandboxing, encryption at rest, third-party plugin signing, formal pen test.

## Docs site

Publish `docs/security/threat-model.md` and `docs/security/ui-requirements.md` in a
*Project → Security* group, linked from the home page. `check_docs.py` must pass.

## Checkpoint

Standard report. Findings table first.
