"""HTTP-level thread persistence tests for ticket #11 (persist-threads).

Per ticket #11 and LOOP.md verify_via fields, these tests exercise the full HTTP
surface (POST /chat/start, GET /chat/stream, GET /threads, DELETE /threads/{id})
through AsyncClient, not ThreadStore methods directly. Thread persistence only
means something when tested end-to-end through the real API contract.

Test literals use test-11-* prefix to avoid collisions with parallel worktrees
on the shared dev Postgres.
"""

import asyncpg
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import re

from app.auth import AuthStore
from app.config import Settings
from app.embed import OllamaClient
from app.main import create_app
from app.rag import BuildPromptResult, RagService, RewriteOutcome, RewriteOutcomeStatus
from app.store import PostgresStore, SearchResult
from app.threads import ThreadStore
from app.traces import TraceStore

_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"
_SECRET_KEY = "test-11-secret-key-do-not-use-in-prod"


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=0, max_size=2)


async def _cleanup(pool: asyncpg.Pool, *emails: str) -> None:
    for email in emails:
        row = await pool.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if row is not None:
            # Cascade cleanup: threads -> thread_messages -> auth_identities -> users
            await pool.execute(
                "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id = $1)",
                row["id"],
            )
            await pool.execute("DELETE FROM threads WHERE user_id = $1", row["id"])
            await pool.execute("DELETE FROM auth_identities WHERE user_id = $1", row["id"])
            await pool.execute("DELETE FROM users WHERE id = $1", row["id"])


def _app_with_auth(auth_store: AuthStore, thread_store: ThreadStore) -> FastAPI:
    """Create app with auth and thread stores for HTTP testing."""
    settings = Settings(
        handbook_path="unused.pdf",
        database_url=_DSN,
        secret_key=_SECRET_KEY,
        app_env="development",
    )
    embed_client = OllamaClient("http://127.0.0.1:1")
    rag_service = RagService(
        store=PostgresStore(pool=None),
        embed_client=embed_client,
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    return create_app(
        rag_service=rag_service,
        provider_clients={},
        api_keys={},
        embed_client=embed_client,
        store=PostgresStore(pool=None),
        trace_store=TraceStore(pool=None),
        thread_store=thread_store,
        settings=settings,
        auth_store=auth_store,
    )


def _async_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


class _FakeStreamClient:
    """Stands in for a real provider client so /chat/stream's generation step
    completes deterministically without a network call, letting the flow reach
    write_threads()."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens

    async def stream_answer(self, api_key: str, model: str, prompt: str):
        for t in self._tokens:
            yield t


def _thread_id_from_chat_start_html(html: str) -> int | None:
    """Parses the numeric thread_id (5th arg) out of chat_start's rendered
    window.startAnswerStream(...) call, the same contract the real frontend
    reads to build the next turn's request."""
    match = re.search(
        r'startAnswerStream\("[^"]*","[^"]*","[^"]*","[^"]*"(?:,"([^"]*)")?\)', html
    )
    if not match or match.group(1) is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _app_with_fake_llm(
    auth_store: AuthStore,
    thread_store: ThreadStore,
    pool: asyncpg.Pool,
    tokens: list[str],
) -> FastAPI:
    """Same shape as _app_with_auth, but with a fake provider client + a stubbed
    RagService.build_prompt so /chat/stream's generation step completes
    deterministically and actually reaches write_threads() — needed by any test
    that must drive a real turn through the streaming endpoint, not just
    /chat/start."""
    rag_service = RagService(
        store=PostgresStore(pool=None),
        embed_client=OllamaClient("http://127.0.0.1:1"),
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )

    async def _fake_build_prompt(question: str) -> BuildPromptResult:
        return BuildPromptResult(
            prompt=f"prompt for: {question}",
            results=[SearchResult(text="handbook excerpt", page=1, score=0.9)],
            rewrite_outcome=RewriteOutcome(status=RewriteOutcomeStatus.NOT_CONFIGURED),
        )

    rag_service.build_prompt = _fake_build_prompt

    settings = Settings(
        handbook_path="unused.pdf",
        database_url=_DSN,
        secret_key=_SECRET_KEY,
        app_env="development",
    )
    return create_app(
        rag_service=rag_service,
        provider_clients={"ollama": _FakeStreamClient(tokens)},
        api_keys={},
        embed_client=OllamaClient("http://127.0.0.1:1"),
        store=PostgresStore(pool=None),
        trace_store=TraceStore(pool),
        thread_store=thread_store,
        settings=settings,
        auth_store=auth_store,
    )


# ---- Fixed acceptance test (CONVENTIONS.md §7): the conversation-persistence ----
# ---- contract, as ONE unit of work — not four separately-passable criteria. ----


async def test_conversation_persists_across_turns_and_devices():
    """Two real turns through POST /chat/start + GET /chat/stream, same session:

    - both turns must share one thread_id, asserted unconditionally (no `if` guard —
      CONVENTIONS.md §7's ban on a conditional-guarded assertion as the only check)
    - thread_messages must end up with 4 rows (2 turns x 2 sides), sources present
      on the assistant side
    - GET /threads/{id} (the "another device" / refresh path) must return BOTH
      turns' messages — a client that only ever reads GET /threads' list response
      (which carries no message content) cannot satisfy this.
    """
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-11-conv-persist@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        rag_service = RagService(
            store=PostgresStore(pool=None),
            embed_client=OllamaClient("http://127.0.0.1:1"),
            collection="handbook_chunks",
            top_k=10,
            pdf_path="unused.pdf",
        )

        async def _fake_build_prompt(question: str) -> BuildPromptResult:
            return BuildPromptResult(
                prompt=f"prompt for: {question}",
                results=[SearchResult(text="handbook excerpt", page=1, score=0.9)],
                rewrite_outcome=RewriteOutcome(status=RewriteOutcomeStatus.NOT_CONFIGURED),
            )

        rag_service.build_prompt = _fake_build_prompt

        settings = Settings(
            handbook_path="unused.pdf",
            database_url=_DSN,
            secret_key=_SECRET_KEY,
            app_env="development",
        )
        app = create_app(
            rag_service=rag_service,
            provider_clients={"ollama": _FakeStreamClient(["Hello", " there"])},
            api_keys={},
            embed_client=OllamaClient("http://127.0.0.1:1"),
            store=PostgresStore(pool=None),
            trace_store=TraceStore(pool),
            thread_store=thread_store,
            settings=settings,
            auth_store=auth_store,
        )
        await TraceStore(pool).ensure_schema()

        async with _async_client(app) as client:
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            login = await client.post(
                "/login", json={"email": email, "password": "test123"}
            )
            assert login.status_code == 200

            # ---- Turn 1 ----
            start1 = await client.post(
                "/chat/start",
                data={"question": "What is the PTO policy?", "model_id": "ollama_gemma4_26b"},
            )
            assert start1.status_code == 200
            thread_id_1 = _thread_id_from_chat_start_html(start1.text)
            assert thread_id_1 is not None and thread_id_1 > 0, (
                "chat_start must return a numeric thread_id for a logged-in user's turn "
                f"(got {thread_id_1!r} from: {start1.text!r})"
            )

            stream1 = await client.get(
                "/chat/stream",
                params={
                    "question": "What is the PTO policy?",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-1",
                    "thread_id": str(thread_id_1),
                },
            )
            assert stream1.status_code == 200

            # ---- Turn 2: sends the SAME thread_id back, as a persisted client must ----
            start2 = await client.post(
                "/chat/start",
                data={
                    "question": "And sick leave?",
                    "model_id": "ollama_gemma4_26b",
                    "thread_id": str(thread_id_1),
                },
            )
            assert start2.status_code == 200
            thread_id_2 = _thread_id_from_chat_start_html(start2.text)
            # Unconditional — a value that fails to parse or comes back different
            # IS the failure this test exists to catch, not a case to skip.
            assert thread_id_2 == thread_id_1, (
                "turn 2 must reuse turn 1's thread_id instead of minting a new thread "
                f"(turn 1: {thread_id_1!r}, turn 2: {thread_id_2!r})"
            )

            stream2 = await client.get(
                "/chat/stream",
                params={
                    "question": "And sick leave?",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-2",
                    "thread_id": str(thread_id_1),
                },
            )
            assert stream2.status_code == 200

            # ---- thread_messages: 4 rows, sources on the assistant side ----
            detail = await thread_store.get_thread(thread_id_1, user_id)
            assert detail is not None
            assert len(detail.messages) == 4, (
                f"expected 4 thread_messages rows (2 turns x 2 sides), got "
                f"{len(detail.messages)}: {[m.role for m in detail.messages]}"
            )
            roles = [m.role for m in detail.messages]
            assert roles == ["user", "assistant", "user", "assistant"], roles
            assert detail.messages[1].sources, "turn 1's assistant message must carry sources"
            assert detail.messages[3].sources, "turn 2's assistant message must carry sources"

            # ---- GET /threads/{id}: the refresh / "another device" path ----
            # must return the full conversation, not an empty/short messages list.
            detail_resp = await client.get(f"/threads/{thread_id_1}")
            assert detail_resp.status_code == 200
            payload = detail_resp.json()
            assert len(payload["messages"]) == 4, (
                "GET /threads/{id} must return both turns' messages — a client "
                "that hydrates from a list endpoint with no message content, or "
                "hardcodes messages: [], cannot satisfy this: "
                f"got {payload['messages']!r}"
            )
    finally:
        await _cleanup(pool, email)


# ---- Criterion 1: Thread_id round-trip through chat_start -> client state -> next turn ----
# (superseded by test_conversation_persists_across_turns_and_devices above, which drives
# two real turns and asserts the round-trip unconditionally — see round-4 verifier
# Finding 3: this test's single-turn check sat behind `if first_thread_id:` and its
# name promised coverage — "persists_across_turns" — it never actually exercised.
# Removed rather than patched, since the fixed test already supersedes it in full.)


# ---- SECURITY: write-side ownership (CONVENTIONS.md §7) ----------------------
# Round-4 verifier Finding 1: chat_start reused ANY thread_id it was given with no
# ownership check, and write_threads() had no user_id/deleted_at guard on the write
# path (only reads were filtered by owner) — so User B could inject messages into
# User A's Thread, and User A would then see B's content in their own conversation.


async def test_cross_user_cannot_write_into_another_users_thread():
    """User B must not be able to write into User A's Thread by supplying A's
    thread_id to /chat/start or /chat/stream. Asserts the write is rejected AND
    that A's thread_messages are unchanged after the attempt — not just that B's
    own read views are filtered (criteria 4/5's read-side checks already covered
    that and still missed this)."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email_a = "test-11-security-a@example.com"
    email_b = "test-11-security-b@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email_a, email_b)

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["ab"])

        async with _async_client(app) as client_a:
            signup_a = await client_a.post(
                "/signup", json={"email": email_a, "password": "test123"}
            )
            assert signup_a.status_code == 201
            user_a_id = signup_a.json()["id"]
            await client_a.post("/login", json={"email": email_a, "password": "test123"})

            start_a = await client_a.post(
                "/chat/start",
                data={"question": "A question", "model_id": "ollama_gemma4_26b"},
            )
            assert start_a.status_code == 200
            thread_id_a = _thread_id_from_chat_start_html(start_a.text)
            assert thread_id_a is not None and thread_id_a > 0

            stream_a = await client_a.get(
                "/chat/stream",
                params={
                    "question": "A question",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-a",
                    "thread_id": str(thread_id_a),
                },
            )
            assert stream_a.status_code == 200

        detail_before = await thread_store.get_thread(thread_id_a, user_a_id)
        assert detail_before is not None
        messages_before = len(detail_before.messages)
        assert messages_before == 2, messages_before

        # User B — separate session — supplies A's thread_id to BOTH endpoints.
        async with _async_client(app) as client_b:
            signup_b = await client_b.post(
                "/signup", json={"email": email_b, "password": "test123"}
            )
            assert signup_b.status_code == 201
            user_b_id = signup_b.json()["id"]
            await client_b.post("/login", json={"email": email_b, "password": "test123"})

            start_b = await client_b.post(
                "/chat/start",
                data={
                    "question": "B INJECTED",
                    "model_id": "ollama_gemma4_26b",
                    "thread_id": str(thread_id_a),
                },
            )
            assert start_b.status_code == 200
            thread_id_b = _thread_id_from_chat_start_html(start_b.text)
            # Unconditional: B must never be handed back A's thread_id.
            assert thread_id_b != thread_id_a, (
                "chat_start echoed back another user's thread_id to a non-owner "
                f"(thread_id_a={thread_id_a!r})"
            )

            # Even bypassing chat_start, a spoofed thread_id straight to
            # chat_stream must not let B's turn land in A's thread.
            stream_b = await client_b.get(
                "/chat/stream",
                params={
                    "question": "B INJECTED",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-b",
                    "thread_id": str(thread_id_a),
                },
            )
            assert stream_b.status_code == 200

        detail_after = await thread_store.get_thread(thread_id_a, user_a_id)
        assert detail_after is not None
        assert len(detail_after.messages) == messages_before, (
            "CROSS-USER WRITE: user B's turn landed in user A's thread — "
            f"had {messages_before} messages before B's attempt, now has "
            f"{len(detail_after.messages)}: "
            f"{[m.content for m in detail_after.messages]}"
        )
        for m in detail_after.messages:
            assert "B INJECTED" not in m.content, (
                f"user B's content leaked into user A's thread: {m.content!r}"
            )

        b_threads = await thread_store.list_threads(user_b_id)
        assert all(t.id != thread_id_a for t in b_threads), (
            "user B's own thread list includes user A's thread_id"
        )
    finally:
        await _cleanup(pool, email_a, email_b)


async def test_cannot_write_into_soft_deleted_thread():
    """Same hole, same cause (round-4 verifier Finding 1): reusing a thread_id
    must also fail once that thread is soft-deleted — a client that still has
    the old id (e.g. a stale tab) must not be able to resurrect writes into it."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-11-security-deleted@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["ab"])

        async with _async_client(app) as client:
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            start1 = await client.post(
                "/chat/start",
                data={"question": "Before delete", "model_id": "ollama_gemma4_26b"},
            )
            thread_id = _thread_id_from_chat_start_html(start1.text)
            assert thread_id is not None and thread_id > 0

            stream1 = await client.get(
                "/chat/stream",
                params={
                    "question": "Before delete",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-del-1",
                    "thread_id": str(thread_id),
                },
            )
            assert stream1.status_code == 200

            detail_before = await thread_store.get_thread(thread_id, user_id)
            assert detail_before is not None
            messages_before = len(detail_before.messages)
            assert messages_before == 2, messages_before

            delete_resp = await client.delete(f"/threads/{thread_id}")
            assert delete_resp.status_code == 200

            # Same client, same owner — reuses the now-deleted thread_id anyway.
            start2 = await client.post(
                "/chat/start",
                data={
                    "question": "After delete",
                    "model_id": "ollama_gemma4_26b",
                    "thread_id": str(thread_id),
                },
            )
            assert start2.status_code == 200
            reused_id = _thread_id_from_chat_start_html(start2.text)
            assert reused_id != thread_id, (
                "chat_start reused a soft-deleted thread_id instead of minting a new one "
                f"(deleted thread_id={thread_id!r})"
            )

            stream2 = await client.get(
                "/chat/stream",
                params={
                    "question": "After delete",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-del-2",
                    "thread_id": str(thread_id),
                },
            )
            assert stream2.status_code == 200

        # The deleted thread's own row count must not have grown — get_thread
        # returns None for a soft-deleted thread (by design), so assert directly
        # via a raw count against thread_messages instead.
        raw_count = await pool.fetchval(
            "SELECT count(*) FROM thread_messages WHERE thread_id = $1", thread_id
        )
        assert raw_count == messages_before, (
            f"writes landed in a soft-deleted thread: had {messages_before} rows, "
            f"now has {raw_count}"
        )
    finally:
        await _cleanup(pool, email)


# ---- Criterion 3: DELETE /threads soft-deletes and removes from list ----


async def test_delete_thread_soft_deletes_and_removes_from_list():
    """Criterion 3: Clicking delete calls DELETE /threads/{id}, sets deleted_at, thread no longer in list."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-11-delete-1@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup
        await _cleanup(pool, email)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # Signup and login
            await client.post("/signup", json={"email": email, "password": "test123"})
            await client.post("/login", json={"email": email, "password": "test123"})

            # Create a thread via POST /chat/start
            chat_start = await client.post(
                "/chat/start",
                data={"question": "Test question?", "model_id": "ollama_gemma4_26b"},
            )
            assert chat_start.status_code == 200

            # Extract thread_id
            import re
            match = re.search(r'startAnswerStream\("([^"]+)","([^"]+)","([^"]+)","([^"]+)","([^"]+)"\)', chat_start.text)
            assert match, "Should return thread_id in startAnswerStream call"
            thread_id = int(match.group(5))

            # Verify thread appears in GET /threads
            list_before = await client.get("/threads")
            assert list_before.status_code == 200
            threads_before = list_before.json()
            assert any(t["id"] == thread_id for t in threads_before), "Thread should be in list"

            # DELETE the thread
            delete_resp = await client.delete(f"/threads/{thread_id}")
            assert delete_resp.status_code == 200, f"DELETE should succeed, got {delete_resp.status_code}"
            assert delete_resp.json()["success"] is True

            # Verify thread no longer appears in GET /threads
            list_after = await client.get("/threads")
            assert list_after.status_code == 200
            threads_after = list_after.json()
            assert not any(t["id"] == thread_id for t in threads_after), "Deleted thread should not be in list"
    finally:
        await _cleanup(pool, email)


# ---- Criterion 4: User only sees their own threads ----


async def test_user_only_sees_own_threads_in_list():
    """Criterion 4: A user only ever sees their own threads in GET /threads and GET /threads/{id}."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email1 = "test-11-user-separate-1@example.com"
    email2 = "test-11-user-separate-2@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup
        await _cleanup(pool, email1, email2)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # User 1: signup, login, create a thread
            await client.post("/signup", json={"email": email1, "password": "test123"})
            await client.post("/login", json={"email": email1, "password": "test123"})

            chat1 = await client.post(
                "/chat/start",
                data={"question": "User 1 question?", "model_id": "ollama_gemma4_26b"},
            )
            assert chat1.status_code == 200

            import re
            match1 = re.search(r'startAnswerStream\("([^"]+)","([^"]+)","([^"]+)","([^"]+)","([^"]+)"\)', chat1.text)
            user1_thread_id = int(match1.group(5)) if match1 else None

            # User 1: verify they see their thread
            list1 = await client.get("/threads")
            assert list1.status_code == 200
            threads1 = list1.json()
            assert any(t["id"] == user1_thread_id for t in threads1), "User 1 should see their thread"

            # Logout user 1
            await client.post("/logout")

            # User 2: signup and login
            await client.post("/signup", json={"email": email2, "password": "test123"})
            await client.post("/login", json={"email": email2, "password": "test123"})

            # User 2: verify they DON'T see user 1's thread
            list2 = await client.get("/threads")
            assert list2.status_code == 200
            threads2 = list2.json()
            assert not any(t["id"] == user1_thread_id for t in threads2), "User 2 should not see User 1's thread"

            # User 2: create their own thread
            chat2 = await client.post(
                "/chat/start",
                data={"question": "User 2 question?", "model_id": "ollama_gemma4_26b"},
            )
            match2 = re.search(r'startAnswerStream\("([^"]+)","([^"]+)","([^"]+)","([^"]+)","([^"]+)"\)', chat2.text)
            user2_thread_id = int(match2.group(5)) if match2 else None

            # User 2: verify they see only their thread (not user 1's)
            list2_after = await client.get("/threads")
            assert list2_after.status_code == 200
            threads2_after = list2_after.json()
            assert any(t["id"] == user2_thread_id for t in threads2_after), "User 2 should see their thread"
            assert not any(t["id"] == user1_thread_id for t in threads2_after), "User 2 should still not see User 1's thread"
    finally:
        await _cleanup(pool, email1, email2)


# ---- Criterion 5: Cross-user access returns 404 ----


async def test_cannot_access_other_users_thread_via_http():
    """Criterion 5: Fetching another user's thread via GET or DELETE returns 404, not data."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email1 = "test-11-access-1@example.com"
    email2 = "test-11-access-2@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup
        await _cleanup(pool, email1, email2)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # User 1: create a thread
            await client.post("/signup", json={"email": email1, "password": "test123"})
            await client.post("/login", json={"email": email1, "password": "test123"})

            chat1 = await client.post(
                "/chat/start",
                data={"question": "Question?", "model_id": "ollama_gemma4_26b"},
            )

            import re
            match1 = re.search(r'startAnswerStream\("([^"]+)","([^"]+)","([^"]+)","([^"]+)","([^"]+)"\)', chat1.text)
            thread_id_1 = int(match1.group(5)) if match1 else None
            assert thread_id_1, "Should have created a thread"

            # Logout and switch to user 2
            await client.post("/logout")

            await client.post("/signup", json={"email": email2, "password": "test123"})
            await client.post("/login", json={"email": email2, "password": "test123"})

            # User 2: try to GET user 1's thread detail
            get_resp = await client.get(f"/threads/{thread_id_1}")
            assert get_resp.status_code == 404, f"GET /threads/{thread_id_1} should return 404, got {get_resp.status_code}"

            # User 2: try to DELETE user 1's thread
            delete_resp = await client.delete(f"/threads/{thread_id_1}")
            assert delete_resp.status_code == 404, f"DELETE /threads/{thread_id_1} should return 404, got {delete_resp.status_code}"
    finally:
        await _cleanup(pool, email1, email2)


# ---- Criterion 6: Anonymous chat is unaffected ----


async def test_anonymous_chat_does_not_write_threads():
    """Criterion 6: anonymous (no login) chat must not write to threads or
    thread_messages, and must not receive a thread_id — including when a request
    tries to spoof one directly on /chat/stream.

    Round-4 verifier Finding 2: the prior version of this test never called
    /chat/stream (only /chat/start, which alone writes nothing) and never counted
    rows, so a mutant that let anonymous /chat/start create a thread under another
    user still passed it; its one thread_id assertion also sat behind `if match:`,
    which this rewrite removes (CONVENTIONS.md §7)."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["anon", " answer"])

        check_start = await pool.fetchval("SELECT now()")

        async with _async_client(app) as client:
            start = await client.post(
                "/chat/start",
                data={"question": "test-11-anon-marker question", "model_id": "ollama_gemma4_26b"},
            )
            assert start.status_code == 200
            thread_id = _thread_id_from_chat_start_html(start.text)
            # Unconditional: absence of a 5th arg (or a non-numeric one) is the only
            # correct outcome for an anonymous turn — not a case to skip past.
            assert thread_id is None, (
                "anonymous chat_start must not return a thread_id "
                f"(got {thread_id!r} from: {start.text!r})"
            )

            stream = await client.get(
                "/chat/stream",
                params={
                    "question": "test-11-anon-marker question",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-anon",
                },
            )
            assert stream.status_code == 200

            # A spoofed thread_id straight on chat/stream must not let an
            # anonymous request write into (or create) any thread either.
            spoofed = await client.get(
                "/chat/stream",
                params={
                    "question": "test-11-anon-marker question 2",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-anon-2",
                    "thread_id": "999999999",
                },
            )
            assert spoofed.status_code == 200

            # Verify: GET /threads as anonymous should return 401
            threads_resp = await client.get("/threads")
            assert threads_resp.status_code == 401, "Anonymous GET /threads should return 401"

        # No thread_messages row anywhere carrying this test's marker content —
        # immune to concurrent activity from other parallel tickets on the shared
        # dev DB, unlike a raw global count.
        marker_count = await pool.fetchval(
            "SELECT count(*) FROM thread_messages WHERE content LIKE '%test-11-anon-marker%'"
        )
        assert marker_count == 0, (
            f"anonymous chat wrote {marker_count} thread_messages row(s) carrying "
            "this test's marker content"
        )
        # No threads row created during this test's execution window at all.
        new_threads = await pool.fetchval(
            "SELECT count(*) FROM threads WHERE created_at > $1", check_start
        )
        assert new_threads == 0, (
            f"anonymous chat created {new_threads} threads row(s) during this test"
        )
    finally:
        await pool.close()
