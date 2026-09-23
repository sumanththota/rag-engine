# #33 contract — draft tail for the issue body (option A)

Not yet applied to the issue. Everything above the `---` is what is already in issue #33;
everything below is the enforceable tail that replaces a separate `LOOP.md`.

Region boundaries were verified against `app/main.py` on 2026-09-21: all eight helpers
(`_TRACES_PAGE_STYLE`, `_rewrite_status_badge`, `_status_icon`, `_step_html`,
`_page_shell`, `_sidebar_html`, `_annotation_panel_html`, `_traces_page_html`) are called
only from inside the traces cluster or from the three routes — `_page_shell` is called
exactly once, from `_traces_page_html`. The extraction is closed.

**Missing from this draft, and required before use:** the region markers #34–#40 will own
(see `.loop/DESIGN.md`, "Sequencing"). Without them the downstream contracts must name
line numbers, which the next merge invalidates.

---

````markdown
## What to build

Prefactor. The Trace review board's rendering (page shell, Trace list, Step rendering,
Annotation panel, styles) and its routes currently live inline in the main app module,
which is why every later change to the review UI would touch a large shared file. Move
them into a dedicated module so each following slice has a narrow, exclusive region to
work in. Nothing a user or a test can observe changes.

## Blocked by

None - can start immediately

---
<!-- LOOP CONTRACT — read by /loop. Everything below is machine-enforced. -->

## Owned regions

Exhaustive. `git diff --name-only <base>...impl/33-*` is checked against exactly this
list; anything else is an escalation, not a judgement call.

- `app/traces_ui.py` — NEW. Receives the review UI verbatim.
  Named `traces_ui` because `app/traces.py` already exists and owns the data layer
  (`TraceStore`, `TraceStep`, `TraceDetail`); this module must not absorb any of it.
- `app/main.py` — DELETIONS AND WIRING ONLY, in exactly two places:
  - the helper cluster `_TRACES_PAGE_STYLE` (~150) through `_traces_page_html`
    (~348-400): `_rewrite_status_badge`, `_status_icon`, `_step_html`, `_page_shell`,
    `_sidebar_html`, `_annotation_panel_html`.
  - the three route handlers at ~1021-1104: `GET /traces`, `GET /traces/{trace_id}`,
    `POST /traces/{trace_id}/annotate`.
  - What may be ADDED: an import of the new module and the call that registers its
    routes. Nothing else. No renames, no reordering, no reformatting of surrounding code.
- `tests/test_33_traces_ui_snapshot.py` — NEW. Fixtures prefixed `test-33-`.
- NOT owned, escalates on touch: `app/traces.py`, `tests/test_traces.py`, any other
  `app/*.py`, `pyproject.toml`, any template.

## Pre-step (NOT the implementer's work)

Before the implementer is spawned, capture golden snapshots from the base branch: for a
fixed seeded set of traces, record the exact response body and status of `GET /traces`,
`GET /traces?unannotated=1`, `GET /traces/{id}` and the redirect target of
`POST /traces/{id}/annotate`. Commit them.

The implementer must not author, regenerate or edit these files. A diff touching them is
an automatic block. This is the whole acceptance mechanism for criterion 1 — an
implementer that can rewrite the oracle has no oracle.

## Acceptance criteria

1. **Byte-identical rendering.** Every captured response is byte-for-byte identical after
   the extraction, including whitespace and attribute order.
   - `verify_via`: pytest — replay each snapshot against the running app and assert
     equality on the raw body. Not a subset match, not a parsed-DOM comparison.
   - `mutation_target`: reorder two sidebar rows in `_sidebar_html`. The snapshot test
     must go red. If it stays green the oracle is not wired.

2. **`tests/test_traces.py` passes unedited.**
   - `verify_via`: `git diff --name-only` shows the file unchanged, AND
     `pytest -q tests/test_traces.py` is green.

3. **`app/main.py` retains no review-UI rendering or routing.**
   - `verify_via`: grep `app/main.py` for `_sidebar_html`, `_step_html`,
     `_annotation_panel_html`, `_traces_page_html`, `_TRACES_PAGE_STYLE`,
     `@app.get("/traces` and `@app.post("/traces` — zero matches for each, except an
     import line.

4. **No schema change, no new dependency.**
   - `verify_via`: `pyproject.toml` and `uv.lock` unchanged in the diff; no
     `CREATE`/`ALTER`/`DROP` in it.

5. **Full suite green.**
   - `verify_via`: `pytest -q`, no `-k` filter.

## Escalation triggers

- any edit outside the owned regions
- any change to a committed snapshot file
- any behavior change proposed as an improvement — this ticket is a move, and a
  "while I was in there" fix is a block, not a bonus
- the same criterion fails in 3 verify rounds

## Budgets

`max_iterations: 6` · `max_verify_rounds: 3` · `wall_clock: 90m`
````
