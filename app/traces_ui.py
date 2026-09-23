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
from app.store import SearchResult
from app.traces import (
    AnnotationStatus,
    GenerateStep,
    RetrieveStep,
    RewriteStep,
    TagCount,
    TraceCounts,
    TraceDetail,
    TraceError,
    TraceFilter,
    TraceNeighbors,
    TraceStatusFilter,
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
    .status-filters { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.5rem; }
    .status-filter { display: inline-block; color: #4361ee; text-decoration: none; border: 1px solid #4361ee; border-radius: 6px; padding: 0.2rem 0.55rem; font-size: 0.72rem; }
    .status-filter.active { background: #4361ee; color: #fff; }
    .status-filter .count-badge { margin-left: 0.3rem; }
    .list-search { display: flex; flex-direction: column; gap: 0.3rem; margin-top: 0.6rem; }
    .list-search input[type=text] { width: 100%; padding: 0.3rem 0.45rem; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 0.75rem; font-family: inherit; }
    .list-search button { align-self: flex-start; background: #4361ee; color: #fff; border: none; border-radius: 6px; padding: 0.2rem 0.6rem; font-size: 0.72rem; cursor: pointer; }
    .list-pager { display: flex; justify-content: space-between; align-items: center; gap: 0.4rem; margin-top: 0.5rem; font-size: 0.72rem; color: #666; }
    .page-link { color: #4361ee; text-decoration: none; }
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
    .rewrite-line { font-size: 0.8rem; color: #555; margin-bottom: 1rem; }
    .detail-section, details.prompt { background: #fff; border-radius: 8px; padding: 0.75rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .section-title, details.prompt > summary { font-size: 0.9rem; font-weight: 700; }
    .detail-section.answer { border-left: 4px solid #147a4a; }
    .detail-section.answer pre { font-size: 0.9rem; background: none; padding: 0; margin-top: 0.5rem; }
    .empty-output { color: #999; font-style: italic; font-size: 0.85rem; margin-top: 0.5rem; }
    details summary { cursor: pointer; }
    details.more-chunks > summary { font-size: 0.78rem; color: #4361ee; margin-top: 0.5rem; }
    .middle-pane pre { white-space: pre-wrap; word-break: break-word; font-size: 0.8rem; background: #f7f8fc; border-radius: 6px; padding: 0.6rem; margin-top: 0.6rem; }
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
    .tag-suggestions { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.4rem; }
    .tag-suggestion { background: #eef0fb; color: #4361ee; border: none; border-radius: 99px; padding: 0.1rem 0.55rem; font-size: 0.72rem; cursor: pointer; }
    .tag-suggestions.prefixes .tag-suggestion { background: none; border: 1px dashed #4361ee; }
    .tag-suggestion .tag-count { color: #888; margin-left: 0.25rem; }
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


# Chunks shown open in the detail; the rest sit behind a collapsed "show N more".
_VISIBLE_CHUNKS = 3


def _first_step(steps: list[TraceStep], step_type: type) -> TraceStep | None:
    return next((s for s in steps if isinstance(s, step_type)), None)


def _rewrite_line_html(step: RewriteStep) -> str:
    parts = [f"Rewrite {_rewrite_status_badge(step.status.value)}"]
    if step.rewritten_query:
        parts.append(f"rewritten: {html.escape(step.rewritten_query)}")
    return f'<div class="rewrite-line">{" · ".join(parts)}</div>'


def _answer_html(generate: GenerateStep | None, errors: list[tuple[str, str]]) -> str:
    """The generate Step's output, with every Step error beside it so a
    failed turn explains itself before any chunk or prompt text."""
    rows = []
    if generate is None:
        rows.append('<div class="empty-output">(no generate Step)</div>')
    elif generate.output:
        rows.append(f"<pre>{html.escape(generate.output)}</pre>")
    elif not generate.error:
        rows.append('<div class="empty-output">(empty output)</div>')
    for step_type, error in errors:
        rows.append(f'<div class="error-text">{step_type} error: {html.escape(error)}</div>')
    return f'<section class="detail-section answer"><div class="section-title">Answer</div>{"".join(rows)}</section>'


def _chunk_html(r: SearchResult) -> str:
    return (
        f'<div class="chunk"><div class="chunk-meta">page {r.page} · score {r.score:.4f}</div>'
        f"{html.escape(r.text)}</div>"
    )


def _chunks_html(retrieve: RetrieveStep) -> str:
    visible = "".join(_chunk_html(r) for r in retrieve.results[:_VISIBLE_CHUNKS])
    rest = retrieve.results[_VISIBLE_CHUNKS:]
    more = (
        f'<details class="more-chunks"><summary>show {len(rest)} more</summary>'
        f'{"".join(_chunk_html(r) for r in rest)}</details>'
        if rest
        else ""
    )
    title = f"Retrieved chunks ({len(retrieve.results)})"
    return f'<section class="detail-section"><div class="section-title">{title}</div>{visible}{more}</section>'


def _prompt_html(generate: GenerateStep) -> str:
    return (
        f'<details class="prompt"><summary>Prompt ({len(generate.prompt)} chars)</summary>'
        f"<pre>{html.escape(generate.prompt)}</pre></details>"
    )


def _trace_detail_html(trace: TraceDetail) -> str:
    """Answer-first reading order: question, rewrite status line, answer
    (with any Step errors), retrieved chunks, then the collapsed prompt."""
    rewrite = _first_step(trace.steps, RewriteStep)
    retrieve = _first_step(trace.steps, RetrieveStep)
    generate = _first_step(trace.steps, GenerateStep)
    errors = [
        (label, step.error)
        for label, step in (("generate", generate), ("retrieve", retrieve))
        if step is not None and step.error
    ]

    parts = [f'<div class="trace-question">{html.escape(trace.question)}</div>']
    if rewrite is not None:
        parts.append(_rewrite_line_html(rewrite))
    parts.append(_answer_html(generate, errors))
    if retrieve is not None:
        parts.append(_chunks_html(retrieve))
    if generate is not None:
        parts.append(_prompt_html(generate))
    return "".join(parts)


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


_FILTER_LABELS = {
    TraceStatusFilter.ALL: "All",
    TraceStatusFilter.UNANNOTATED: "Unannotated",
    TraceStatusFilter.PASS: AnnotationStatus.PASS.value,
    TraceStatusFilter.FAIL: AnnotationStatus.FAIL.value,
}

_EMPTY_LIST_MESSAGES = {
    TraceStatusFilter.ALL: "No traces yet — chat turns are captured automatically once you ask a question.",
    TraceStatusFilter.UNANNOTATED: "No unannotated traces — everything caught up.",
    TraceStatusFilter.PASS: "No PASS traces yet.",
    TraceStatusFilter.FAIL: "No FAIL traces yet.",
}


# Trace list page size, for both the list and its "Showing X-Y of N" summary.
_PAGE_SIZE = 50


def _parse_page(raw: str | None) -> int:
    """1-based page number; anything missing, non-numeric or below 1 is 1."""
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1


def _list_qs(trace_filter: TraceFilter, page: int = 1) -> str:
    """Query string that carries the active filter (and page) onto a link;
    defaults are left out, so the unfiltered first page's URLs stay bare.
    Not HTML-escaped: escape it when it goes into an attribute."""
    params = {}
    if trace_filter.status is not TraceStatusFilter.ALL:
        params["status"] = trace_filter.status.value
    if trace_filter.tag:
        params["tag"] = trace_filter.tag
    if trace_filter.q:
        params["q"] = trace_filter.q
    if page > 1:
        params["page"] = str(page)
    return f"?{urllib.parse.urlencode(params)}" if params else ""


def _trace_href(trace_id: str, trace_filter: TraceFilter, page: int = 1) -> str:
    return html.escape(f"/traces/{urllib.parse.quote(trace_id)}{_list_qs(trace_filter, page)}")


def _with_selected(traces: list[TraceSummary], selected: TraceDetail) -> list[TraceSummary]:
    """The Trace list plus the opened Trace, if it fell outside the list
    (older than the newest-50 window, or not matching the active filter), so
    the opened Trace is always visible and highlighted."""
    if any(t.trace_id == selected.trace_id for t in traces):
        return traces
    extra = TraceSummary(
        trace_id=selected.trace_id,
        created_at=selected.created_at,
        question=selected.question,
        status=selected.status,
    )
    return sorted([*traces, extra], key=lambda t: (t.created_at, t.trace_id), reverse=True)


def _status_filters_html(counts: TraceCounts, trace_filter: TraceFilter) -> str:
    """Status links keep the tag and search, and go back to page 1."""
    links = []
    for option, label in _FILTER_LABELS.items():
        active = " active" if option is trace_filter.status else ""
        href = html.escape(f"/traces{_list_qs(trace_filter.model_copy(update={'status': option}))}")
        links.append(
            f'<a class="status-filter{active}" href="{href}">'
            f'{label}<span class="count-badge">{counts.for_filter(option)}</span></a>'
        )
    return f'<div class="status-filters">{"".join(links)}</div>'


def _list_search_html(trace_filter: TraceFilter) -> str:
    """GET form for the tag filter and question search; the status filter
    rides along as a hidden field, and submitting goes back to page 1."""
    status_html = (
        f'<input type="hidden" name="status" value="{trace_filter.status.value}"/>'
        if trace_filter.status is not TraceStatusFilter.ALL
        else ""
    )
    return (
        '<form class="list-search" method="get" action="/traces">'
        f"{status_html}"
        f'<input type="text" name="q" value="{html.escape(trace_filter.q or "")}" placeholder="Search questions"/>'
        f'<input type="text" name="tag" value="{html.escape(trace_filter.tag or "")}" placeholder="Tag, e.g. theme:misdirection"/>'
        '<button type="submit">Filter</button>'
        "</form>"
    )


def _list_pager_html(trace_filter: TraceFilter, page: int, listed: int, total: int) -> str:
    """"Showing X-Y of N" plus newer/older links (the list is newest first,
    so older is the next page). Empty when nothing matches at all."""
    if total == 0:
        return ""
    last_page = -(-total // _PAGE_SIZE)
    first = (page - 1) * _PAGE_SIZE
    summary = f"Showing {first + 1}-{first + listed} of {total}" if listed else f"{total} traces"
    newer = (
        f'<a class="page-link newer" href="{html.escape(f"/traces{_list_qs(trace_filter, min(page - 1, last_page))}")}">&larr; Newer</a>'
        if page > 1
        else ""
    )
    older = (
        f'<a class="page-link older" href="{html.escape(f"/traces{_list_qs(trace_filter, page + 1)}")}">Older &rarr;</a>'
        if page < last_page
        else ""
    )
    return f'<div class="list-pager">{newer}<span class="list-summary">{summary}</span>{older}</div>'


def _empty_list_message(trace_filter: TraceFilter, page: int, total: int) -> str:
    if total > 0:
        last_page = -(-total // _PAGE_SIZE)
        return f"No traces on page {page} — the last page is {last_page}."
    if trace_filter.tag or trace_filter.q:
        return "No traces match these filters."
    return _EMPTY_LIST_MESSAGES[trace_filter.status]


def _sidebar_html(
    traces: list[TraceSummary],
    counts: TraceCounts,
    trace_filter: TraceFilter,
    page: int,
    pager_html: str,
    empty_message: str,
    selected_id: str | None,
) -> str:
    if not traces:
        list_html = f'<div class="empty">{html.escape(empty_message)}</div>'
    else:
        items = []
        for t in traces:
            question = t.question if len(t.question) <= 70 else t.question[:67] + "…"
            active = " active" if t.trace_id == selected_id else ""
            items.append(
                f'<a class="thread-item{active}" href="{_trace_href(t.trace_id, trace_filter, page)}">'
                f'<div class="thread-item-top"><span>{html.escape(t.trace_id)}</span>{_status_icon(t.status)}</div>'
                f'<div class="thread-question">{html.escape(question)}</div>'
                f'<div class="thread-meta">{html.escape(t.created_at.isoformat())}</div>'
                "</a>"
            )
        list_html = "".join(items)

    return (
        '<div class="sidebar">'
        '<div class="sidebar-header">'
        '<span class="sidebar-title">Traces</span>'
        f"{_status_filters_html(counts, trace_filter)}"
        f"{_list_search_html(trace_filter)}"
        f"{pager_html}"
        "</div>"
        f'<div class="thread-list">{list_html}</div>'
        "</div>"
    )


# Tag prefixes offered as starting points alongside the tags already in use.
_TAG_PREFIXES = ("theme:", "keep:")

# Clicking a suggestion appends it to the comma-separated tags field (once);
# a prefix is always appended so the rest of the tag can be typed after it.
_TAG_SUGGESTION_SCRIPT = """
  <script>
    document.querySelectorAll(".tag-suggestion").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = document.getElementById("tags");
        const tag = btn.dataset.tag;
        const parts = input.value.split(",").map((t) => t.trim()).filter(Boolean);
        if (!tag.endsWith(":") && parts.includes(tag)) return;
        input.value = [...parts, tag].join(", ");
        input.focus();
      });
    });
  </script>
"""


def _tag_suggestion_html(tag: str, count: int | None = None) -> str:
    count_html = f'<span class="tag-count">{count}</span>' if count is not None else ""
    return (
        f'<button type="button" class="tag-suggestion" data-tag="{html.escape(tag)}">'
        f"{html.escape(tag)}{count_html}</button>"
    )


def _tag_suggestions_html(tag_counts: list[TagCount]) -> str:
    """Tags already in use across all Traces (most-used first), then the
    theme:/keep: prefixes. Suggestions only: the field stays free text."""
    used = "".join(_tag_suggestion_html(c.tag, c.count) for c in tag_counts)
    prefixes = "".join(_tag_suggestion_html(p) for p in _TAG_PREFIXES)
    used_html = f'<div class="tag-suggestions">{used}</div>' if used else ""
    return f'{used_html}<div class="tag-suggestions prefixes">{prefixes}</div>{_TAG_SUGGESTION_SCRIPT}'


def _annotation_panel_html(
    trace: TraceDetail,
    neighbors: TraceNeighbors,
    trace_filter: TraceFilter,
    page: int,
    tag_counts: list[TagCount],
) -> str:
    tags_value = html.escape(", ".join(trace.tags))
    note_value = html.escape(trace.note or "")
    pass_checked = " checked" if trace.status == AnnotationStatus.PASS.value else ""
    fail_checked = " checked" if trace.status == AnnotationStatus.FAIL.value else ""
    annotate_url = f"/traces/{urllib.parse.quote(trace.trace_id)}/annotate{_list_qs(trace_filter, page)}"

    prev_html = (
        f'<a href="{_trace_href(neighbors.prev_id, trace_filter, page)}">&larr; Back</a>'
        if neighbors.prev_id
        else '<span class="disabled">&larr; Back</span>'
    )
    next_html = (
        f'<a href="{_trace_href(neighbors.next_id, trace_filter, page)}">Next &rarr;</a>'
        if neighbors.next_id
        else '<span class="disabled">Next &rarr;</span>'
    )

    return (
        '<div class="panel-title">Rate Conversation</div>'
        # The filter and page ride on the action's query string, not form fields:
        # the form's own `status` field is the Annotation's PASS/FAIL.
        f'<form method="post" action="{html.escape(annotate_url)}">'
        '<div class="rate-group">'
        f'<input type="radio" id="status-pass" name="status" value="{AnnotationStatus.PASS.value}"{pass_checked}/>'
        f'<label for="status-pass" class="rate-btn pass">{AnnotationStatus.PASS.value}</label>'
        f'<input type="radio" id="status-fail" name="status" value="{AnnotationStatus.FAIL.value}"{fail_checked}/>'
        f'<label for="status-fail" class="rate-btn fail">{AnnotationStatus.FAIL.value}</label>'
        "</div>"
        '<label for="note">Notes</label>'
        f'<textarea id="note" name="note" rows="5" placeholder="Add your notes here...">{note_value}</textarea>'
        '<label for="tags">Tags (comma-separated)</label>'
        f'<input id="tags" type="text" name="tags" value="{tags_value}" autocomplete="off"/>'
        f"{_tag_suggestions_html(tag_counts)}"
        '<button type="submit" class="update-btn">Update Annotation</button>'
        "</form>"
        f'<div class="panel-nav">{prev_html}{next_html}</div>'
    )


def _traces_page_html(
    traces: list[TraceSummary],
    counts: TraceCounts,
    trace_filter: TraceFilter,
    page: int,
    total: int,
    selected: TraceDetail | None,
    neighbors: TraceNeighbors | None,
    tag_counts: list[TagCount] | None = None,
) -> str:
    """Renders the single 3-pane board (thread list / trace steps /
    annotation panel) — both GET /traces and GET /traces/{trace_id} share
    this, differing only in whether a trace is selected, so the sidebar and
    its filter/count state never fall out of sync between the two routes.
    `traces` is one page of the filtered list; `total` is how many match."""
    pager_html = _list_pager_html(trace_filter, page, len(traces), total)
    empty_message = _empty_list_message(trace_filter, page, total)
    if selected is not None:
        traces = _with_selected(traces, selected)
    sidebar_html = _sidebar_html(
        traces, counts, trace_filter, page, pager_html, empty_message, selected.trace_id if selected else None
    )

    if selected is None:
        middle_html = '<div class="empty">Select a trace to review.</div>'
        right_html = ""
    else:
        middle_html = _trace_detail_html(selected)
        panel_html = _annotation_panel_html(
            selected, neighbors or TraceNeighbors(), trace_filter, page, tag_counts or []
        )
        right_html = f'<div class="right-pane">{panel_html}</div>'

    body_html = f'{sidebar_html}<div class="middle-pane">{middle_html}</div>{right_html}'
    title = f"Trace {html.escape(selected.trace_id)}" if selected else "Traces"
    return _page_shell(title, "<h1>Traces</h1>", body_html, main_class="board")


def build_router(trace_store: TraceStore) -> APIRouter:
    """Returns the review-board routes bound to `trace_store` (a closure,
    like the rest of create_app()'s routes, so the module stays importable
    without a live Postgres)."""
    router = APIRouter()

    async def list_page(trace_filter: TraceFilter, page: int) -> tuple[list[TraceSummary], int, TraceCounts]:
        """One page of the filtered Trace list, how many Traces match the
        filter, and the table-wide status counts. Raises TraceError."""
        traces = await trace_store.list_recent(
            limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE, trace_filter=trace_filter
        )
        total = await trace_store.count(trace_filter)
        counts = await trace_store.counts()
        return traces, total, counts

    @router.get("/traces")
    async def traces_list(status: str = "all", tag: str = "", q: str = "", page: str = "1") -> HTMLResponse:
        # Renders the same 3-pane board as GET /traces/{trace_id}, with the
        # first trace on the page auto-selected (matching the reference UI's
        # always-something-open sidebar) — never redirects there, so this
        # stays a plain 200 for callers that just want the thread list.
        trace_filter = TraceFilter.parse(status, tag, q)
        page_number = _parse_page(page)
        try:
            traces, total, counts = await list_page(trace_filter, page_number)
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            empty_counts = TraceCounts(total=0, unannotated=0, passed=0, failed=0)
            return HTMLResponse(
                _traces_page_html([], empty_counts, trace_filter, page_number, 0, None, None),
                status_code=500,
            )

        if not traces:
            return HTMLResponse(
                _traces_page_html(traces, counts, trace_filter, page_number, total, None, None)
            )

        first_id = traces[0].trace_id
        token = trace_id_var.set(first_id)
        try:
            trace = await trace_store.get(first_id)
            neighbors = await trace_store.neighbors(first_id, trace_filter)
            tag_counts = await trace_store.tag_counts()
        except TraceError as err:
            logger.warning("traces list failed err=%s", err, extra={"stage": "http"})
            return HTMLResponse(
                _traces_page_html(traces, counts, trace_filter, page_number, total, None, None),
                status_code=500,
            )
        finally:
            trace_id_var.reset(token)

        return HTMLResponse(
            _traces_page_html(traces, counts, trace_filter, page_number, total, trace, neighbors, tag_counts)
        )

    @router.get("/traces/{trace_id}")
    async def trace_detail(
        trace_id: str, status: str = "all", tag: str = "", q: str = "", page: str = "1"
    ) -> HTMLResponse:
        # trace_id is the Trace being reviewed, so it's the natural scope for
        # trace_id_var here (spec story 17: correlate a Trace's review-UI
        # activity with its own capture logs via the same trace_id).
        trace_filter = TraceFilter.parse(status, tag, q)
        page_number = _parse_page(page)
        token = trace_id_var.set(trace_id)
        try:
            try:
                trace = await trace_store.get(trace_id)
                if trace is not None:
                    neighbors = await trace_store.neighbors(trace_id, trace_filter)
                    traces, total, counts = await list_page(trace_filter, page_number)
                    tag_counts = await trace_store.tag_counts()
            except TraceError as err:
                logger.warning("trace detail failed err=%s", err, extra={"stage": "http"})
                return HTMLResponse("<p>Could not load trace.</p>", status_code=500)
            if trace is None:
                return HTMLResponse("<p>Trace not found.</p>", status_code=404)
            return HTMLResponse(
                _traces_page_html(
                    traces, counts, trace_filter, page_number, total, trace, neighbors, tag_counts
                )
            )
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
            params = request.query_params
            trace_filter = TraceFilter.parse(params.get("status"), params.get("tag"), params.get("q"))
            page_number = _parse_page(params.get("page"))

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

            return RedirectResponse(
                url=f"/traces/{urllib.parse.quote(trace_id)}{_list_qs(trace_filter, page_number)}",
                status_code=303,
            )
        finally:
            trace_id_var.reset(token)

    return router
