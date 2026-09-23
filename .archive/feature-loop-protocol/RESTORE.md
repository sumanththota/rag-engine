# Feature Loop Protocol — archived 2026-09-23

Paused in favour of a different workflow. Nothing here is live: agents should
ignore this directory entirely.

Original locations, if you ever restore:

| archived path | original path |
| --- | --- |
| `loop/` | `.loop/` |
| `claude/agents/implementer.md` | `.claude/agents/implementer.md` |
| `claude/agents/verifier.md` | `.claude/agents/verifier.md` |
| `claude/commands/loop.md` | `.claude/commands/loop.md` |
| `docs/feature-loop-protocol.html` | `docs/feature-loop-protocol.html` |
| `feature_list.json` | `feature_list.json` |
| `progress.md` | `progress.md` |

Restore with `git mv` in reverse.

Removed from live files (restore by hand if the protocol returns):
- `flp-test-instance` dev server (port 8099) in `.claude/launch.json`.
- Old CONVENTIONS.md §7 worktree/orchestrator/verifier rules (see git history before "align ralph").
- Test docstrings citing `.loop/9` and `.loop/18` now say "issue #9/#18".
- GitHub labels `agent:gate-pending`, `agent:in-progress` deleted; `agent:verified` kept (still on closed #9-#12, #18).

User-level skills `flp-doc-sync`, `flp-reshape`, `loop-me` live in `~/.claude/skills/` and are global, not part of this repo.
