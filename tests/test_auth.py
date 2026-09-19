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

import base64
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


def _flip_a_real_bit(b64url_segment: str) -> str:
    """Decodes a base64url (unpadded) itsdangerous token segment, flips one
    bit in its first decoded byte, and re-encodes. Unlike mutating a
    character in the encoded text directly — which can land on a base64
    "slack" bit that decodes back to the *same* byte roughly 1 time in 4
    (itsdangerous's digest is base64-encoded, and not every encoded bit
    maps to real digest data) — this is guaranteed to change the decoded
    bytes every time, so the resulting signature is guaranteed invalid."""
    padded = b64url_segment + "=" * (-len(b64url_segment) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    raw[0] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def test_verify_session_cookie_rejects_tampered_value():
    from app.auth import sign_session_cookie

    token = sign_session_cookie(_SECRET_KEY, 42)
    payload, timestamp, signature = token.split(".")
    tampered = ".".join([payload, timestamp, _flip_a_real_bit(signature)])

    assert tampered != token
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


# ---- Google OAuth (ticket 10) -----------------------------------------------


def _app_with_google_oauth(auth_store: AuthStore, app_env: str = "development") -> FastAPI:
    """Create app with Google OAuth configured (routes mount only if client_id/secret/redirect_uri are set)."""
    from app.config import Settings

    settings = Settings(
        handbook_path="unused.pdf",
        database_url=_DSN,
        secret_key=_SECRET_KEY,
        app_env=app_env,
        google_client_id="test-google-client-id",
        google_client_secret="test-google-client-secret",
        google_redirect_uri="http://testserver/auth/google/callback",
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


async def test_google_oauth_login_redirects_to_google_with_state():
    """Criterion 1: GET /auth/google/login redirects to Google consent screen with state."""
    import urllib.parse

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            resp = await client.get("/auth/google/login", follow_redirects=False)

            # Should be a redirect (3xx)
            assert resp.status_code in (302, 303, 307, 308), f"Expected redirect, got {resp.status_code}"

            # Should have Location header pointing to Google
            assert "location" in resp.headers or "Location" in resp.headers
            location = resp.headers.get("location") or resp.headers.get("Location")

            # Parse the URL to extract components
            parsed = urllib.parse.urlparse(location)
            query_params = urllib.parse.parse_qs(parsed.query)

            # Criterion 1: assert host is exactly accounts.google.com
            assert parsed.hostname == "accounts.google.com", f"Expected host accounts.google.com, got {parsed.hostname}"

            # Criterion 1: assert state is present and non-empty
            assert "state" in query_params, "state parameter missing from Location query string"
            state_values = query_params["state"]
            assert len(state_values) > 0, "state parameter is empty"
            assert len(state_values[0]) > 0, "state value is empty string"
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_with_valid_state_and_verified_email():
    """Criterion 2a/5: callback with valid state and verified email -> 302 to /?login=google with session cookie."""
    import urllib.parse
    from unittest.mock import AsyncMock, patch

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-valid@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            # First, visit /auth/google/login to get a state cookie set by SessionMiddleware
            login_resp = await client.get("/auth/google/login", follow_redirects=False)
            # Extract state from the Location header to simulate the authorization code flow
            location = login_resp.headers.get("location") or login_resp.headers.get("Location")
            parsed = urllib.parse.urlparse(location)
            query_params = urllib.parse.parse_qs(parsed.query)
            state = query_params["state"][0]  # Get the state value from the redirect

            # Mock only the network layers (fetch_access_token and parse_id_token)
            # authorize_access_token validates state and is NOT mocked
            with patch("authlib.integrations.base_client.async_app.AsyncOAuth2Mixin.fetch_access_token") as mock_fetch, \
                 patch("authlib.integrations.base_client.async_openid.AsyncOpenIDMixin.parse_id_token") as mock_parse:

                # Mock fetch_access_token to return a token dict with userinfo
                mock_fetch.return_value = {
                    "access_token": "test-token",
                    "token_type": "Bearer",
                    "userinfo": {
                        "email": email,
                        "email_verified": True,
                        "sub": "google-123",
                    }
                }

                # Mock parse_id_token (called by authorize_access_token internally)
                mock_parse.return_value = {
                    "email": email,
                    "email_verified": True,
                    "sub": "google-123",
                }

                # Invoke callback with the state code
                callback_resp = await client.get(
                    f"/auth/google/callback?code=test-code&state={state}",
                    follow_redirects=False
                )

                # Criterion 5: assert 302
                assert callback_resp.status_code == 302, f"Expected 302, got {callback_resp.status_code}"

                # Criterion 5: assert Location == '/?login=google'
                callback_location = callback_resp.headers.get("location") or callback_resp.headers.get("Location")
                assert callback_location == "/?login=google", f"Expected /?login=google, got {callback_location}"

                # Criterion 2a & 5: assert EXACTLY ONE Set-Cookie named 'session' on this response
                set_cookie_headers = callback_resp.headers.get_list("set-cookie")
                session_cookies = [h for h in set_cookie_headers if h.startswith("session=")]
                assert len(session_cookies) == 1, f"Expected exactly 1 Set-Cookie named session, got {len(session_cookies)}: {set_cookie_headers}"

                # Criterion 2a: assert the session cookie authenticates at GET /me
                me_resp = await client.get("/me")
                assert me_resp.status_code == 200
                me_data = me_resp.json()
                assert me_data["user"] is not None
                assert me_data["user"]["email"] == email
    finally:
        await _cleanup(pool, email)


async def test_google_oauth_callback_rejects_missing_state():
    """Criterion 2b: missing state -> 4xx, no auth cookie."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            # Request callback without state parameter (this should fail state validation)
            resp = await client.get("/auth/google/callback?code=test-code", follow_redirects=False)

            # Should be a client error (not 500)
            assert 400 <= resp.status_code < 500, f"Expected 4xx for missing state, got {resp.status_code}"

            # Should NOT have set session cookie
            set_cookie_headers = resp.headers.get_list("set-cookie")
            session_cookies = [h for h in set_cookie_headers if h.startswith("session=") and "Max-Age=2592000" in h]
            assert len(session_cookies) == 0, f"Should not have set auth session cookie, got: {set_cookie_headers}"
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_rejects_mismatched_state():
    """Criterion 2b: mismatched state -> 4xx, no auth cookie."""
    import urllib.parse

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            # Visit /auth/google/login to get a valid state
            login_resp = await client.get("/auth/google/login", follow_redirects=False)

            # Request callback with wrong state
            resp = await client.get("/auth/google/callback?code=test-code&state=wrong-state", follow_redirects=False)

            # Should be a client error (not 500)
            assert 400 <= resp.status_code < 500, f"Expected 4xx for mismatched state, got {resp.status_code}"

            # Should NOT have set session cookie
            set_cookie_headers = resp.headers.get_list("set-cookie")
            session_cookies = [h for h in set_cookie_headers if h.startswith("session=") and "Max-Age=2592000" in h]
            assert len(session_cookies) == 0, f"Should not have set auth session cookie, got: {set_cookie_headers}"
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_rejects_unverified_email():
    """Criterion 2c: email_verified=false -> 4xx, no auth cookie, no user row created."""
    import urllib.parse
    from unittest.mock import AsyncMock, patch

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-unverified@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            # First, visit /auth/google/login to get a state cookie
            login_resp = await client.get("/auth/google/login", follow_redirects=False)
            location = login_resp.headers.get("location") or login_resp.headers.get("Location")
            parsed = urllib.parse.urlparse(location)
            query_params = urllib.parse.parse_qs(parsed.query)
            state = query_params["state"][0]

            # Mock only fetch_access_token and parse_id_token to return unverified email
            with patch("authlib.integrations.base_client.async_app.AsyncOAuth2Mixin.fetch_access_token") as mock_fetch, \
                 patch("authlib.integrations.base_client.async_openid.AsyncOpenIDMixin.parse_id_token") as mock_parse:

                # Return unverified email
                mock_fetch.return_value = {
                    "access_token": "test-token",
                    "token_type": "Bearer",
                    "userinfo": {
                        "email": email,
                        "email_verified": False,  # KEY: unverified
                        "sub": "google-456",
                    }
                }

                mock_parse.return_value = {
                    "email": email,
                    "email_verified": False,  # KEY: unverified
                    "sub": "google-456",
                }

                # Invoke callback with valid state but unverified email
                resp = await client.get(
                    f"/auth/google/callback?code=test-code&state={state}",
                    follow_redirects=False
                )

                # Should be 4xx
                assert 400 <= resp.status_code < 500, f"Expected 4xx for unverified email, got {resp.status_code}"

                # Should NOT have set session cookie
                set_cookie_headers = resp.headers.get_list("set-cookie")
                session_cookies = [h for h in set_cookie_headers if h.startswith("session=") and "Max-Age=2592000" in h]
                assert len(session_cookies) == 0, f"Should not have set auth session cookie, got: {set_cookie_headers}"

            # Verify no user was created for unverified email
            async with pool.acquire() as conn:
                user_rows = await conn.fetch("SELECT id FROM users WHERE email = $1", email)
                assert len(user_rows) == 0, f"Should not create user for unverified email, but found {len(user_rows)} rows"
    finally:
        await _cleanup(pool, email)


async def test_google_login_adds_auth_identity_to_existing_password_user():
    """Criterion 3: signup with password, then Google callback -> same user with 2 providers, no duplicate user created."""
    import urllib.parse
    from unittest.mock import patch

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-merge@example.com"

    try:
        app = _app_with_google_oauth(store)

        # First, create a user with password signup
        async with _async_client(app) as client:
            resp = await client.post("/signup", json={"email": email, "password": "password123"})
            assert resp.status_code == 201
            user1_id = resp.json()["id"]

            # Verify user was created with password provider
            async with pool.acquire() as conn:
                user_rows = await conn.fetch("SELECT id FROM users WHERE email = $1", email)
                assert len(user_rows) == 1, "Should have exactly 1 user for this email"
                assert user_rows[0]["id"] == user1_id

                identity_rows = await conn.fetch(
                    "SELECT provider FROM auth_identities WHERE user_id = $1 ORDER BY provider",
                    user1_id,
                )
                assert len(identity_rows) == 1
                assert identity_rows[0]["provider"] == "password"

            # Now simulate Google login via the HTTP callback (criterion 3 requires HTTP, not direct store call)
            # First, visit /auth/google/login to get a state
            login_resp = await client.get("/auth/google/login", follow_redirects=False)
            location = login_resp.headers.get("location") or login_resp.headers.get("Location")
            parsed = urllib.parse.urlparse(location)
            query_params = urllib.parse.parse_qs(parsed.query)
            state = query_params["state"][0]

            # Mock fetch_access_token and parse_id_token to return the same email
            with patch("authlib.integrations.base_client.async_app.AsyncOAuth2Mixin.fetch_access_token") as mock_fetch, \
                 patch("authlib.integrations.base_client.async_openid.AsyncOpenIDMixin.parse_id_token") as mock_parse:

                mock_fetch.return_value = {
                    "access_token": "test-token",
                    "token_type": "Bearer",
                    "userinfo": {
                        "email": email,
                        "email_verified": True,
                        "sub": "google-merge-123",
                    }
                }

                mock_parse.return_value = {
                    "email": email,
                    "email_verified": True,
                    "sub": "google-merge-123",
                }

                # Invoke callback with the existing password user's email
                callback_resp = await client.get(
                    f"/auth/google/callback?code=test-code&state={state}",
                    follow_redirects=False
                )
                assert callback_resp.status_code == 302, f"Expected 302, got {callback_resp.status_code}"

            # Verify still only 1 user (no duplicate) and now has 2 providers
            async with pool.acquire() as conn:
                user_rows = await conn.fetch("SELECT id FROM users WHERE email = $1", email)
                assert len(user_rows) == 1, "Should still have exactly 1 user (no duplicate created)"

                identity_rows = await conn.fetch(
                    "SELECT provider FROM auth_identities WHERE user_id = $1 ORDER BY provider",
                    user1_id,
                )
                assert len(identity_rows) == 2, f"Should have 2 providers, got {len(identity_rows)}"
                providers = [row["provider"] for row in identity_rows]
                assert "password" in providers, "Should still have password provider"
                assert "google" in providers, "Should now have google provider"
    finally:
        await _cleanup(pool, email)


async def test_password_signup_rejected_for_google_only_email():
    """Criterion 4: Password signup with Google-only email rejected with clear message."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-only@example.com"

    try:
        app = _app_with_google_oauth(store)

        # Create a Google-only user (no password)
        await store.find_or_create_user_with_google_identity(email, email)

        # Now try to sign up with password to the same email
        async with _async_client(app) as client:
            resp = await client.post("/signup", json={"email": email, "password": "newpassword"})

            # Should be 409 conflict
            assert resp.status_code == 409

            # Error message should mention Google
            error_msg = resp.json().get("error", "").lower()
            assert "google" in error_msg, f"Error message should mention Google: {error_msg}"

        # Verify password_hash is still NULL
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ai.password_hash FROM auth_identities ai
                WHERE ai.provider = 'password' AND ai.user_id = (
                    SELECT id FROM users WHERE email = $1
                )
                """,
                email,
            )
            # There should be no password auth_identity
            assert row is None
    finally:
        await _cleanup(pool, email)




async def test_google_oauth_disabled_when_client_id_unset():
    """Google routes should not mount if client_id is not set."""
    from app.config import Settings

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        # Create app WITH auth_store but WITHOUT Google creds
        settings = Settings(
            handbook_path="unused.pdf",
            database_url=_DSN,
            secret_key=_SECRET_KEY,
            app_env="development",
            # google_client_id and others default to ""
        )
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
            settings=settings,
            auth_store=store,
        )

        async with _async_client(app) as client:
            # /auth/google/login should not exist (404)
            resp = await client.get("/auth/google/login")
            assert resp.status_code == 404
    finally:
        await _cleanup(pool)


async def test_google_oauth_find_or_create_handles_duplicate_google_identity():
    """Verify find_or_create_user_with_google_identity handles duplicate calls gracefully."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-duplicate-google@example.com"

    try:
        # First call: creates user + google identity
        user1 = await store.find_or_create_user_with_google_identity(email, email)
        assert user1.email == email

        # Second call with same email: should return same user (idempotent)
        user2 = await store.find_or_create_user_with_google_identity(email, email)
        assert user2.id == user1.id
        assert user2.email == email

        # Verify only one user exists
        async with pool.acquire() as conn:
            user_rows = await conn.fetch("SELECT id FROM users WHERE email = $1", email)
            assert len(user_rows) == 1

            # Verify only one google identity
            google_rows = await conn.fetch(
                "SELECT id FROM auth_identities WHERE user_id = $1 AND provider = 'google'",
                user1.id,
            )
            assert len(google_rows) == 1
    finally:
        await _cleanup(pool, email)


async def test_google_oauth_is_google_only_user_checks_password():
    """Verify is_google_only_user correctly distinguishes users."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    google_only_email = "test-10-google-only-check@example.com"
    password_user_email = "test-10-password-user@example.com"
    both_email = "test-10-both@example.com"

    try:
        # Google-only user
        await store.find_or_create_user_with_google_identity(google_only_email, google_only_email)
        assert await store.is_google_only_user(google_only_email)

        # Password user
        await store.create_user_with_password(password_user_email, "password123")
        assert not await store.is_google_only_user(password_user_email)

        # Both: create password first, then add google
        await store.create_user_with_password(both_email, "password123")
        user = await store.find_or_create_user_with_google_identity(both_email, both_email)
        assert not await store.is_google_only_user(both_email), "Should not be google-only if password exists"

        # Non-existent user
        assert not await store.is_google_only_user("test-10-nonexistent@example.com")
    finally:
        await _cleanup(pool, google_only_email, password_user_email, both_email)


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
