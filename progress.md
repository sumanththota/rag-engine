# Progress

Read this first each session. Rolling snapshot of NOW, not history.
Pass/fail per feature: `feature_list.json`. Ticket contract: `.loop/<id>/LOOP.md`. Ticket history: `.loop/INDEX.md`.
Live workflow state is GitHub labels; if this file disagrees with a label, the label wins, then fix this file.

Updated: 2026-09-20 · master @ 1689c06 · baseline `pytest -q` = 143 passed

## Now
- Nothing in flight. No open PRs, no `impl/*` branches.

## Next
- **F005 / #32** Optional login UX (signup, dismissible modal, logout). Label `ready-for-agent`. deps F003, F004 both pass.
  - Not scaffolded yet: no `.loop/32/LOOP.md`, no `impl/32-*` branch.
  - Step 1: orchestrator writes `.loop/32/LOOP.md` from the issue body, then runs `/loop`.

## Blocked / needs human
- None.

## Decisions that bind the next work
- ADR-0003: auth is additive, never gating. Anonymous chat must work identically to today.
- #32 supersedes #18 criterion 1 (blocking login overlay). Frontend only, no backend/schema change.
- #32 adds Playwright + Chromium as a dev-only dep. It is the first real browser test seam, so `loop.md` check (d) hand-evidence should be replaced by it for F005.
- Test literals use the ticket prefix: `test-32-...` (CONVENTIONS.md §7).

## Known gaps (not scheduled)
- `.claude/worktrees` orchestrator-made worktrees get no `.env` copy and no venv; copy/rebuild by hand (CONVENTIONS.md §7).
- No JS test harness until #32 lands.
- Prod-readiness backlog (auth gating, async ingest, hybrid search): `docs/roadmap/production-gaps.md`.

## Parked branches (unmerged, one commit each, not tickets)
- `origin/flp/verifier-planner-sonnet`: verifier + planner default model opus -> sonnet
- `origin/docs/flp-manual-update`: FLP manual sync with #10/#12 findings
- `origin/evals/phase2-axial-coding`: eval phase 2 axial-coding tags

## Recent (newest first, keep last 5)
- 2026-09-20 #18 login UI merged (PR #29); #12 and #18 closed; retro written
- 2026-09-20 #12 thread sync merged (PR #26)
- 2026-09-19 #10 Google OAuth merged (PR #17)
- 2026-09-19 #11 persisted threads merged (PR #15)
- 2026-09-18 #9 password auth merged (PR #14)

## Update rules
- Orchestrator rewrites Now/Next/Blocked at the END of every `/loop` tick and at merge. Implementers and verifier never edit this file.
- Edit in place. Do not append a log: history belongs in `.loop/<id>/journal.md` and `INDEX.md`.
- One line per item, link don't restate. Delete an item when done; push it to Recent, drop the 6th.
- If Now is empty, say so. Never leave a stale in-flight item.
