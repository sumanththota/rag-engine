"""HTTP-level thread persistence tests for ticket #11 (persist-threads).

Per ticket #11 and LOOP.md verify_via fields, these tests exercise the full HTTP
surface (POST /chat/start, GET /chat/stream, GET /threads, DELETE /threads/{id})
through AsyncClient, not ThreadStore methods directly. Thread persistence only
means something when tested end-to-end through the real API contract.

Test literals use test-11-* prefix to avoid collisions with parallel worktrees
on the shared dev Postgres.
"""

import asyncio
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


# ---- Criterion 1 & 2 & 4: POST /threads/sync (ticket #12) ----

async def test_sync_threads_accepts_batch_and_upserts():
    """Criterion 1: POST /threads/sync accepts a batch of local Threads
    (id/title/messages) and upserts each into threads/thread_messages for the logged-in user.
    Then GET /threads and /threads/{id} confirm the rows exist with correct data."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-batch-1@example.com"

    # Use real string client ids: UUID-shaped and timestamp-shaped
    client_id_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"  # UUID v4 shaped
    client_id_ts = "test-12-t-1695273600000-a1b2c3"  # timestamp-shaped with test-12- prefix

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # Sign up and log in
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            # Sync a batch of client-generated threads with string ids
            sync_payload = [
                {
                    "id": client_id_uuid,
                    "title": "Synced chat 1",
                    "messages": [
                        {"role": "user", "content": "Hello", "sources": None},
                        {"role": "assistant", "content": "Hi there", "sources": [{"text": "example"}]},
                    ],
                },
                {
                    "id": client_id_ts,
                    "title": "Synced chat 2",
                    "messages": [
                        {"role": "user", "content": "Another question", "sources": None},
                    ],
                },
            ]
            sync_resp = await client.post(
                "/threads/sync",
                json=sync_payload,
            )
            assert sync_resp.status_code == 200, f"sync should succeed, got {sync_resp.status_code}"
            sync_data = sync_resp.json()
            assert sync_data.get("synced", 0) == 2, f"sync should report 2 new threads, got {sync_data.get('synced')}"

            # Extract server ids from sync response (only source of truth for server ids)
            assert "threads" in sync_data, "sync response must include threads mapping"
            threads_mapping = sync_data["threads"]
            assert len(threads_mapping) == 2, f"Expected 2 thread mappings, got {len(threads_mapping)}"

            # Find server ids by client_id
            server_id_1 = None
            server_id_2 = None
            for mapping in threads_mapping:
                if mapping["client_id"] == client_id_uuid:
                    server_id_1 = mapping["id"]
                elif mapping["client_id"] == client_id_ts:
                    server_id_2 = mapping["id"]

            assert server_id_1 is not None, f"server id for {client_id_uuid} not in response"
            assert server_id_2 is not None, f"server id for {client_id_ts} not in response"
            assert isinstance(server_id_1, int) and server_id_1 > 0, f"server_id_1 should be positive int, got {server_id_1}"
            assert isinstance(server_id_2, int) and server_id_2 > 0, f"server_id_2 should be positive int, got {server_id_2}"

            # Verify threads appear in GET /threads
            list_resp = await client.get("/threads")
            assert list_resp.status_code == 200
            threads_list = list_resp.json()
            assert any(t["id"] == server_id_1 for t in threads_list), f"Thread {server_id_1} should be in list"
            assert any(t["id"] == server_id_2 for t in threads_list), f"Thread {server_id_2} should be in list"

            # Verify client_id is exposed in list
            thread_1_in_list = next((t for t in threads_list if t["id"] == server_id_1), None)
            assert thread_1_in_list and thread_1_in_list.get("client_id") == client_id_uuid, (
                f"Thread {server_id_1} should have client_id {client_id_uuid} in list"
            )

            # Verify thread detail via GET /threads/{id} using server id
            detail_resp = await client.get(f"/threads/{server_id_1}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()
            assert detail["id"] == server_id_1, f"server id mismatch in detail"
            assert detail["client_id"] == client_id_uuid, f"client_id mismatch in detail"
            assert detail["title"] == "Synced chat 1"
            assert len(detail["messages"]) == 2
            assert detail["messages"][0]["role"] == "user"
            assert detail["messages"][0]["content"] == "Hello"
            assert detail["messages"][1]["role"] == "assistant"
            assert detail["messages"][1]["content"] == "Hi there"
            assert detail["messages"][1]["sources"] == [{"text": "example"}]

            # Verify second thread detail
            detail_resp2 = await client.get(f"/threads/{server_id_2}")
            assert detail_resp2.status_code == 200
            detail2 = detail_resp2.json()
            assert detail2["id"] == server_id_2
            assert detail2["client_id"] == client_id_ts
            assert detail2["title"] == "Synced chat 2"
            assert len(detail2["messages"]) == 1
            assert detail2["messages"][0]["content"] == "Another question"
    finally:
        await _cleanup(pool, email)
        await pool.close()


async def test_sync_threads_idempotent_by_thread_id():
    """Criterion 2: Logging in a second time (or syncing twice) with the same local
    Threads does not create duplicates (idempotent by Thread id). POST the same batch
    twice, then GET /threads and confirm one row per client_id. Also test concurrent POSTs."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-idempotent-2@example.com"

    client_id = "test-12-idem-f47ac10b-58cc-4372-a567-0e02b2c3d479"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # Sign up and log in
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            await client.post("/login", json={"email": email, "password": "test123"})

            # Sync a batch with string client id
            sync_payload = [
                {
                    "id": client_id,
                    "title": "Idempotent test",
                    "messages": [
                        {"role": "user", "content": "Q1", "sources": None},
                        {"role": "assistant", "content": "A1", "sources": None},
                    ],
                }
            ]
            sync_resp1 = await client.post("/threads/sync", json=sync_payload)
            assert sync_resp1.status_code == 200
            sync_data_1 = sync_resp1.json()
            assert sync_data_1["synced"] == 1, "First sync should create 1 new thread"

            # Extract server id from first sync
            server_id = sync_data_1["threads"][0]["id"]
            assert isinstance(server_id, int) and server_id > 0

            # Get thread count after first sync
            list_resp1 = await client.get("/threads")
            threads_1 = list_resp1.json()
            count_1 = len([t for t in threads_1 if t["id"] == server_id])
            assert count_1 == 1, f"Should have exactly 1 thread {server_id} after first sync"

            detail_resp1 = await client.get(f"/threads/{server_id}")
            detail_1 = detail_resp1.json()
            msg_count_1 = len(detail_1["messages"])
            assert msg_count_1 == 2, "Should have 2 messages after first sync"

            # Sync the SAME batch again (idempotency test) — should return synced=0
            sync_resp2 = await client.post("/threads/sync", json=sync_payload)
            assert sync_resp2.status_code == 200
            sync_data_2 = sync_resp2.json()
            assert sync_data_2["synced"] == 0, "Second sync of same batch should create 0 new threads"
            assert sync_data_2["threads"][0]["id"] == server_id, "Server id should remain the same"

            # Verify no duplicates were created
            list_resp2 = await client.get("/threads")
            threads_2 = list_resp2.json()
            count_2 = len([t for t in threads_2 if t["id"] == server_id])
            assert count_2 == 1, f"Should still have exactly 1 thread {server_id} after second sync (idempotent)"

            detail_resp2 = await client.get(f"/threads/{server_id}")
            detail_2 = detail_resp2.json()
            msg_count_2 = len(detail_2["messages"])
            assert msg_count_2 == 2, "Should still have 2 messages (idempotent, no duplicates)"

            # Test concurrent POSTs with asyncio.gather
            sync_resp3a, sync_resp3b = await asyncio.gather(
                client.post("/threads/sync", json=sync_payload),
                client.post("/threads/sync", json=sync_payload),
            )

            assert sync_resp3a.status_code == 200
            assert sync_resp3b.status_code == 200
            data_3a = sync_resp3a.json()
            data_3b = sync_resp3b.json()

            # Both responses should have synced=0 (idempotent)
            assert data_3a["synced"] == 0, "Concurrent sync A should create 0 new threads"
            assert data_3b["synced"] == 0, "Concurrent sync B should create 0 new threads"

            # Both should return the same server id
            assert data_3a["threads"][0]["id"] == server_id
            assert data_3b["threads"][0]["id"] == server_id

            # Verify final message count is still 2 (no duplicates from concurrent calls)
            detail_resp3 = await client.get(f"/threads/{server_id}")
            detail_3 = detail_resp3.json()
            msg_count_3 = len(detail_3["messages"])
            assert msg_count_3 == 2, "Concurrent syncs should not create duplicate messages"
    finally:
        await _cleanup(pool, email)
        await pool.close()


async def test_sync_threads_anonymous_then_login():
    """Criterion 4: A Thread created anonymously (client-side, with a client-generated id),
    then synced after login, appears identically in the server-side Thread list
    (same client_id, title, messages in order, including identical consecutive messages).
    Tests that the client_id in the sync request matches the client_id in the response and detail."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email = "test-12-sync-anon-to-login-3@example.com"

    # Use real string client id
    client_id = "test-12-anon-d550ac20-c4c7-47f6-9e0e-e0c1a96b8220"

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email)

        async with _async_client(_app_with_auth(auth_store, thread_store)) as client:
            # Simulate anonymous user with a local thread in localStorage
            # Including two identical consecutive messages to test that dedup is removed
            anon_thread = {
                "id": client_id,
                "title": "Anonymous discussion",
                "messages": [
                    {"role": "user", "content": "First question from anon", "sources": None},
                    {"role": "assistant", "content": "First answer to anon", "sources": [{"text": "doc A", "page": 1}]},
                    # Two identical consecutive assistant messages to verify both survive (no role+content dedup)
                    {"role": "assistant", "content": "Repeated message", "sources": None},
                    {"role": "assistant", "content": "Repeated message", "sources": None},
                    {"role": "user", "content": "Follow-up from anon", "sources": None},
                    {"role": "assistant", "content": "Follow-up answer", "sources": [{"text": "doc B", "page": 2}]},
                ],
            }

            # Sign up and log in (now user is logged in)
            signup = await client.post(
                "/signup", json={"email": email, "password": "test123"}
            )
            assert signup.status_code == 201
            user_id = signup.json()["id"]
            await client.post("/login", json={"email": email, "password": "test123"})

            # Sync the anonymous thread
            sync_resp = await client.post("/threads/sync", json=[anon_thread])
            assert sync_resp.status_code == 200
            sync_data = sync_resp.json()

            # Extract server id from sync response
            assert sync_data["synced"] == 1, "Should create 1 new thread"
            assert len(sync_data["threads"]) == 1
            thread_mapping = sync_data["threads"][0]
            assert thread_mapping["client_id"] == client_id, "Response must map correct client_id"
            server_id = thread_mapping["id"]
            assert isinstance(server_id, int) and server_id > 0

            # Verify the thread appears in the server list
            list_resp = await client.get("/threads")
            assert list_resp.status_code == 200
            threads_list = list_resp.json()
            synced_thread = next((t for t in threads_list if t["id"] == server_id), None)
            assert synced_thread is not None, f"Synced thread {server_id} should appear in list"
            assert synced_thread.get("client_id") == client_id, "client_id should be exposed in list"

            # Verify the thread detail is identical
            detail_resp = await client.get(f"/threads/{server_id}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()

            # Check server id
            assert detail["id"] == server_id, f"ID mismatch in detail"

            # Check client_id matches what was sent
            assert detail["client_id"] == client_id, (
                f"client_id mismatch: expected {client_id!r}, got {detail.get('client_id')!r}"
            )

            # Check title
            assert detail["title"] == "Anonymous discussion", (
                f"Title mismatch: expected 'Anonymous discussion', got {detail['title']!r}"
            )

            # Check messages in order (6 messages: 2 identical consecutive ones must both survive)
            assert len(detail["messages"]) == 6, (
                f"Expected 6 messages (with 2 identical), got {len(detail['messages'])}"
            )
            roles = [m["role"] for m in detail["messages"]]
            assert roles == ["user", "assistant", "assistant", "assistant", "user", "assistant"], (
                f"Role sequence mismatch: {roles}"
            )
            contents = [m["content"] for m in detail["messages"]]
            expected_contents = [
                "First question from anon",
                "First answer to anon",
                "Repeated message",
                "Repeated message",
                "Follow-up from anon",
                "Follow-up answer",
            ]
            assert contents == expected_contents, (
                f"Content mismatch: {contents} != {expected_contents}"
            )

            # Check sources
            assert detail["messages"][1]["sources"] == [{"text": "doc A", "page": 1}], (
                f"Sources mismatch for message 1: {detail['messages'][1]['sources']}"
            )
            assert detail["messages"][5]["sources"] == [{"text": "doc B", "page": 2}], (
                f"Sources mismatch for message 5: {detail['messages'][5]['sources']}"
            )
    finally:
        await _cleanup(pool, email)
        await pool.close()


async def test_sync_threads_owner_scoped():
    """Owner-scoped criterion: User B syncing a client_id that User A owns gets a separate
    thread (not A's); A's data is unchanged; 404 when B tries to access A's thread;
    401 when anonymous tries; 422 on integer id; 422 on 129-char id."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)
    email_a = "test-12-owner-a@example.com"
    email_b = "test-12-owner-b@example.com"

    client_id_shared = "test-12-owned-f47ac10b-58cc-4372-a567-0e02b2c3d479"  # Both users will sync this id

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()
        await _cleanup(pool, email_a, email_b)

        app = _app_with_auth(auth_store, thread_store)

        # ---- User A syncs the thread first ----
        async with _async_client(app) as client_a:
            signup_a = await client_a.post(
                "/signup", json={"email": email_a, "password": "test123"}
            )
            assert signup_a.status_code == 201
            user_a_id = signup_a.json()["id"]
            await client_a.post("/login", json={"email": email_a, "password": "test123"})

            sync_a = [
                {
                    "id": client_id_shared,
                    "title": "User A's thread",
                    "messages": [
                        {"role": "user", "content": "A's message", "sources": None},
                    ],
                }
            ]
            sync_resp_a = await client_a.post("/threads/sync", json=sync_a)
            assert sync_resp_a.status_code == 200
            server_id_a = sync_resp_a.json()["threads"][0]["id"]

            # Verify A's thread
            list_a = await client_a.get("/threads")
            threads_a = list_a.json()
            thread_a_in_list = next((t for t in threads_a if t["id"] == server_id_a), None)
            assert thread_a_in_list is not None
            assert thread_a_in_list["title"] == "User A's thread"

        # ---- User B syncs the SAME client_id ----
        async with _async_client(app) as client_b:
            signup_b = await client_b.post(
                "/signup", json={"email": email_b, "password": "test123"}
            )
            assert signup_b.status_code == 201
            user_b_id = signup_b.json()["id"]
            await client_b.post("/login", json={"email": email_b, "password": "test123"})

            sync_b = [
                {
                    "id": client_id_shared,
                    "title": "User B's thread",
                    "messages": [
                        {"role": "user", "content": "B's message", "sources": None},
                    ],
                }
            ]
            sync_resp_b = await client_b.post("/threads/sync", json=sync_b)
            assert sync_resp_b.status_code == 200
            server_id_b = sync_resp_b.json()["threads"][0]["id"]

            # B gets a DIFFERENT server id (separate thread)
            assert server_id_b != server_id_a, (
                f"User B should get a separate thread for the same client_id, "
                f"not A's thread (A's server_id={server_id_a}, B's={server_id_b})"
            )

            # B's thread has B's title and message
            detail_b = await client_b.get(f"/threads/{server_id_b}")
            assert detail_b.status_code == 200
            assert detail_b.json()["title"] == "User B's thread"
            assert detail_b.json()["messages"][0]["content"] == "B's message"

        # ---- Verify A's data is unchanged ----
        async with _async_client(app) as client_a_verify:
            signup_a_verify = await client_a_verify.post(
                "/signup", json={"email": email_a, "password": "test123"}
            )
            # Already exists
            assert signup_a_verify.status_code == 409 or signup_a_verify.status_code == 201
            await client_a_verify.post("/login", json={"email": email_a, "password": "test123"})

            detail_a_verify = await client_a_verify.get(f"/threads/{server_id_a}")
            assert detail_a_verify.status_code == 200
            assert detail_a_verify.json()["title"] == "User A's thread", "A's thread title unchanged"
            assert detail_a_verify.json()["messages"][0]["content"] == "A's message", "A's message unchanged"

        # ---- B cannot access A's thread (404, not 403, per HTTP semantics) ----
        async with _async_client(app) as client_b_verify:
            await client_b_verify.post("/login", json={"email": email_b, "password": "test123"})
            get_a_thread = await client_b_verify.get(f"/threads/{server_id_a}")
            assert get_a_thread.status_code == 404, (
                f"User B accessing User A's thread should return 404 (resource not in B's scope), "
                f"got {get_a_thread.status_code}"
            )

        # ---- Anonymous cannot sync (401) ----
        async with _async_client(app) as client_anon:
            sync_anon = [
                {
                    "id": "test-12-anon-fail-id",
                    "title": "Should fail",
                    "messages": [{"role": "user", "content": "test", "sources": None}],
                }
            ]
            sync_resp_anon = await client_anon.post("/threads/sync", json=sync_anon)
            assert sync_resp_anon.status_code == 401, (
                f"sync without login should return 401, got {sync_resp_anon.status_code}"
            )

        # ---- Integer id should return 422 (validation error) ----
        async with _async_client(app) as client_int_id:
            signup = await client_int_id.post(
                "/signup", json={"email": "test-12-int-id@example.com", "password": "test123"}
            )
            assert signup.status_code in [201, 409]
            await client_int_id.post("/login", json={"email": "test-12-int-id@example.com", "password": "test123"})

            # Send integer id (should fail validation)
            sync_int = [
                {
                    "id": 999,  # integer, not string
                    "title": "Should fail",
                    "messages": [{"role": "user", "content": "test", "sources": None}],
                }
            ]
            sync_resp_int = await client_int_id.post("/threads/sync", json=sync_int)
            assert sync_resp_int.status_code == 422, (
                f"integer id should return 422 (validation error), got {sync_resp_int.status_code}"
            )

        await _cleanup(pool, "test-12-int-id@example.com")

        # ---- 129-char id should return 422 (too long) ----
        async with _async_client(app) as client_long_id:
            signup = await client_long_id.post(
                "/signup", json={"email": "test-12-long-id@example.com", "password": "test123"}
            )
            assert signup.status_code in [201, 409]
            await client_long_id.post("/login", json={"email": "test-12-long-id@example.com", "password": "test123"})

            # Send 129-char id (max is 128)
            long_id = "x" * 129
            sync_long = [
                {
                    "id": long_id,
                    "title": "Should fail",
                    "messages": [{"role": "user", "content": "test", "sources": None}],
                }
            ]
            sync_resp_long = await client_long_id.post("/threads/sync", json=sync_long)
            assert sync_resp_long.status_code == 422, (
                f"129-char id should return 422 (validation error), got {sync_resp_long.status_code}"
            )

        await _cleanup(pool, "test-12-long-id@example.com")

    finally:
        await _cleanup(pool, email_a, email_b)
        await pool.close()
