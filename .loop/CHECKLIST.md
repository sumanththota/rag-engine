# Pre-run checklist — #33 alone

Design: `DESIGN.md`. Spec: `RULES.md`. Done 2026-09-22: design, rules, all three agent
prompts, #33 reference contract.

## 1. Safety
- [ ] `pg_dump` the traces table.
- [ ] Create + push `traces-ui` from `master`.
- [ ] Work only in `../rag-engine-flp-driver`. Never move HEAD in the main checkout.
- [x] `.gitignore`: `.loop/state/`.

## 2. drive.sh → state machine (implements `RULES.md`)
- [ ] State file read/write, atomic.
- [ ] Lock (`mkdir` + PID).
- [ ] `TICKETS` scope, one ticket at a time, `Blocked by` parsing.
- [ ] T1–T4, each check → act → record.
- [ ] Recovery: `implementing` at tick start → failed round.
- [ ] Agent launcher: flags per role table, `--output-format json`, watchdog killing the
      process group.
- [ ] Label mirror (write-only).
- [ ] Tick log to `.loop/.ticks/`.

## 3. Scripts (`.loop/bin/`, Python)
- [ ] `check-contract` — schema, coverage, paths, oracles, no browser.
- [ ] `gate` — fresh clone, `.venv`, `BASE_SHA`, diff/owned/protected, `pytest`,
      mechanical checks.
- [ ] `parse-verdict` — shape check, per-criterion results.

## 4. Verify the agent launcher
- [x] `--disallowedTools Write,Edit` removes those tools in `-p` (probed 2026-09-22).
- [x] Bash deny-patterns are bypassable (`/usr/bin/git push`) → replaced by sandbox.
- [x] `.loop/agents/sandbox.json`: GitHub unreachable from Bash by any binary; PyPI and
      Postgres :5433 reachable; writes to home/other dirs blocked.
- [x] Commits work inside the sandbox; agents use `git clone --local`, not worktrees.
- [ ] **By hand** (the session's classifier refused to automate it): in a scratch repo,
      ask a sandboxed `claude -p` to run `curl https://api.github.com` with
      `dangerouslyDisableSandbox: true`. Expect refusal/403. If it succeeds, the
      boundary fails — stop.
- [ ] `.venv` install (`pip install -e ".[dev]"`) completes inside the sandbox.
- [ ] Each role completes one run with no permission prompt.

## 5. #33 setup
- [ ] Add criterion 5 (`# region:` markers) to issue #33.
- [ ] Build oracles on `traces-ui` from a dedicated clone: fake-`TraceStore` fixture
      via `create_app(trace_store=...)`, golden files, `tests/test_33_snapshot.py`.
      Confirm the test passes on `traces-ui` and fails when a row is swapped.
- [ ] Commit + push.

## 6. Runs
- [ ] Dry run, stub agents: every transition, every gate failure, a kill mid-tick.
- [ ] Real run: `TICKETS=33 MAX_TICKS=10`, attended, hands off.
- [ ] After: `.loop/.ticks/`, `.loop/33/journal.md`, PR into `traces-ui`.
