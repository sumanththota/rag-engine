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
    names a REAL, existing thread_id directly on /chat/stream.

    Round-4 verifier Finding 2: the prior version of this test never called
    /chat/stream (only /chat/start, which alone writes nothing) and never counted
    rows; its one thread_id assertion also sat behind `if match:` (CONVENTIONS.md
    §7). Fixed in the previous round by driving /chat/stream and counting rows.

    Round-5 verifier Finding A: that fix was still unfalsifiable — it spoofed
    thread_id=999999999, an id that can never exist, so (a) a mutant where
    anonymous /chat/stream writes into a supplied thread_id passes anyway (the
    write fails silently, thread_id refers to nothing), and (b) a mutant where
    anonymous /chat/start creates a thread row under some other/made-up user_id
    only gets caught by luck — a foreign-key violation blocks the insert on an
    empty DB regardless of whether the code is right. Fixed here by seeding a
    REAL user and a REAL thread first, then pointing the anonymous requests at
    that real id — only a correct implementation can leave it untouched."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    owner_email = "test-11-anon-target-owner@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, owner_email)

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["anon", " answer"])

        # ---- Seed a real owner with a real thread (and a real message in it) ----
        async with _async_client(app) as owner_client:
            signup = await owner_client.post(
                "/signup", json={"email": owner_email, "password": "test123"}
            )
            assert signup.status_code == 201
            owner_id = signup.json()["id"]
            await owner_client.post(
                "/login", json={"email": owner_email, "password": "test123"}
            )

            owner_start = await owner_client.post(
                "/chat/start",
                data={"question": "owner question", "model_id": "ollama_gemma4_26b"},
            )
            owner_thread_id = _thread_id_from_chat_start_html(owner_start.text)
            assert owner_thread_id is not None and owner_thread_id > 0

            owner_stream = await owner_client.get(
                "/chat/stream",
                params={
                    "question": "owner question",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-owner",
                    "thread_id": str(owner_thread_id),
                },
            )
            assert owner_stream.status_code == 200

        detail_before = await thread_store.get_thread(owner_thread_id, owner_id)
        assert detail_before is not None
        messages_before = len(detail_before.messages)
        assert messages_before == 2, messages_before
        check_start = await pool.fetchval("SELECT now()")

        # ---- Anonymous client (separate session, no cookies from the owner) ----
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

            # Names a REAL, existing thread (the owner's) — not an id that can
            # never exist — so a write here would actually land somewhere real.
            spoofed = await client.get(
                "/chat/stream",
                params={
                    "question": "test-11-anon-marker question 2",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-anon-2",
                    "thread_id": str(owner_thread_id),
                },
            )
            assert spoofed.status_code == 200

            # Verify: GET /threads as anonymous should return 401
            threads_resp = await client.get("/threads")
            assert threads_resp.status_code == 401, "Anonymous GET /threads should return 401"

        # The owner's real thread must be completely untouched by the anonymous
        # requests that named its id.
        detail_after = await thread_store.get_thread(owner_thread_id, owner_id)
        assert detail_after is not None
        assert len(detail_after.messages) == messages_before, (
            "anonymous request wrote into a real, existing thread it named: "
            f"had {messages_before} messages, now {len(detail_after.messages)}: "
            f"{[m.content for m in detail_after.messages]}"
        )

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
        await _cleanup(pool, owner_email)
        await pool.close()


# ---- Round-5 verifier Finding B: oversized thread_id must 404, not crash -----


async def test_oversized_thread_id_returns_404_not_500():
    """An out-of-range thread_id (too large for Postgres bigint) must be
    rejected with 404, not crash the whole turn with an unhandled 500.

    Round-5 verifier Finding B: the ownership-check call site around
    thread_store.get_thread() only caught ValueError (from int() rejecting
    non-numeric input) — a numeric-but-out-of-range id parses fine in Python
    (arbitrary precision ints) but raises ThreadsError once it hits Postgres,
    which propagated uncaught out of both /chat/start and /chat/stream."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-11-oversized-id@example.com"
    oversized_id = "99999999999999999999"  # exceeds bigint range (max ~9.2e18)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["ab"])
        # raise_app_exceptions=False so an unhandled exception in the app comes
        # back as a real response with a status code, not a raised exception in
        # the test itself — needed to observe the current 500 at all.
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            await client.post("/login", json={"email": email, "password": "test123"})

            start = await client.post(
                "/chat/start",
                data={
                    "question": "q",
                    "model_id": "ollama_gemma4_26b",
                    "thread_id": oversized_id,
                },
            )
            assert start.status_code == 404, (
                f"oversized thread_id on /chat/start must return 404, got {start.status_code}"
            )

            stream = await client.get(
                "/chat/stream",
                params={
                    "question": "q",
                    "model_id": "ollama_gemma4_26b",
                    "started_at_ms": "0",
                    "trace_id": "test-11-trace-oversized",
                    "thread_id": oversized_id,
                },
            )
            assert stream.status_code == 404, (
                f"oversized thread_id on /chat/stream must return 404, got {stream.status_code}"
            )
    finally:
        await _cleanup(pool, email)


async def test_get_and_delete_thread_oversized_id_returns_404_not_500():
    """Same bug, same fix, on the other two routes that take a thread_id.

    Round-6 verifier NEEDS_WORK: /chat/start and /chat/stream were fixed to
    return 404 for an out-of-bigint-range thread_id, but GET /threads/{id} and
    DELETE /threads/{id} still return 500 for the same input — this is the
    second route pair to need this exact fix after the first only covered two
    of four routes. This test pins BOTH routes and BOTH the maximum valid
    bigint boundary (max+1) and a grossly oversized value, matching the
    verifier's own probe inputs, so a fix that only handles one shape (e.g.
    only the grossly-oversized string, not the off-by-one boundary) still
    fails it."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-11-oversized-id-get-delete@example.com"
    oversized_ids = [
        "9223372036854775808",  # int64 max (9223372036854775807) + 1
        "99999999999999999999",  # grossly oversized
    ]

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_fake_llm(auth_store, thread_store, pool, tokens=["ab"])
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            await client.post("/login", json={"email": email, "password": "test123"})

            for oversized_id in oversized_ids:
                get_resp = await client.get(f"/threads/{oversized_id}")
                assert get_resp.status_code == 404, (
                    f"GET /threads/{oversized_id} must return 404, got "
                    f"{get_resp.status_code}"
                )

                delete_resp = await client.delete(f"/threads/{oversized_id}")
                assert delete_resp.status_code == 404, (
                    f"DELETE /threads/{oversized_id} must return 404, got "
                    f"{delete_resp.status_code}"
                )
    finally:
        await _cleanup(pool, email)


# ---- Criterion 1: POST /threads/sync upserts threads ----


async def test_sync_threads_basic_upsert():
    """Criterion 1: POST /threads/sync accepts batch, upserts into threads/thread_messages
    for the logged-in user. Verify via HTTP: assert response shape, then independently
    GET /threads/{id} and confirm title + messages match the submitted payload, in order."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-basic@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_auth(auth_store, thread_store)

        async with _async_client(app) as client:
            # Signup and login
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            # Build a local thread with 3 ordered messages (user/assistant/user)
            local_thread = {
                "client_thread_id": "local-thread-1",
                "title": "Test Thread Title",
                "messages": [
                    {"role": "user", "content": "First user message"},
                    {"role": "assistant", "content": "First assistant response", "sources": [{"page": 1, "score": 0.95, "text": "handbook excerpt"}]},
                    {"role": "user", "content": "Second user message"},
                ]
            }

            # POST /threads/sync
            sync_resp = await client.post("/threads/sync", json=[local_thread])
            assert sync_resp.status_code == 200
            sync_data = sync_resp.json()
            assert isinstance(sync_data, list)
            assert len(sync_data) == 1
            result = sync_data[0]
            assert "client_thread_id" in result
            assert "id" in result
            assert result["client_thread_id"] == "local-thread-1"
            server_thread_id = result["id"]
            assert isinstance(server_thread_id, int) and server_thread_id > 0

            # GET /threads/{id} to verify title + messages match exactly
            detail_resp = await client.get(f"/threads/{server_thread_id}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()
            assert detail["title"] == "Test Thread Title"
            assert len(detail["messages"]) == 3
            assert detail["messages"][0]["role"] == "user"
            assert detail["messages"][0]["content"] == "First user message"
            assert detail["messages"][1]["role"] == "assistant"
            assert detail["messages"][1]["content"] == "First assistant response"
            assert detail["messages"][1]["sources"] is not None
            assert len(detail["messages"][1]["sources"]) == 1
            assert detail["messages"][1]["sources"][0]["page"] == 1
            assert detail["messages"][2]["role"] == "user"
            assert detail["messages"][2]["content"] == "Second user message"

            # Verify thread appears in GET /threads list
            list_resp = await client.get("/threads")
            assert list_resp.status_code == 200
            threads = list_resp.json()
            assert any(t["id"] == server_thread_id for t in threads)
    finally:
        await _cleanup(pool, email)


# ---- Criterion 2: Idempotent sync (no duplicates on second call) ----


async def test_sync_threads_idempotent_second_call():
    """Criterion 2: Calling POST /threads/sync twice with identical payload
    (same client_thread_id) is idempotent — exactly ONE server thread exists after,
    and thread_messages count does not double. Verify via HTTP: call sync twice,
    then list_threads and check count, then GET and verify message count unchanged."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-dedup@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_auth(auth_store, thread_store)

        async with _async_client(app) as client:
            # Signup and login
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            # First sync
            local_thread = {
                "client_thread_id": "dedup-test-1",
                "title": "Original Title",
                "messages": [
                    {"role": "user", "content": "First message"},
                    {"role": "assistant", "content": "First response"},
                ]
            }
            sync1 = await client.post("/threads/sync", json=[local_thread])
            assert sync1.status_code == 200
            result1 = sync1.json()[0]
            thread_id_1 = result1["id"]

            # Verify thread was created
            detail1 = await client.get(f"/threads/{thread_id_1}")
            assert detail1.status_code == 200
            detail1_data = detail1.json()
            assert detail1_data["title"] == "Original Title"
            assert len(detail1_data["messages"]) == 2

            # Second sync with IDENTICAL payload (including same client_thread_id)
            sync2 = await client.post("/threads/sync", json=[local_thread])
            assert sync2.status_code == 200
            result2 = sync2.json()[0]
            thread_id_2 = result2["id"]

            # Must be the same thread_id (upsert, not new insert)
            assert thread_id_2 == thread_id_1, (
                f"second sync created a new thread instead of upserting: "
                f"thread_id_1={thread_id_1}, thread_id_2={thread_id_2}"
            )

            # Verify thread still appears once in list_threads (not duplicated)
            list_resp = await client.get("/threads")
            assert list_resp.status_code == 200
            threads = list_resp.json()
            matching_threads = [t for t in threads if t["id"] == thread_id_1]
            assert len(matching_threads) == 1, (
                f"expected exactly 1 thread with id={thread_id_1} in list, "
                f"got {len(matching_threads)}"
            )

            # Verify message count didn't double
            detail2 = await client.get(f"/threads/{thread_id_2}")
            assert detail2.status_code == 200
            detail2_data = detail2.json()
            assert len(detail2_data["messages"]) == 2, (
                f"second sync duplicated messages: expected 2, got "
                f"{len(detail2_data['messages'])}"
            )
    finally:
        await _cleanup(pool, email)


# ---- Criterion 3: window.afterLogin defined and sync before hydrate ----


def test_after_login_defined_in_index_html():
    """Criterion 3 (BLOCKED-ON-18, carve-out): Grep evidence that window.afterLogin
    is defined in index.html and calls fetch('/threads/sync', ...) BEFORE calling
    hydratThreadsFromServer(). Does NOT test login form (that's #18's job)."""
    # Read index.html and search for region markers and the function definition
    with open("app/templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # Verify region markers exist
    assert "// region: after-login (#12)" in html, (
        "Region marker '// region: after-login (#12)' not found in index.html"
    )
    assert "// endregion: after-login" in html, (
        "Region marker '// endregion: after-login' not found in index.html"
    )

    # Extract region content
    start_marker = "// region: after-login (#12)"
    end_marker = "// endregion: after-login"
    start_idx = html.find(start_marker)
    end_idx = html.find(end_marker)
    assert start_idx >= 0 and end_idx > start_idx, (
        "Could not extract region between markers"
    )
    region = html[start_idx:end_idx + len(end_marker)]

    # Verify window.afterLogin is defined
    assert "window.afterLogin" in region, (
        "window.afterLogin not defined in region"
    )
    assert "async function" in region or "function ()" in region, (
        "afterLogin not defined as a function"
    )

    # Verify /threads/sync is called
    assert "/threads/sync" in region, (
        "/threads/sync endpoint not called in afterLogin"
    )

    # Verify hydratThreadsFromServer is called
    assert "hydratThreadsFromServer()" in region, (
        "hydratThreadsFromServer not called in afterLogin"
    )

    # Verify sync comes BEFORE hydrate (sync-then-hydrate order).
    # Strip line comments (// ...) from the region first, so the check operates on
    # real code, not comment text. The function's own leading comment may contain
    # the literal substring "/threads/sync", which would satisfy the ordering
    # assertion regardless of what the actual code does.
    def _strip_line_comments(js: str) -> str:
        return "\n".join(line.split("//", 1)[0] for line in js.split("\n"))

    code_only = _strip_line_comments(region)
    sync_idx = code_only.find("/threads/sync")
    hydrate_idx = code_only.find("hydratThreadsFromServer()")
    assert sync_idx != -1, "no real (non-comment) reference to /threads/sync in the region"
    assert hydrate_idx != -1, "no real (non-comment) call to hydratThreadsFromServer() in the region"
    assert sync_idx < hydrate_idx, (
        "hydratThreadsFromServer called BEFORE /threads/sync in actual code (ignoring comments); "
        f"sync at {sync_idx}, hydrate at {hydrate_idx}"
    )


# ---- Criterion 4: Thread content and ordering preserved after sync ----


async def test_sync_threads_preserves_order_and_content():
    """Criterion 4: A Thread created anonymously (locally), then synced after login,
    appears identically in the server-side list (title, messages, in order) as ONE
    thread (not duplicated across repeated syncs). Build local-shaped payload with
    >=3 ordered messages (user/assistant/user with sources), POST /threads/sync,
    GET /threads/{id}, assert title/messages/order match byte-for-byte, including
    after a second identical sync."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-order@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        app = _app_with_auth(auth_store, thread_store)

        async with _async_client(app) as client:
            # Signup and login
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            # Build payload with >=3 ordered messages with sources
            local_thread = {
                "client_thread_id": "order-test-1",
                "title": "Ordered Messages Test",
                "messages": [
                    {"role": "user", "content": "What is policy?"},
                    {"role": "assistant", "content": "Policy is...", "sources": [{"page": 1, "score": 0.95, "text": "Source 1"}]},
                    {"role": "user", "content": "What about benefits?"},
                    {"role": "assistant", "content": "Benefits include...", "sources": [{"page": 2, "score": 0.87, "text": "Source 2"}]},
                ]
            }

            # First sync
            sync1 = await client.post("/threads/sync", json=[local_thread])
            assert sync1.status_code == 200
            thread_id = sync1.json()[0]["id"]

            # Get thread and verify exact content + order
            detail1 = await client.get(f"/threads/{thread_id}")
            assert detail1.status_code == 200
            data1 = detail1.json()
            assert data1["title"] == "Ordered Messages Test"
            assert len(data1["messages"]) == 4
            # Verify exact order
            assert data1["messages"][0]["role"] == "user"
            assert data1["messages"][0]["content"] == "What is policy?"
            assert data1["messages"][1]["role"] == "assistant"
            assert data1["messages"][1]["content"] == "Policy is..."
            assert data1["messages"][1]["sources"][0]["page"] == 1
            assert data1["messages"][2]["role"] == "user"
            assert data1["messages"][2]["content"] == "What about benefits?"
            assert data1["messages"][3]["role"] == "assistant"
            assert data1["messages"][3]["content"] == "Benefits include..."
            assert data1["messages"][3]["sources"][0]["page"] == 2

            # Second identical sync (idempotent test again for this criterion)
            sync2 = await client.post("/threads/sync", json=[local_thread])
            assert sync2.status_code == 200
            thread_id_2 = sync2.json()[0]["id"]
            assert thread_id_2 == thread_id

            # Get thread again after second sync
            detail2 = await client.get(f"/threads/{thread_id}")
            assert detail2.status_code == 200
            data2 = detail2.json()

            # Verify byte-for-byte match: same title, same message count, same order, same content
            assert data2["title"] == data1["title"]
            assert len(data2["messages"]) == len(data1["messages"]) == 4
            for i, msg in enumerate(data2["messages"]):
                original_msg = data1["messages"][i]
                assert msg["role"] == original_msg["role"]
                assert msg["content"] == original_msg["content"]
                if original_msg.get("sources"):
                    assert msg["sources"] is not None
                    assert len(msg["sources"]) == len(original_msg["sources"])
                    for j, src in enumerate(msg["sources"]):
                        original_src = original_msg["sources"][j]
                        assert src["page"] == original_src["page"]
                        assert src["text"] == original_src["text"]
    finally:
        await _cleanup(pool, email)


def test_get_thread_orders_messages_with_id_tiebreaker():
    """Criterion 4 / HAZARD 2: Postgres does not guarantee any particular order for
    rows with identical created_at values without an explicit secondary sort key.
    A same-transaction batch insert (e.g. from sync_threads) gives every message an
    identical created_at (Postgres now() is fixed per transaction), so without this
    tiebreaker, message order becomes Postgres's undefined tie-break behavior, not
    the submitted order. A behavioral HTTP test cannot reliably distinguish this from
    a correct-by-coincidence result on a small, uncontended table (verified live:
    the behavioral test alone stayed green 3/3 runs with this tiebreaker removed) —
    assert directly on the query text instead."""
    import inspect
    from app.threads import ThreadStore
    source = inspect.getsource(ThreadStore.get_thread)
    assert "ORDER BY created_at ASC, id ASC" in source, (
        "get_thread's message query must break created_at ties with id ASC as a "
        "secondary sort key — see HAZARD 2 in .loop/12/LOOP.md"
    )


# ---- Criterion 5: Ownership/access control (attacker perspective) ----


async def test_sync_threads_respects_ownership():
    """Criterion 5 (ORCHESTRATOR-ADDED): /threads/sync only ever writes to the
    CALLER's own user_id (from session cookie), never a client-supplied one.
    ATTACKER PERSPECTIVE: User A cannot target another user's threads via a crafted
    payload. Seed a real thread for user B, have user A sync a client_thread_id that
    collides with user B's, verify user B's thread is UNTOUCHED and user A got a
    NEW thread instead."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email_a = "test-12-sync-owner-a@example.com"
    email_b = "test-12-sync-owner-b@example.com"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email_a, email_b)

        app = _app_with_auth(auth_store, thread_store)

        # ---- Seed user B with a real thread ----
        async with _async_client(app) as client_b:
            signup_b = await client_b.post(
                "/signup", json={"email": email_b, "password": "test123"}
            )
            assert signup_b.status_code == 201
            user_b_id = signup_b.json()["id"]
            await client_b.post("/login", json={"email": email_b, "password": "test123"})

            # User B syncs a thread with a specific client_thread_id
            thread_b = {
                "client_thread_id": "collision-id",
                "title": "User B's Secret Thread",
                "messages": [
                    {"role": "user", "content": "B's secret message"},
                ]
            }
            sync_b = await client_b.post("/threads/sync", json=[thread_b])
            assert sync_b.status_code == 200
            thread_b_id = sync_b.json()[0]["id"]

            # Verify B's thread exists and has the expected title
            detail_b = await client_b.get(f"/threads/{thread_b_id}")
            assert detail_b.status_code == 200
            data_b = detail_b.json()
            assert data_b["title"] == "User B's Secret Thread"
            messages_b_before = len(data_b["messages"])
            assert messages_b_before == 1

        # ---- User A attempts to sync with the SAME client_thread_id (collision attack) ----
        async with _async_client(app) as client_a:
            signup_a = await client_a.post(
                "/signup", json={"email": email_a, "password": "test123"}
            )
            assert signup_a.status_code == 201
            user_a_id = signup_a.json()["id"]
            await client_a.post("/login", json={"email": email_a, "password": "test123"})

            # A tries to sync with the same collision-id
            thread_a = {
                "client_thread_id": "collision-id",
                "title": "User A's Thread (spoofed title)",
                "messages": [
                    {"role": "user", "content": "A's injected message"},
                ]
            }
            sync_a = await client_a.post("/threads/sync", json=[thread_a])
            assert sync_a.status_code == 200
            thread_a_id = sync_a.json()[0]["id"]

            # A must have gotten a NEW thread_id (not B's)
            assert thread_a_id != thread_b_id, (
                f"sync allowed A to write into B's thread (same thread_id) via "
                f"collision on client_thread_id"
            )

            # Verify A's thread exists independently
            detail_a = await client_a.get(f"/threads/{thread_a_id}")
            assert detail_a.status_code == 200
            data_a = detail_a.json()
            assert data_a["title"] == "User A's Thread (spoofed title)"

        # ---- Verify B's thread was NOT touched ----
        async with _async_client(app) as client_b_check:
            # Re-login as B
            await client_b_check.post("/login", json={"email": email_b, "password": "test123"})

            # Check B's thread again
            detail_b_after = await client_b_check.get(f"/threads/{thread_b_id}")
            assert detail_b_after.status_code == 200
            data_b_after = detail_b_after.json()

            # B's thread must be COMPLETELY unchanged
            assert data_b_after["title"] == "User B's Secret Thread", (
                f"B's thread title was modified: was 'User B's Secret Thread', "
                f"now '{data_b_after['title']}'"
            )
            assert len(data_b_after["messages"]) == messages_b_before, (
                f"B's thread message count changed: was {messages_b_before}, "
                f"now {len(data_b_after['messages'])}"
            )
            assert data_b_after["messages"][0]["content"] == "B's secret message", (
                f"B's message content was altered: {data_b_after['messages'][0]['content']}"
            )
    finally:
        await _cleanup(pool, email_a, email_b)
