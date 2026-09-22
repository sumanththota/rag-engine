# #33 contract — reference example

Hand-written 2026-09-22 as the target for planner output (format: `RULES.md` § Contract).
Not applied to the issue — the planner writes the real one.

Verified against `app/main.py` 2026-09-21: the eight helpers (`_TRACES_PAGE_STYLE`,
`_rewrite_status_badge`, `_status_icon`, `_step_html`, `_page_shell`, `_sidebar_html`,
`_annotation_panel_html`, `_traces_page_html`) are called only from inside the traces
cluster or the three routes, so the extraction is closed.

**Before use:**
- Add issue criterion 5 (C6 below): the `# region:` markers. Human-owned intent.
- Commit the oracles to `traces-ui` in setup (CHECKLIST §5). The fixture injects a fake
  `TraceStore` via `create_app(trace_store=...)` so output doesn't depend on the shared DB.

Issue #33's criteria, by position: 1 routes unchanged + existing tests pass unedited ·
2 main module has no review UI · 3 no schema change or new dependency · 4 full suite
green · 5 region markers *(to add)*.

---

````
<!-- LOOP CONTRACT v1 -->
```yaml
owned:
  - app/traces_ui.py (NEW)
  - app/main.py
protected:
  - tests/golden/traces/*
  - tests/test_33_snapshot.py
oracles:
  - tests/golden/traces/fixture.py
  - tests/golden/traces/list.html
  - tests/golden/traces/list_unannotated.html
  - tests/golden/traces/detail.html
  - tests/golden/traces/annotate_redirect.txt
  - tests/test_33_snapshot.py
criteria:
  - id: C1
    from_issue: 1
    kind: behavioral
    verify_via: .venv/bin/pytest -q tests/test_33_snapshot.py  # byte-equal raw bodies
    mutation_target: swap the order of two rows rendered by _sidebar_html
  - id: C2
    from_issue: 1
    kind: mechanical
    check: git diff --quiet "$BASE_SHA" -- tests/test_traces.py && .venv/bin/pytest -q tests/test_traces.py
  - id: C3
    from_issue: 2
    kind: mechanical
    check: "! grep -nE '_sidebar_html|_step_html|_annotation_panel_html|_traces_page_html|_TRACES_PAGE_STYLE|@app\\.(get|post)\\(\"/traces' app/main.py"
  - id: C4
    from_issue: 3
    kind: mechanical
    check: "git diff --quiet \"$BASE_SHA\" -- pyproject.toml uv.lock && ! git diff \"$BASE_SHA\" | grep -qiE '^\\+.*\\b(CREATE|ALTER|DROP) (TABLE|INDEX)'"
  - id: C5
    from_issue: 4
    kind: mechanical
    check: .venv/bin/pytest -q
  - id: C6
    from_issue: 5
    kind: mechanical
    check: "for m in trace-list-rows sidebar-filters annotation-panel detail-body styles; do grep -q \"# region: $m\" app/traces_ui.py || exit 1; done"
budgets: {max_rounds: 6, max_verify_rounds: 3, wall_clock_min: 90}
```
````

Five of six criteria are mechanical. The verifier judges only C1 — whether the snapshot
test really compares raw bytes — plus the issue as a whole.
