"""Trace review board: the 3-pane HTML UI (Trace list / Steps / Annotation
panel) and its routes, `GET /traces`, `GET /traces/{trace_id}` and
`POST /traces/{trace_id}/annotate`.

Split out of app/main.py so review-UI changes stay in one module;
`create_app()` only mounts `build_router(trace_store)`.
"""

import html
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.logging_utils import trace_id_var
from app.traces import (
    AnnotationStatus,
    GenerateStep,
    RetrieveStep,
    RewriteStep,
    TraceCounts,
    TraceDetail,
    TraceError,
    TraceNeighbors,
    TraceStep,
    TraceStore,
    TraceSummary,
)

# Same logger name the routes used while they lived in app/main.py, so
# existing log lines/greps are unchanged.
logger = logging.getLogger("server")

_TRACES_PAGE_STYLE = """
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f4f6f9; color: #1a1a2e; line-height: 1.5; }
    header { background: #1a1a2e; color: #fff; padding: 1rem 1.5rem; }
    header h1 { font-size: 1.1rem; }
    main.board { display: flex; height: calc(100vh - 53px); overflow: hidden; }

    .badge { display: inline-block; font-size: 0.72rem; font-weight: 700; border-radius: 99px; padding: 0.1rem 0.6rem; }
    .badge.ran { background: #e3f8ec; color: #147a4a; }
    .badge.not_configured { background: #eef0fb; color: #666; }
    .badge.failed_fallback { background: #fdeaea; color: #b00020; }

    /* left pane: thread list */
    .sidebar { width: 300px; flex-shrink: 0; background: #fff; border-right: 1px solid #e2e5ec; display: flex; flex-direction: column; }
    .sidebar-header { padding: 0.9rem 1rem; border-bottom: 1px solid #eee; }
    .sidebar-title { font-size: 0.85rem; font-weight: 700; }
    .count-badge { background: #eef0fb; color: #4361ee; border-radius: 99px; padding: 0.05rem 0.5rem; font-size: 0.7rem; margin-left: 0.4rem; }
    .filter-toggle { display: inline-block; margin-top: 0.5rem; color: #4361ee; text-decoration: none; border: 1px solid #4361ee; border-radius: 6px; padding: 0.2rem 0.55rem; font-size: 0.72rem; }
    .filter-toggle.active { background: #4361ee; color: #fff; }
    .thread-list { overflow-y: auto; flex: 1; }
    .thread-item { display: block; padding: 0.7rem 1rem; border-bottom: 1px solid #f0f1f5; text-decoration: none; color: inherit; }
    .thread-item:hover { background: #f7f8fc; }
    .thread-item.active { background: #eef0fb; border-left: 3px solid #4361ee; }
    .thread-item-top { display: flex; justify-content: space-between; align-items: center; font-family: monospace; font-size: 0.78rem; font-weight: 700; }
    .status-icon.pass { color: #147a4a; }
    .status-icon.fail { color: #b00020; }
    .status-icon.unannotated { color: #ccc; }
    .thread-question { font-size: 0.78rem; color: #444; margin-top: 0.25rem; }
    .thread-meta { font-size: 0.7rem; color: #999; margin-top: 0.2rem; }

    /* middle pane: trace steps */
    .middle-pane { flex: 1; overflow-y: auto; padding: 1.5rem; }
    .trace-question { background: #fff; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); font-size: 0.9rem; }
    details.step { background: #fff; border-radius: 8px; padding: 0.75rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    details.step summary { font-size: 0.9rem; font-weight: 700; cursor: pointer; }
    details.step > *:not(summary) { margin-top: 0.6rem; }
    .step pre { white-space: pre-wrap; word-break: break-word; font-size: 0.8rem; background: #f7f8fc; border-radius: 6px; padding: 0.6rem; }
    .chunk { border-left: 3px solid #4361ee; background: #f7f8fc; border-radius: 6px; padding: 0.5rem 0.7rem; margin-top: 0.4rem; font-size: 0.8rem; }
    .chunk-meta { color: #888; font-size: 0.72rem; margin-bottom: 0.2rem; }
    .error-text { color: #b00020; font-size: 0.8rem; margin-top: 0.4rem; }
    .empty { color: #999; padding: 2rem; text-align: center; font-size: 0.85rem; }

    /* right pane: annotation panel */
    .right-pane { width: 280px; flex-shrink: 0; background: #fff; border-left: 1px solid #e2e5ec; padding: 1.25rem; overflow-y: auto; }
    .panel-title { font-size: 0.85rem; font-weight: 700; margin-bottom: 0.75rem; }
    .rate-group { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
    .rate-group input[type=radio] { position: absolute; opacity: 0; pointer-events: none; }
    .rate-btn { flex: 1; text-align: center; padding: 0.5rem; border-radius: 6px; font-size: 0.8rem; font-weight: 700; cursor: pointer; background: #eef0fb; color: #666; }
    .rate-btn.pass { color: #147a4a; }
    .rate-btn.fail { color: #b00020; }
    input#status-pass:checked + label.rate-btn.pass { background: #147a4a; color: #fff; }
    input#status-fail:checked + label.rate-btn.fail { background: #b00020; color: #fff; }
    .right-pane label { display: block; font-size: 0.8rem; font-weight: 600; margin: 0.6rem 0 0.3rem; }
    .right-pane textarea, .right-pane input[type=text] { width: 100%; padding: 0.5rem; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 0.85rem; font-family: inherit; }
    .update-btn { margin-top: 0.9rem; width: 100%; background: #4361ee; color: #fff; border: none; border-radius: 6px; padding: 0.6rem; font-size: 0.85rem; cursor: pointer; }
    .panel-nav { display: flex; justify-content: space-between; margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid #eee; }
    .panel-nav a, .panel-nav span.disabled { font-size: 0.82rem; text-decoration: none; color: #4361ee; }
    .panel-nav span.disabled { color: #ccc; }
  </style>
"""


def _rewrite_status_badge(status: str) -> str:
    return f'<span class="badge {html.escape(status)}">{html.escape(status)}</span>'


def _status_icon(status: str | None) -> str:
    if status == AnnotationStatus.PASS.value:
        return '<span class="status-icon pass">&#10003;</span>'
    if status == AnnotationStatus.FAIL.value:
        return '<span class="status-icon fail">&#10007;</span>'
    return '<span class="status-icon unannotated">&ndash;</span>'


def _step_html(step: TraceStep) -> str:
    if isinstance(step, RewriteStep):
        rows = []
        if step.original_question:
            rows.append(f"<pre>original: {html.escape(step.original_question)}</pre>")
        if step.rewritten_query:
            rows.append(f"<pre>rewritten: {html.escape(step.rewritten_query)}</pre>")
        summary = f"Rewrite {_rewrite_status_badge(step.status.value)}"
        return f'<details class="step" open><summary>{summary}</summary>{"".join(rows)}</details>'

    if isinstance(step, RetrieveStep):
        rows = []
        for r in step.results:
            rows.append(
                f'<div class="chunk"><div class="chunk-meta">page {r.page} · score {r.score:.4f}</div>'
                f"{html.escape(r.text)}</div>"
            )
        if step.error:
            rows.append(f'<div class="error-text">{html.escape(step.error)}</div>')
        summary = f"Retrieve ({len(step.results)} results)"
        return f'<details class="step" open><summary>{summary}</summary>{"".join(rows)}</details>'

    if isinstance(step, GenerateStep):
        rows = [
            f"<pre>{html.escape(step.prompt)}</pre>",
            f"<pre>{html.escape(step.output)}</pre>",
        ]
        if step.error:
            rows.append(f'<div class="error-text">{html.escape(step.error)}</div>')
        return f'<details class="step" open><summary>Generate</summary>{"".join(rows)}</details>'

    step_type = html.escape(str(getattr(step, "type", "?")))
    return f'<details class="step" open><summary>Unknown step type: {step_type}</summary></details>'


def _page_shell(title: str, header_html: str, body_html: str, *, main_class: str = "") -> str:
    main_attr = f' class="{main_class}"' if main_class else ""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        f"<title>{title}</title>{_TRACES_PAGE_STYLE}</head><body>"
        f"<header>{header_html}</header>"
        f"<main{main_attr}>{body_html}</main>"
        "</body></html>"
    )


def _sidebar_html(
    traces: list[TraceSummary], counts: TraceCounts, unannotated_only: bool, selected_id: str | None
) -> str:
    qs = "?unannotated=1" if unannotated_only else ""
    if not traces:
        list_html = (
            '<div class="empty">No unannotated traces — everything caught up.</div>'
            if unannotated_only
            else '<div class="empty">No traces yet — chat turns are captured automatically once you ask a question.</div>'
        )
    else:
        items = []
        for t in traces:
            question = t.question if len(t.question) <= 70 else t.question[:67] + "…"
            active = " active" if t.trace_id == selected_id else ""
            items.append(
                f'<a class="thread-item{active}" href="/traces/{urllib.parse.quote(t.trace_id)}{qs}">'
                f'<div class="thread-item-top"><span>{html.escape(t.trace_id)}</span>{_status_icon(t.status)}</div>'
                f'<div class="thread-question">{html.escape(question)}</div>'
                f'<div class="thread-meta">{html.escape(t.created_at.isoformat())}</div>'
                "</a>"
            )
        list_html = "".join(items)

    toggle_href = "/traces" if unannotated_only else "/traces?unannotated=1"
    toggle_label = "Show all" if unannotated_only else f"Show unannotated ({counts.unannotated})"
    return (
        '<div class="sidebar">'
        '<div class="sidebar-header">'
        f'<span class="sidebar-title">All Traces<span class="count-badge">{counts.total}</span></span><br/>'
        f'<a class="filter-toggle{" active" if unannotated_only else ""}" href="{toggle_href}">{toggle_label}</a>'
        "</div>"
        f'<div class="thread-list">{list_html}</div>'
        "</div>"
    )


def _annotation_panel_html(trace: TraceDetail, neighbors: TraceNeighbors, unannotated_only: bool) -> str:
    tags_value = html.escape(", ".join(trace.tags))
    note_value = html.escape(trace.note or "")
    qs = "?unannotated=1" if unannotated_only else ""
    unannotated_field = '<input type="hidden" name="unannotated" value="1"/>' if unannotated_only else ""
    pass_checked = " checked" if trace.status == AnnotationStatus.PASS.value else ""
    fail_checked = " checked" if trace.status == AnnotationStatus.FAIL.value else ""

    prev_html = (
        f'<a href="/traces/{urllib.parse.quote(neighbors.prev_id)}{qs}">&larr; Back</a>'
        if neighbors.prev_id
        else '<span class="disabled">&larr; Back</span>'
    )
    next_html = (
        f'<a href="/traces/{urllib.parse.quote(neighbors.next_id)}{qs}">Next &rarr;</a>'
        if neighbors.next_id
        else '<span class="disabled">Next &rarr;</span>'
    )

    return (
        '<div class="panel-title">Rate Conversation</div>'
        f'<form method="post" action="/traces/{urllib.parse.quote(trace.trace_id)}/annotate">'
        f"{unannotated_field}"
        '<div class="rate-group">'
        f'<input type="radio" id="status-pass" name="status" value="{AnnotationStatus.PASS.value}"{pass_checked}/>'
        f'<label for="status-pass" class="rate-btn pass">{AnnotationStatus.PASS.value}</label>'
        f'<input type="radio" id="status-fail" name="status" value="{AnnotationStatus.FAIL.value}"{fail_checked}/>'
        f'<label for="status-fail" class="rate-btn fail">{AnnotationStatus.FAIL.value}</label>'
        "</div>"
        '<label for="note">Notes</label>'
        f'<textarea id="note" name="note" rows="5" placeholder="Add your notes here...">{note_value}</textarea>'
        '<label for="tags">Tags (comma-separated)</label>'
        f'<input id="tags" type="text" name="tags" value="{tags_value}"/>'
        '<button type="submit" class="update-btn">Update Annotation</button>'
        "</form>"
        f'<div class="panel-nav">{prev_html}{next_html}</div>'
    )


def _traces_page_html(
    traces: list[TraceSummary],
    counts: TraceCounts,
    unannotated_only: bool,
    selected: TraceDetail | None,
    neighbors: TraceNeighbors | None,
) -> str:
    """Renders the single 3-pane board (thread list / trace steps /
    annotation panel) — both GET /traces and GET /traces/{trace_id} share
    this, differing only in whether a trace is selected, so the sidebar and
    its filter/count state never fall out of sync between the two routes."""
    sidebar_html = _sidebar_html(traces, counts, unannotated_only, selected.trace_id if selected else None)

    if selected is None:
        middle_html = '<div class="empty">Select a trace to review.</div>'
        right_html = ""
    else:
        question_html = f'<div class="trace-question">{html.escape(selected.question)}</div>'
        steps_html = "".join(_step_html(step) for step in selected.steps)
        middle_html = f"{question_html}{steps_html}"
        panel_html = _annotation_panel_html(selected, neighbors or TraceNeighbors(), unannotated_only)
        right_html = f'<div class="right-pane">{panel_html}</div>'

    body_html = f'{sidebar_html}<div class="middle-pane">{middle_html}</div>{right_html}'
    title = f"Trace {html.escape(selected.trace_id)}" if selected else "Traces"
    return _page_shell(title, "<h1>Traces</h1>", body_html, main_class="board")


def build_router(trace_store: TraceStore) -> APIRouter:
    """Returns the review-board routes bound to `trace_store` (a closure,
    like the rest of create_app()'s routes, so the module stays importable
    without a live Postgres)."""
    router = APIRouter()

    @router.get("/traces")
    async def traces_list(unannotated: bool = False) -> HTMLResponse:
        # Renders the same 3-pane board as GET /traces/{trace_id}, with the
        # most recent trace auto-selected (matching the reference UI's
        # always-something-open sidebar) — never redirects there, so this
        # stays a plain 200 for callers that just want the thread list.
        try:
            traces = await trace_store.list_recent(unannotated_only=unannotated)
            counts = await trace_store.counts()
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            return HTMLResponse(
                _traces_page_html([], TraceCounts(total=0, unannotated=0), unannotated, None, None),
                status_code=500,
            )

        if not traces:
            return HTMLResponse(_traces_page_html(traces, counts, unannotated, None, None))

        first_id = traces[0].trace_id
        token = trace_id_var.set(first_id)
        try:
            trace = await trace_store.get(first_id)
            neighbors = await trace_store.neighbors(first_id, unannotated_only=unannotated)
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            return HTMLResponse(
                _traces_page_html(traces, counts, unannotated, None, None), status_code=500
            )
        finally:
            trace_id_var.reset(token)

        return HTMLResponse(_traces_page_html(traces, counts, unannotated, trace, neighbors))

    @router.get("/traces/{trace_id}")
    async def trace_detail(trace_id: str, unannotated: bool = False) -> HTMLResponse:
        # trace_id is the Trace being reviewed, so it's the natural scope for
        # trace_id_var here (spec story 17: correlate a Trace's review-UI
        # activity with its own capture logs via the same trace_id).
        token = trace_id_var.set(trace_id)
        try:
            try:
                trace = await trace_store.get(trace_id)
                if trace is not None:
                    neighbors = await trace_store.neighbors(trace_id, unannotated_only=unannotated)
                    traces = await trace_store.list_recent(unannotated_only=unannotated)
                    counts = await trace_store.counts()
            except TraceError as err:
                logger.warning("trace detail failed err=%s", err, extra={"stage": "http"})
                return HTMLResponse("<p>Could not load trace.</p>", status_code=500)
            if trace is None:
                return HTMLResponse("<p>Trace not found.</p>", status_code=404)
            return HTMLResponse(_traces_page_html(traces, counts, unannotated, trace, neighbors))
        finally:
            trace_id_var.reset(token)

    @router.post("/traces/{trace_id}/annotate")
    async def annotate_trace(trace_id: str, request: Request):
        token = trace_id_var.set(trace_id)
        try:
            form = await request.form()
            status_raw = str(form.get("status", "")).strip()
            note = str(form.get("note", "")).strip()
            tags = [t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()]
            unannotated_filter = str(form.get("unannotated", "")).strip() == "1"

            try:
                status = AnnotationStatus(status_raw)
            except ValueError:
                valid = [s.value for s in AnnotationStatus]
                return PlainTextResponse(f"status must be one of {valid}", status_code=400)

            try:
                updated = await trace_store.annotate(trace_id, status, note, tags)
            except TraceError as err:
                logger.warning("annotate failed err=%s", err, extra={"stage": "http"})
                return HTMLResponse("<p>Could not save annotation.</p>", status_code=500)

            if not updated:
                return HTMLResponse("<p>Trace not found.</p>", status_code=404)

            redirect_qs = "?unannotated=1" if unannotated_filter else ""
            return RedirectResponse(
                url=f"/traces/{urllib.parse.quote(trace_id)}{redirect_qs}", status_code=303
            )
        finally:
            trace_id_var.reset(token)

    return router
