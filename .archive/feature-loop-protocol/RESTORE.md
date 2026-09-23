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

Left in place on purpose:
- `.claude/launch.json` still has the `flp-test-instance` dev server (port 8099) — a plain second app instance, harmless outside the protocol.
- `tests/test_auth.py` and `tests/test_login_ui.py` have docstrings citing `.loop/9` and `.loop/18`; those tickets now live under `loop/` here.
- User-level skills `flp-doc-sync`, `flp-reshape`, `loop-me` live in `~/.claude/skills/` and are global, not part of this repo.
