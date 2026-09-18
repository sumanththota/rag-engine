"""Auth tests — Feature Loop ticket 9 (.loop/9/LOOP.md).

AuthStore tests hit the real dev Postgres at localhost:5433 (same DSN as
tests/test_traces.py), per CONVENTIONS.md §7: a users/auth_identities write
only means something if it actually lands and reads back correctly. Every
hardcoded email is prefixed test-9-... so this suite never collides with
another ticket's rows on users.email's unique constraint.

HTTP-level tests reuse test_main.py's ASGITransport/httpx.AsyncClient
pattern so /signup, /login, /logout, /me run on the same event loop as the
AuthStore's asyncpg pool.
"""

import os

import asyncpg
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import AuthError, AuthStore, make_get_current_user_optional, verify_session_cookie
from app.config import ConfigError, load_config
from app.embed import OllamaClient
from app.main import create_app
from app.rag import RagService
from app.store import PostgresStore
from app.traces import TraceStore

_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"
_SECRET_KEY = "test-9-secret-key-do-not-use-in-prod"


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=0, max_size=2)


async def _cleanup(pool: asyncpg.Pool, *emails: str) -> None:
    for email in emails:
        row = await pool.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if row is not None:
            await pool.execute("DELETE FROM auth_identities WHERE user_id = $1", row["id"])
            await pool.execute("DELETE FROM users WHERE id = $1", row["id"])
    await pool.close()


# ---- AuthStore (unit, real Postgres) --------------------------------------


async def test_create_user_with_password_writes_users_and_auth_identities_rows():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-signup@example.com"

    try:
        user = await store.create_user_with_password(email, "correct horse battery staple")

        user_row = await pool.fetchrow("SELECT id, email FROM users WHERE id = $1", user.id)
        assert user_row is not None
        assert user_row["email"] == email

        identity_row = await pool.fetchrow(
            "SELECT provider, provider_uid, password_hash FROM auth_identities WHERE user_id = $1",
            user.id,
        )
        assert identity_row is not None
        assert identity_row["provider"] == "password"
        assert identity_row["provider_uid"] == email
        # argon2id hashes are prefixed "$argon2id$" — asserting on that
        # (rather than the raw password never appearing) pins down the
        # specific algorithm the ADR requires, not just "some hash".
        assert identity_row["password_hash"].startswith("$argon2id$")
        assert "correct horse battery staple" not in identity_row["password_hash"]
    finally:
        await _cleanup(pool, email)


async def test_create_user_with_password_rejects_duplicate_email():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-duplicate@example.com"

    try:
        await store.create_user_with_password(email, "first-password")
        try:
            await store.create_user_with_password(email, "second-password")
            assert False, "expected AuthError for duplicate email"
        except AuthError:
            pass
    finally:
        await _cleanup(pool, email)


async def test_authenticate_password_roundtrip():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-login@example.com"

    try:
        created = await store.create_user_with_password(email, "sw0rdfish!")

        ok = await store.authenticate_password(email, "sw0rdfish!")
        assert ok is not None
        assert ok.id == created.id
        assert ok.email == email

        wrong_password = await store.authenticate_password(email, "not-the-password")
        assert wrong_password is None

        unknown_email = await store.authenticate_password("test-9-nobody@example.com", "whatever")
        assert unknown_email is None
    finally:
        await _cleanup(pool, email)


# ---- session cookie --------------------------------------------------------


def test_verify_session_cookie_roundtrip():
    from app.auth import sign_session_cookie

    token = sign_session_cookie(_SECRET_KEY, 42)
    assert verify_session_cookie(_SECRET_KEY, token) == 42


def test_verify_session_cookie_rejects_tampered_value():
    from app.auth import sign_session_cookie

    token = sign_session_cookie(_SECRET_KEY, 42)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert verify_session_cookie(_SECRET_KEY, tampered) is None


def test_verify_session_cookie_rejects_garbage():
    assert verify_session_cookie(_SECRET_KEY, "not-a-real-cookie") is None


def test_set_session_cookie_attributes_match_spec():
    # LOOP.md's Context: "Cookie: itsdangerous-signed, HttpOnly, SameSite=Lax,
    # Secure unless APP_ENV=development, sliding 30-day idle expiry."
    from starlette.responses import Response

    from app.auth import set_session_cookie

    secure_resp = Response()
    set_session_cookie(secure_resp, _SECRET_KEY, 1, secure=True)
    secure_header = secure_resp.headers["set-cookie"]
    assert "HttpOnly" in secure_header
    assert "samesite=lax" in secure_header.lower()
    assert "Secure" in secure_header
    assert f"Max-Age={30 * 24 * 60 * 60}" in secure_header

    dev_resp = Response()
    set_session_cookie(dev_resp, _SECRET_KEY, 1, secure=False)
    assert "Secure" not in dev_resp.headers["set-cookie"]


def test_verify_session_cookie_rejects_expired(monkeypatch):
    import app.auth as auth_module

    token = auth_module.sign_session_cookie(_SECRET_KEY, 42)
    # Force the max_age check to treat any token, however fresh, as expired
    # — deterministic stand-in for waiting out the real 30-day idle expiry.
    monkeypatch.setattr(auth_module, "_SESSION_MAX_AGE_SECONDS", -1)
    assert auth_module.verify_session_cookie(_SECRET_KEY, token) is None


# ---- get_current_user_optional: never raises -------------------------------


async def test_get_current_user_optional_returns_none_for_missing_cookie():
    pool = await _pool()
    store = AuthStore(pool)
    dep = make_get_current_user_optional(store, _SECRET_KEY)

    class _Req:
        cookies: dict = {}

    assert await dep(_Req()) is None
    await pool.close()


async def test_get_current_user_optional_returns_none_for_invalid_cookie():
    pool = await _pool()
    store = AuthStore(pool)
    dep = make_get_current_user_optional(store, _SECRET_KEY)

    class _Req:
        cookies = {"session": "garbage"}

    assert await dep(_Req()) is None
    await pool.close()


async def test_get_current_user_optional_never_raises_on_db_failure():
    # Unreachable pool (same style as test_main.py's _unreachable_rag_service)
    # so get_user() hits a real connection failure, exercising the
    # AuthError-caught-to-None branch rather than a mock.
    from app.auth import sign_session_cookie

    pool = await asyncpg.create_pool(
        dsn="postgresql://u:p@127.0.0.1:1/db", min_size=0, max_size=1, timeout=1, command_timeout=1
    )
    store = AuthStore(pool)
    dep = make_get_current_user_optional(store, _SECRET_KEY)
    token = sign_session_cookie(_SECRET_KEY, 1)

    class _Req:
        cookies = {"session": token}

    result = await dep(_Req())  # must not raise
    assert result is None
    await pool.close()


# ---- HTTP: /signup, /login, /logout, /me -----------------------------------


def _app_with_auth(auth_store: AuthStore, app_env: str = "development") -> FastAPI:
    from app.config import Settings

    settings = Settings(
        handbook_path="unused.pdf",
        database_url=_DSN,
        secret_key=_SECRET_KEY,
        app_env=app_env,
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
        settings=settings,
        auth_store=auth_store,
    )


def _async_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_signup_then_duplicate_signup_conflicts():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-http-signup@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            resp = await client.post("/signup", json={"email": email, "password": "hunter22"})
            assert resp.status_code == 201
            assert resp.json()["email"] == email

            dup = await client.post("/signup", json={"email": email, "password": "hunter22"})
            assert dup.status_code == 409
    finally:
        await _cleanup(pool, email)


async def test_login_sets_cookie_and_wrong_password_is_401():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-http-login@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            await client.post("/signup", json={"email": email, "password": "correct-pw"})

            bad = await client.post("/login", json={"email": email, "password": "wrong-pw"})
            assert bad.status_code == 401
            assert "session" not in bad.cookies

            good = await client.post("/login", json={"email": email, "password": "correct-pw"})
            assert good.status_code == 200
            assert "session" in good.cookies
    finally:
        await _cleanup(pool, email)


async def test_login_cookie_is_secure_unless_app_env_is_development():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-http-secure-cookie@example.com"

    try:
        async with _async_client(_app_with_auth(store, app_env="production")) as client:
            await client.post("/signup", json={"email": email, "password": "correct-pw"})
            resp = await client.post("/login", json={"email": email, "password": "correct-pw"})
            assert "Secure" in resp.headers["set-cookie"]
    finally:
        await _cleanup(pool, email)


async def test_login_then_me_then_logout_then_me():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-9-http-me@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            await client.post("/signup", json={"email": email, "password": "correct-pw"})

            anon = await client.get("/me")
            assert anon.json()["user"] is None

            await client.post("/login", json={"email": email, "password": "correct-pw"})

            logged_in = await client.get("/me")
            assert logged_in.json()["user"]["email"] == email

            await client.post("/logout")

            after_logout = await client.get("/me")
            assert after_logout.json()["user"] is None
    finally:
        await _cleanup(pool, email)


async def test_me_returns_none_for_invalid_cookie_without_500():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    async with _async_client(_app_with_auth(store)) as client:
        client.cookies.set("session", "not-a-real-cookie")
        resp = await client.get("/me")
        assert resp.status_code == 200
        assert resp.json()["user"] is None
    await pool.close()


# ---- regression: anonymous chat unaffected by auth wiring ------------------


async def test_health_live_unaffected_when_auth_is_wired():
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    async with _async_client(_app_with_auth(store)) as client:
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    await pool.close()


async def test_chat_start_unaffected_when_auth_is_wired():
    async with _async_client(
        _app_with_auth(AuthStore(await _pool()))
    ) as client:
        resp = await client.post(
            "/chat/start", data={"question": "hello?", "model_id": "groq_llama31_8b"}
        )
        assert resp.status_code == 200
        assert "startAnswerStream" in resp.text


async def test_create_app_without_auth_store_mounts_no_auth_routes():
    # Existing callers (tests/test_main.py) build create_app() without
    # auth_store/settings at all — confirms that path still works and that
    # /signup simply doesn't exist rather than 500ing.
    embed_client = OllamaClient("http://127.0.0.1:1")
    rag_service = RagService(
        store=PostgresStore(pool=None),
        embed_client=embed_client,
        collection="handbook_chunks",
        top_k=10,
        pdf_path="unused.pdf",
    )
    app = create_app(
        rag_service=rag_service,
        provider_clients={},
        api_keys={},
        embed_client=embed_client,
        store=PostgresStore(pool=None),
        trace_store=TraceStore(pool=None),
    )
    async with _async_client(app) as client:
        resp = await client.post("/signup", json={"email": "x@example.com", "password": "x"})
        assert resp.status_code == 404


# ---- config: SECRET_KEY required at boot, same as HANDBOOK_PATH/DATABASE_URL


def test_load_config_raises_config_error_when_secret_key_missing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HANDBOOK_PATH=/tmp/h.pdf\nDATABASE_URL=postgresql://u:p@localhost/db\n"
    )
    monkeypatch.chdir(tmp_path)
    for key in ("HANDBOOK_PATH", "DATABASE_URL", "SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)

    try:
        load_config()
        assert False, "expected ConfigError for missing SECRET_KEY"
    except ConfigError:
        pass


def test_load_config_succeeds_when_secret_key_present(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HANDBOOK_PATH=/tmp/h.pdf\n"
        "DATABASE_URL=postgresql://u:p@localhost/db\n"
        "SECRET_KEY=test-9-boot-secret\n"
    )
    monkeypatch.chdir(tmp_path)
    for key in ("HANDBOOK_PATH", "DATABASE_URL", "SECRET_KEY", "APP_ENV"):
        monkeypatch.delenv(key, raising=False)

    try:
        settings = load_config()
        assert settings.secret_key == "test-9-boot-secret"
        assert settings.app_env == "production"
    finally:
        # load_config() -> _load_dotenv_into_environ() sets SECRET_KEY into
        # the real os.environ via setdefault(). monkeypatch.delenv here
        # would NOT undo that: since monkeypatch never saw SECRET_KEY set
        # before this point, it would record "test-9-boot-secret" (the
        # value the SUT just wrote) as the "original" to restore at
        # teardown — putting it right back. Popping directly is the only
        # way to actually remove it and keep it gone for later tests.
        os.environ.pop("SECRET_KEY", None)
