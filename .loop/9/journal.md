# Ticket 9 journal — password auth (impl/9-password-auth)

## Iteration 1 — 2026-09-18

### What I did
- `app/config.py`: added `Settings.app_env` (`APP_ENV`, default `"production"`)
  and `Settings.secret_key` (`SECRET_KEY`, required, validated non-empty).
  Missing `SECRET_KEY` now raises `ConfigError` via `load_config()`, same
  path as `HANDBOOK_PATH`/`DATABASE_URL`.
- New module `app/auth.py` (own `AuthError`, own `ensure_schema()`, same
  shape as `PostgresStore`/`TraceStore`):
  - `AuthStore` — Postgres-backed `users` + `auth_identities` tables (split
    per ADR-0003), `create_user_with_password`, `authenticate_password`,
    `get_user`.
  - `hash_password`/`verify_password` — argon2id via `argon2-cffi`
    (`PasswordHasher(type=argon2.Type.ID)`).
  - `sign_session_cookie`/`verify_session_cookie` — itsdangerous
    `URLSafeTimedSerializer`, sliding 30-day idle expiry
    (`_SESSION_MAX_AGE_SECONDS`). `verify_session_cookie` never raises:
    `BadSignature`/expiry both resolve to `None`.
  - `set_session_cookie`/`clear_session_cookie` — HttpOnly, SameSite=Lax,
    Secure unless `APP_ENV=development`.
  - `make_get_current_user_optional(auth_store, secret_key)` — builds the
    `Depends(...)` dependency; missing cookie, invalid/expired cookie, or a
    DB failure on `get_user()` (`AuthError`) all resolve to `None`, never
    raise.
- `app/main.py` (own region, new `# ---- auth (ticket 9) ----` block,
  mirroring the existing `# ---- traces review (ticket 4) ----` pattern):
  - `create_app()` gained `auth_store: AuthStore | None = None`. Routes
    (`POST /signup`, `POST /login`, `POST /logout`, `GET /me`) are mounted
    **only** when both `auth_store` and `settings` are supplied — so every
    existing `create_app()` call site (tests/test_main.py, unmodified)
    keeps working exactly as before, unaware auth exists. This is the
    concrete mechanism behind "anonymous chat unaffected."
  - `bootstrap()` now also builds an `AuthStore`, calls `ensure_schema()`
    (warn-and-continue on failure, same non-critical stance as
    `trace_store.ensure_schema()` — ADR-0003's "additive not gating"), and
    passes it into `create_app()`.
- `pyproject.toml`: added `argon2-cffi>=23.1`, `itsdangerous>=2.2` (installed
  into `.venv` directly with `pip install`, since a plain `pip install -e
  ".[dev]"` currently fails in this checkout — pre-existing setuptools
  multi-package-discovery error from `logs/`/`observability/` dirs at repo
  root, unrelated to this ticket, not touched).
- New `tests/test_auth.py` (21 tests, all fixtures/emails prefixed
  `test-9-...` per CONVENTIONS.md §7), hitting the real dev Postgres at
  `localhost:5433` for `AuthStore`/HTTP-level tests, no server booted on
  `:8080`:
  - `AuthStore`: signup writes both rows with an argon2id hash
    (`$argon2id$` prefix asserted, raw password asserted absent), duplicate
    email raises `AuthError`, login round-trip (right/wrong password,
    unknown email).
  - Session cookie: sign/verify round-trip, tampered value rejected,
    garbage rejected, expired rejected (via `monkeypatch.setattr` shrinking
    `_SESSION_MAX_AGE_SECONDS` to `-1` — deterministic, no real 30-day
    wait), cookie attributes match the Context spec (HttpOnly/SameSite=Lax/
    Secure-unless-dev/Max-Age).
  - `get_current_user_optional`: None for missing/invalid cookie, and
    (real, unreachable-pool) DB failure — confirms it never raises.
  - HTTP: `/signup` 201 then 409 on duplicate; `/login` 401 on wrong
    password (no cookie set) then 200 with cookie; `/logout` clears it
    (`/me` flips back to `null`); `/me` returns `null` for a tampered
    cookie without 500ing; login cookie carries `Secure` when
    `APP_ENV=production`.
  - Regression: `/health/live` and `/chat/start` behave identically with
    auth wired in; `create_app()` called the old way (no `auth_store`) 404s
    on `/signup` instead of mounting it or erroring.
  - Config: missing `SECRET_KEY` -> `ConfigError`; present -> `Settings`
    populated, `app_env` defaults to `"production"`.

### Evidence
`pytest -q tests/test_auth.py` (the verifier_command):
```
collected 21 items
tests/test_auth.py .....................                                 [100%]
21 passed in 1.42s
```
Full suite `pytest -q`: `1 failed, 105 passed` — see "Known side effect"
below for the one failure; it is pre-existing test surface outside this
ticket's owned regions, not new feature-code drift.
`pytest -q tests/test_main.py` (existing, unmodified — regression proof
for criterion 5): `28 passed`.
No leftover `test-9-*` rows in the dev DB after the run (checked directly
against Postgres after the suite finished).

### Known side effect (flagged, not fixed — outside owned regions)
`tests/test_config.py::test_load_config_propagates_llamaparse_vars_into_os_environ`
now fails: it writes `HANDBOOK_PATH`/`DATABASE_URL` (not `SECRET_KEY`) into
a temp `.env` and calls `load_config()`, which now requires `SECRET_KEY` —
a direct, unavoidable consequence of criterion 6 ("missing SECRET_KEY
raises ConfigError at boot, same as HANDBOOK_PATH/DATABASE_URL today").
`tests/test_config.py` is not app/auth.py, app/config.py, or main.py, so
per "touch only owned regions" I have not edited it — leaving it for the
orchestrator/human to decide (one-line fix: add `SECRET_KEY=...` to that
test's temp `.env`). Confirmed this is a genuine, order-independent
failure, not a red herring — earlier I saw it accidentally pass in a
full-suite run because an env var my own test set via
`os.environ.setdefault` (inside `load_config()`) leaked into the process
and was never actually cleaned up (a `monkeypatch.delenv` on a var
monkeypatch never saw set restores it, it doesn't remove it) I fixed that
leak in my own test (now uses `os.environ.pop` directly in a `finally`),
so this failure is now real, deterministic, and reproducible in isolation
(`pytest -q tests/test_config.py` alone) and in the full suite either way.

### Acceptance criteria status
1. POST /signup creates users + auth_identities rows (argon2id) — GREEN
2. POST /login verifies password, sets signed session cookie — GREEN
3. POST /logout clears the cookie — GREEN
4. Depends(get_current_user_optional) never raises, None for anon/invalid/expired — GREEN
5. Existing anonymous chat flow unaffected — GREEN (test_main.py unmodified, still 28/28; auth routes only mount when explicitly wired)
6. APP_ENV/SECRET_KEY in Settings; missing SECRET_KEY -> ConfigError at boot — GREEN

All six acceptance criteria green. Every check under
`verifier_command` (`pytest -q tests/test_auth.py`) passes: 21/21.

### What's next
Nothing red. Per the user's direct instruction I am not setting
`agent:gate-pending`, not labeling `agent:verified`, and not opening or
merging anything — that verification step is being driven by hand. Flagging
the `tests/test_config.py` side effect above for whoever reviews next.

## 2026-09-18T05:34:31Z — verify round 1: NEEDS_WORK fix

Verifier round 1 returned NEEDS_WORK: full-suite run (not just
verifier_command) surfaced a regression in a pre-existing, unmodified test —
`tests/test_config.py::test_load_config_propagates_llamaparse_vars_into_os_environ`
started failing because `SECRET_KEY` became a required Settings field
(criterion 6) but that test's .env fixture never set it.

Fix: added `SECRET_KEY=test-9-boot-secret` to that test's .env fixture and
delenv/pop it the same way `tests/test_auth.py`'s own SECRET_KEY tests do.
No change to the SECRET_KEY requirement itself.

Approvals recorded (given explicitly by the user in chat, not self-granted):
- Out-of-region edit to `tests/test_config.py` (outside app/auth.py,
  app/config.py, and main.py's auth region) — approved to fix the regression
  above.
- `pyproject.toml` dependency additions (argon2-cffi, itsdangerous) from the
  original round — approved.

Full suite green after fix: `PYTHONPATH=. uv run pytest -q` → 106 passed.

## 2026-09-18T01:49:35-04:00 — verify round 2: NEEDS_WORK, two findings

Verifier round 2 returned NEEDS_WORK with two findings. Handled separately,
per the user's explicit instructions on which was real:

**Finding 1 — tests/test_config.py regression: stale-branch artifact, not a
live regression.** The verifier gated `verify/9-password-auth` @ `cc65bb1`,
one commit behind `impl/9-password-auth` — it predated `d6f2bce` (the round-1
fix above, logged in this same file). `verify/9-password-auth` has since
been fast-forwarded to `d6f2bce` by the user. No code change made for this
finding; confirmed by re-reading `git show d6f2bce -- tests/test_config.py`
(the fix is there) and `git branch --all --contains d6f2bce` (both `impl/`
and, after the fast-forward, `verify/9-password-auth` contain it).

**Finding 2 — flaky tamper test, real, fixed.**
`tests/test_auth.py::test_verify_session_cookie_rejects_tampered_value`
mutated the token's last base64url character
(`token[:-1] + ("a" if token[-1] != "a" else "b")`). Root cause per the
verifier: itsdangerous base64url-encodes the raw HMAC digest, and the last
character of a base64 group can carry "slack" bits that don't correspond to
real digest bytes — flipping only that character decodes back to an
*identical* byte string roughly 1 time in 4, so the test asserted
`verify_session_cookie(...) is None` against a signature that was, bit for
bit, still valid. That's a nondeterministic test, not a bug in
`app/auth.py` — confirmed by the verifier decoding both signatures
byte-for-byte and finding them equal on a failing run.

Fix (`tests/test_auth.py` only — `app/auth.py` not touched, assertion not
weakened): added `_flip_a_real_bit()`, which base64url-decodes the token's
signature segment, XORs a real bit in the first decoded byte, and
re-encodes. An XOR always changes the byte value, so the re-signed digest
is guaranteed invalid on every run — no reliance on which base64 group
boundary the last character happens to fall on. The test now also asserts
`tampered != token` before checking rejection, so a would-be no-op mutation
fails loudly instead of silently passing.

Evidence — the exact command from the verifier's instructions, run 10
times back to back, nothing else changed between runs:
```
PYTHONPATH=. uv run pytest -q tests/test_auth.py
run 1:  21 passed in 1.31s
run 2:  21 passed in 1.32s
run 3:  21 passed in 1.29s
run 4:  21 passed in 1.29s
run 5:  21 passed in 1.26s
run 6:  21 passed in 1.27s
run 7:  21 passed in 1.28s
run 8:  21 passed in 1.29s
run 9:  21 passed in 1.27s
run 10: 21 passed in 1.25s
```
**10 for 10.** No leftover `test-9-*` rows in the dev Postgres after the
run.

### Acceptance criteria status (unchanged from round 1, criterion 4 now
solid rather than "PASS but the dedicated tamper test is flaky")
1. signup → users + auth_identities (argon2id) — GREEN
2. login verifies password, sets signed cookie — GREEN
3. logout clears cookie — GREEN
4. get_current_user_optional never raises, None for anon/invalid/expired — GREEN (tamper-rejection now deterministic, 10/10)
5. anonymous chat unaffected — GREEN
6. APP_ENV/SECRET_KEY, ConfigError on missing SECRET_KEY — GREEN

### What's next
Nothing red, ten consecutive clean runs recorded above. Setting
`agent:gate-pending` on issue #9 now, per "Set agent:gate-pending only after
ten clean consecutive runs." Not opening a PR, not running the verifier, not
labeling `agent:verified` — per the user's explicit instructions, those
stay driven by hand.
