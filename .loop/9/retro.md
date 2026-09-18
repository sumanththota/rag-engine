## Outcome: merged · verifier-rejections: 2 · verify rounds: 3 (at cap)

Iterations-to-green: UNRELIABLE — the first journal entry consolidated the
whole initial implementation into one entry, so the entry count does not
reflect the real number of implementer passes. Not reporting a number rather
than reporting a wrong one.

Wall-clock and tokens-per-feature: not captured. Nothing in this run measured
either; per the manual, only iteration count and verify-round count are
actually enforced/counted.

## What made this hard

- Criterion 6 ("missing SECRET_KEY raises ConfigError at boot") had a side
  effect the implementer didn't own: `tests/test_config.py` — outside
  app/auth.py, app/config.py, and main.py's auth region — started failing
  because its .env fixture never set SECRET_KEY. `verifier_command`
  (`pytest -q tests/test_auth.py`) was green the whole time; only a full-suite
  run caught it. Fixed by adding SECRET_KEY to that test's fixture, with the
  out-of-region edit explicitly approved rather than made unilaterally.
- The dedicated tamper test for cookie verification
  (`test_verify_session_cookie_rejects_tampered_value`) mutated only the
  token's last base64url character. itsdangerous base64url-encodes the raw
  HMAC digest, and the last character of a base64 group can carry slack bits
  that don't affect the decoded bytes — flipping only that character produced
  an unchanged signature on roughly 1 run in 4, so a single passing run proved
  nothing. `verifier_command` run 10 times back to back exposed it as
  intermittent rather than green. Fixed by XOR-ing a real bit in the decoded
  signature instead of mutating an encoded character, which is guaranteed
  invalid on every run, and asserting `tampered != token` so a future no-op
  mutation fails loudly instead of silently passing.
- Both findings were criteria the implementer's own green suite reported as
  passing. Neither would have been caught by `verifier_command` run once.

## Learnings -> promote

- [ ] -> CONVENTIONS.md §7: run `verifier_command` 3+ times back to back with
      nothing else changed; a criterion that passes some runs and fails
      others is a NEEDS_WORK finding, not a pass.
- [ ] -> CONVENTIONS.md §7: "the full suite is green" is a standing
      acceptance criterion on every feature — `verifier_command` is narrower
      than the blast radius of any real change, and a scoped command cannot
      evidence an unscoped claim.
- [ ] -> CONVENTIONS.md §7: before gating, fast-forward `verify/<id>-*` to
      `impl/<id>-*` and confirm the diff is empty — a lagging verify branch
      gates stale code and can burn a whole round re-finding something
      already fixed.
- [ ] -> CONVENTIONS.md: `PYTHONPATH=.` is required to run the suite until
      `pyproject.toml` has a `[build-system]` table.

None of these are checked off — promotion into CONVENTIONS.md itself hasn't
happened yet, this just records the candidates.

## Superseded

None. This is the first retro in the index.
