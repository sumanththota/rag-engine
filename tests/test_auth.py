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


# Both patch targets are the network-facing methods authlib's Starlette client inherits. State
# validation (`authorize_access_token` -> get_state_data/_format_state_params) is NEVER patched.
_GOOGLE_FETCH = "authlib.integrations.base_client.async_app.AsyncOAuth2Mixin.fetch_access_token"
_GOOGLE_PARSE = "authlib.integrations.base_client.async_openid.AsyncOpenIDMixin.parse_id_token"


def _google_network_mocks(claims: dict):
    """Patch ONLY the network layers, shaped like the real id_token path.

    fetch_access_token returns a token that CONTAINS an `id_token` and NO `userinfo`; authlib's own
    authorize_access_token then calls parse_id_token(token, nonce=...) and stores the result in
    token["userinfo"]. Returns (stack, mock_fetch, mock_parse); use `with stack:` around the request.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    stack = ExitStack()
    mock_fetch = stack.enter_context(patch(_GOOGLE_FETCH, new_callable=AsyncMock))
    mock_parse = stack.enter_context(patch(_GOOGLE_PARSE, new_callable=AsyncMock))
    mock_fetch.return_value = {"access_token": "test-10-access", "token_type": "Bearer", "id_token": "test-10-id-token"}
    mock_parse.return_value = claims
    return stack, mock_fetch, mock_parse


def _google_never_reach_network():
    """Patch fetch/parse for tests where they must NOT run; assert_not_called() proves state was rejected first."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    stack = ExitStack()
    mock_fetch = stack.enter_context(patch(_GOOGLE_FETCH, new_callable=AsyncMock))
    mock_parse = stack.enter_context(patch(_GOOGLE_PARSE, new_callable=AsyncMock))
    mock_fetch.return_value = {"access_token": "must-not-be-used", "token_type": "Bearer", "id_token": "x"}
    mock_parse.return_value = {"email": "test-10-must-not-be-used@example.com", "email_verified": True, "sub": "x"}
    return stack, mock_fetch, mock_parse


async def _google_login_params(client) -> dict:
    """GET /auth/google/login (no redirect follow) and return the parsed Location query."""
    import urllib.parse

    resp = await client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code in (302, 303, 307, 308), f"Expected redirect, got {resp.status_code}"
    return urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)


async def _google_sign_in(client, claims: dict):
    """Login redirect, then callback with the real state; only network layers mocked.

    Returns (callback_response, mock_fetch, mock_parse, login_query).
    """
    query = await _google_login_params(client)
    stack, mock_fetch, mock_parse = _google_network_mocks(claims)
    with stack:
        resp = await client.get(
            f"/auth/google/callback?code=test-code&state={query['state'][0]}", follow_redirects=False
        )
    return resp, mock_fetch, mock_parse, query


def _auth_session_set_cookies(resp) -> list[str]:
    return [h for h in resp.headers.get_list("set-cookie") if h.startswith("session=")]


async def test_google_oauth_login_redirects_to_google_with_state():
    """Criterion 1 (+ HAZARD 5d): GET /auth/google/login -> Google consent, non-empty state, openid scope, PKCE."""
    import urllib.parse

    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            resp = await client.get("/auth/google/login", follow_redirects=False)
            assert resp.status_code in (302, 303, 307, 308), f"Expected redirect, got {resp.status_code}"

            parsed = urllib.parse.urlparse(resp.headers["location"])
            query = urllib.parse.parse_qs(parsed.query)

            assert parsed.hostname == "accounts.google.com", f"Expected accounts.google.com, got {parsed.hostname}"
            assert query.get("state") and query["state"][0], "state missing/empty in Location query"
            # HAZARD 5: without `openid` authlib generates no nonce and Google returns no id_token.
            assert "openid" in query.get("scope", [""])[0].split(), f"scope must contain openid: {query.get('scope')}"
            assert query.get("nonce") and query["nonce"][0], "nonce missing (only generated with openid scope)"
            assert query.get("code_challenge_method") == ["S256"], "PKCE S256 challenge missing"
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_with_valid_state_and_verified_email():
    """Criteria 2a + 5: real state check, id_token path -> 302 /?login=google, exactly one `session` cookie, /me works."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-valid@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            claims = {"email": email, "email_verified": True, "sub": "test-10-sub-valid"}
            resp, mock_fetch, mock_parse, _ = await _google_sign_in(client, claims)

            assert resp.status_code == 302, f"Expected 302, got {resp.status_code}: {resp.text}"
            assert resp.headers["location"] == "/?login=google"

            session_cookies = _auth_session_set_cookies(resp)
            assert len(session_cookies) == 1, f"Expected exactly 1 `session` Set-Cookie: {resp.headers.get_list('set-cookie')}"

            # State validation ran and handed the stored PKCE verifier + code to the token exchange.
            mock_fetch.assert_awaited_once()
            assert mock_fetch.await_args.kwargs.get("code") == "test-code"
            assert mock_fetch.await_args.kwargs.get("code_verifier"), "PKCE code_verifier from state data not forwarded"
            # userinfo reached the route via authlib's own parse_id_token call.
            mock_parse.assert_awaited_once()

            me_resp = await client.get("/me")
            assert me_resp.status_code == 200
            assert me_resp.json()["user"]["email"] == email
    finally:
        await _cleanup(pool, email)


async def test_google_oauth_callback_parses_id_token_once_via_authlib_with_nonce():
    """HAZARD 4 guard: parse_id_token awaited EXACTLY ONCE (by authlib, not the route) and with the login nonce."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-once@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            claims = {"email": email, "email_verified": True, "sub": "test-10-sub-once"}
            resp, mock_fetch, mock_parse, login_query = await _google_sign_in(client, claims)

            assert resp.status_code == 302, f"Expected 302, got {resp.status_code}: {resp.text}"
            # A route that calls parse_id_token itself makes this 2 (and without a nonce).
            assert mock_parse.await_count == 1, f"parse_id_token awaited {mock_parse.await_count}x, expected exactly 1"
            call = mock_parse.await_args
            nonce = call.kwargs.get("nonce") if "nonce" in call.kwargs else (call.args[1] if len(call.args) > 1 else None)
            assert nonce, f"parse_id_token call carried no nonce: {call}"
            assert nonce == login_query["nonce"][0], "nonce passed to parse_id_token is not the one sent to Google"
            # The token handed to it is the one fetch_access_token returned (has id_token, no userinfo pre-injected).
            token_arg = call.args[0]
            assert token_arg["id_token"] == "test-10-id-token"
            mock_fetch.assert_awaited_once()
    finally:
        await _cleanup(pool, email)


async def test_google_oauth_callback_rejects_missing_state():
    """Criterion 2b: no state -> 4xx JSON, token exchange never attempted, no auth cookie."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            stack, mock_fetch, mock_parse = _google_never_reach_network()
            with stack:
                resp = await client.get("/auth/google/callback?code=test-code", follow_redirects=False)

            assert 400 <= resp.status_code < 500, f"Expected 4xx for missing state, got {resp.status_code}"
            assert resp.json() == {"error": "authorization failed"}
            # If the state check were removed these would have been called (or hit Google's live endpoint).
            mock_fetch.assert_not_called()
            mock_parse.assert_not_called()
            assert _auth_session_set_cookies(resp) == [], "auth cookie must not be set on state failure"
            me = await client.get("/me")
            assert me.json()["user"] is None
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_rejects_mismatched_state():
    """Criterion 2b: state that does not match the login's -> 4xx JSON, token exchange never attempted, no cookie."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            query = await _google_login_params(client)  # a genuine state now exists in the session
            assert query["state"][0] != "test-10-wrong-state"

            stack, mock_fetch, mock_parse = _google_never_reach_network()
            with stack:
                resp = await client.get(
                    "/auth/google/callback?code=test-code&state=test-10-wrong-state", follow_redirects=False
                )

            assert 400 <= resp.status_code < 500, f"Expected 4xx for mismatched state, got {resp.status_code}"
            assert resp.json() == {"error": "authorization failed"}
            mock_fetch.assert_not_called()
            mock_parse.assert_not_called()
            assert _auth_session_set_cookies(resp) == [], "auth cookie must not be set on state failure"
            me = await client.get("/me")
            assert me.json()["user"] is None
    finally:
        await _cleanup(pool)


async def test_google_oauth_callback_rejects_unverified_email():
    """Criterion 2c: email_verified=false -> 4xx, no auth cookie, no user row created."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-unverified@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            claims = {"email": email, "email_verified": False, "sub": "test-10-sub-unverified"}
            resp, mock_fetch, mock_parse, _ = await _google_sign_in(client, claims)

            assert 400 <= resp.status_code < 500, f"Expected 4xx for unverified email, got {resp.status_code}"
            assert resp.json() == {"error": "email not verified"}
            mock_parse.assert_awaited_once()  # got past state validation and id_token parsing; rejected on email_verified
            assert _auth_session_set_cookies(resp) == [], "auth cookie must not be set for unverified email"
            assert (await client.get("/me")).json()["user"] is None

        rows = await pool.fetch("SELECT id FROM users WHERE email = $1", email)
        assert len(rows) == 0, f"no user may be created for unverified email, found {len(rows)}"
    finally:
        await _cleanup(pool, email)


async def test_google_login_adds_auth_identity_to_existing_password_user():
    """Criterion 3: POST /signup, then mocked-Google callback (same email) -> one users row, password+google identities."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-merge@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            resp = await client.post("/signup", json={"email": email, "password": "password123"})
            assert resp.status_code == 201
            user_id = resp.json()["id"]

            claims = {"email": email, "email_verified": True, "sub": "test-10-sub-merge"}
            callback_resp, _, mock_parse, _ = await _google_sign_in(client, claims)
            assert callback_resp.status_code == 302, f"Expected 302, got {callback_resp.status_code}"
            mock_parse.assert_awaited_once()

        user_rows = await pool.fetch("SELECT id FROM users WHERE email = $1", email)
        assert len(user_rows) == 1, "no duplicate user may be created"
        assert user_rows[0]["id"] == user_id
        providers = sorted(
            r["provider"] for r in await pool.fetch("SELECT provider FROM auth_identities WHERE user_id = $1", user_id)
        )
        assert providers == ["google", "password"], f"expected google+password on same user, got {providers}"
    finally:
        await _cleanup(pool, email)


async def test_password_signup_rejected_for_google_only_email():
    """Criterion 4: Google-only user made via the mocked callback; POST /signup -> 409 naming Google, no password identity."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-10-google-only@example.com"

    try:
        app = _app_with_google_oauth(store)
        async with _async_client(app) as client:
            claims = {"email": email, "email_verified": True, "sub": "test-10-sub-only"}
            callback_resp, _, _, _ = await _google_sign_in(client, claims)
            assert callback_resp.status_code == 302, f"Google-only user not created: {callback_resp.status_code}"

            resp = await client.post("/signup", json={"email": email, "password": "newpassword"})
            assert resp.status_code == 409
            error_msg = resp.json().get("error", "").lower()
            assert "google" in error_msg, f"Error message should mention Google: {error_msg}"
            assert error_msg != "email already registered"

        row = await pool.fetchrow(
            """
            SELECT ai.password_hash FROM auth_identities ai
            WHERE ai.provider = 'password' AND ai.user_id = (SELECT id FROM users WHERE email = $1)
            """,
            email,
        )
        assert row is None, "signup attempt must not have created a password identity"
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
