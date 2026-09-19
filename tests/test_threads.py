"""ThreadStore tests for ticket #11 (persist-threads).

Per ticket #11 and CONVENTIONS.md §7, these hit the real dev Postgres at
localhost:5433 (.env.example's DATABASE_URL) rather than an unreachable socket.
Thread persistence only means something if it actually lands and reads back
correctly.

Test literals use test-11-* prefix to avoid collisions with parallel worktrees
on the shared dev Postgres.
"""

import asyncpg

from app.auth import AuthStore
from app.threads import ThreadStore

_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=0, max_size=2)


async def test_threads_ensure_schema_creates_tables():
    """Criterion: ThreadStore.ensure_schema creates threads and thread_messages tables."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        # Ensure auth schema first (threads references users)
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Verify tables exist by checking if we can query information_schema
        row = await pool.fetchval(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'threads' AND table_schema = 'public'
            """
        )
        assert row == 1, "threads table should exist"

        row = await pool.fetchval(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'thread_messages' AND table_schema = 'public'
            """
        )
        assert row == 1, "thread_messages table should exist"
    finally:
        await pool.close()


async def test_logged_in_user_can_create_and_retrieve_thread():
    """Criterion: Logging in, chatting, then refreshing shows the same conversation."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup any existing test-11-user-1 users
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-1%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-1%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-1%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-1%'"
        )

        # Create a test user
        user = await auth_store.create_user_with_password(
            "test-11-user-1@example.com", "password123"
        )

        # Create a thread for this user
        thread_id = await thread_store.create_thread(user.id, title="Test Conversation")

        # Write messages to the thread
        msg1_id = await thread_store.write_message(
            thread_id, "user", "What is the add/drop deadline?"
        )
        msg2_id = await thread_store.write_message(
            thread_id,
            "assistant",
            "The add/drop deadline is in week 2.",
            sources=[{"page": 4, "score": 0.91, "text": "Add/drop ends week 2."}]
        )

        # Retrieve the thread (simulating a refresh)
        retrieved = await thread_store.get_thread(thread_id, user.id)
        assert retrieved is not None
        assert retrieved.id == thread_id
        assert retrieved.title == "Test Conversation"
        assert len(retrieved.messages) == 2
        assert retrieved.messages[0].role == "user"
        assert retrieved.messages[0].content == "What is the add/drop deadline?"
        assert retrieved.messages[1].role == "assistant"
        assert retrieved.messages[1].content == "The add/drop deadline is in week 2."
        assert retrieved.messages[1].sources is not None
        assert len(retrieved.messages[1].sources) == 1
        assert retrieved.messages[1].sources[0]["page"] == 4
    finally:
        # Clean up
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-%'"
        )
        await pool.close()


async def test_soft_delete_removes_thread_from_list():
    """Criterion: Clicking delete sets deleted_at server-side; Thread no longer appears in list."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup any existing test-11-user-2 users
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-2%'"
        )

        # Create a test user
        user = await auth_store.create_user_with_password(
            "test-11-user-2@example.com", "password123"
        )

        # Create two threads
        thread_id_1 = await thread_store.create_thread(user.id, title="Thread 1")
        thread_id_2 = await thread_store.create_thread(user.id, title="Thread 2")

        # List threads before delete
        threads = await thread_store.list_threads(user.id)
        assert len(threads) == 2

        # Soft-delete one thread
        deleted = await thread_store.soft_delete_thread(thread_id_1, user.id)
        assert deleted is True

        # List threads after delete
        threads = await thread_store.list_threads(user.id)
        assert len(threads) == 1
        assert threads[0].id == thread_id_2
    finally:
        # Clean up
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-2%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-2%'"
        )
        await pool.close()


async def test_user_only_sees_own_threads():
    """Criterion: A user only ever sees their own Threads in the list."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup any existing test-11-user-3 users
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-3%'"
        )

        # Create two test users
        user1 = await auth_store.create_user_with_password(
            "test-11-user-3a@example.com", "password123"
        )
        user2 = await auth_store.create_user_with_password(
            "test-11-user-3b@example.com", "password123"
        )

        # Create threads for each user
        thread1_user1 = await thread_store.create_thread(user1.id, title="User 1 Thread 1")
        thread2_user1 = await thread_store.create_thread(user1.id, title="User 1 Thread 2")
        thread1_user2 = await thread_store.create_thread(user2.id, title="User 2 Thread 1")

        # User 1 should only see their threads
        user1_threads = await thread_store.list_threads(user1.id)
        assert len(user1_threads) == 2
        assert all(t.user_id == user1.id for t in user1_threads)

        # User 2 should only see their threads
        user2_threads = await thread_store.list_threads(user2.id)
        assert len(user2_threads) == 1
        assert user2_threads[0].user_id == user2.id
    finally:
        # Clean up
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-3%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-3%'"
        )
        await pool.close()


async def test_cannot_access_other_users_thread():
    """Criterion: Fetching another user's Thread returns 404/403, not their data."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup any existing test-11-user-4 users
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-4%'"
        )

        # Create two test users
        user1 = await auth_store.create_user_with_password(
            "test-11-user-4a@example.com", "password123"
        )
        user2 = await auth_store.create_user_with_password(
            "test-11-user-4b@example.com", "password123"
        )

        # Create a thread for user1
        thread_id = await thread_store.create_thread(user1.id, title="User 1 Thread")

        # User2 should not be able to access user1's thread
        retrieved = await thread_store.get_thread(thread_id, user2.id)
        assert retrieved is None, "User 2 should not be able to access User 1's thread"
    finally:
        # Clean up
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-4%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-4%'"
        )
        await pool.close()


async def test_each_turn_writes_one_message_per_side():
    """Criterion: Each chat turn writes one thread_messages row per side (user + assistant), including sources."""
    pool = await _pool()
    auth_store = AuthStore(pool)
    thread_store = ThreadStore(pool)

    try:
        await auth_store.ensure_schema()
        await thread_store.ensure_schema()

        # Pre-cleanup any existing test-11-user-5 users
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-5%'"
        )

        # Create a test user
        user = await auth_store.create_user_with_password(
            "test-11-user-5@example.com", "password123"
        )

        # Create a thread
        thread_id = await thread_store.create_thread(user.id, title="Test Turn")

        # Simulate a chat turn: user question + assistant response with sources
        sources = [
            {"page": 1, "score": 0.95, "text": "First chunk"},
            {"page": 2, "score": 0.87, "text": "Second chunk"}
        ]

        await thread_store.write_message(
            thread_id, "user", "What is the policy?", sources=None
        )
        await thread_store.write_message(
            thread_id, "assistant", "The policy states...", sources=sources
        )

        # Retrieve and verify
        retrieved = await thread_store.get_thread(thread_id, user.id)
        assert len(retrieved.messages) == 2

        # Verify user message
        assert retrieved.messages[0].role == "user"
        assert retrieved.messages[0].content == "What is the policy?"
        assert retrieved.messages[0].sources is None

        # Verify assistant message with sources
        assert retrieved.messages[1].role == "assistant"
        assert retrieved.messages[1].content == "The policy states..."
        assert retrieved.messages[1].sources == sources
        assert len(retrieved.messages[1].sources) == 2
    finally:
        # Clean up
        await pool.execute(
            "DELETE FROM thread_messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%'))"
        )
        await pool.execute(
            "DELETE FROM threads WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%')"
        )
        await pool.execute(
            "DELETE FROM auth_identities WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'test-11-user-5%')"
        )
        await pool.execute(
            "DELETE FROM users WHERE email LIKE 'test-11-user-5%'"
        )
        await pool.close()
