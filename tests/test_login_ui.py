"""Login UI tests — Feature Loop ticket 18 (.loop/18/LOOP.md).

Contract-level tests covering:
- GET /me returns {"user": null} when logged out, {"user": {...}} when logged in
- POST /login returns 200 + cookie for good credentials, 401 for bad
- Static grep checks on index.html confirming the form inputs, submit handler,
  and afterLogin() call logic

All HTTP tests use test_auth.py's pool + auth setup pattern; test-18-* email
prefixes ensure no collision with other ticket rows on users.email unique constraint.
"""

import re
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import AuthStore
from app.config import Settings
from app.embed import OllamaClient
from app.main import create_app
from app.rag import RagService
from app.store import PostgresStore
from app.traces import TraceStore

_DSN = "postgresql://handbook:handbook@localhost:5433/handbook"
_SECRET_KEY = "test-18-secret-key-do-not-use-in-prod"
_TEMPLATES_DIR = Path(__file__).parent.parent / "app" / "templates"
_INDEX_HTML = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=0, max_size=2)


async def _cleanup(pool: asyncpg.Pool, *emails: str) -> None:
    for email in emails:
        row = await pool.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if row is not None:
            await pool.execute("DELETE FROM auth_identities WHERE user_id = $1", row["id"])
            await pool.execute("DELETE FROM users WHERE id = $1", row["id"])
    await pool.close()


def _app_with_auth(auth_store: AuthStore, app_env: str = "development") -> FastAPI:
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


# ---- Criterion 1: Anonymous users see login form; GET /me returns null -------


async def test_18_get_me_returns_null_when_not_logged_in():
    """Criterion 1: GET /me returns {"user": null} for anonymous user."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()

    try:
        async with _async_client(_app_with_auth(store)) as client:
            resp = await client.get("/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data == {"user": None}
    finally:
        await pool.close()


async def test_18_get_me_returns_user_when_logged_in():
    """Criterion 1: GET /me returns {"user": {...}} for logged-in user."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-18-me-check@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            # Sign up and log in
            await client.post("/signup", json={"email": email, "password": "testpass123"})
            await client.post("/login", json={"email": email, "password": "testpass123"})

            # Check /me returns the user
            resp = await client.get("/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data["user"] is not None
            assert data["user"]["email"] == email
    finally:
        await _cleanup(pool, email)


# ---- Criterion 2: POST /login success calls afterLogin() exactly once --------


async def test_18_post_login_success_returns_200_and_sets_cookie():
    """Criterion 2: POST /login returns 200 + cookie for good credentials."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-18-login-success@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            # Create account
            await client.post("/signup", json={"email": email, "password": "correct-pw"})

            # Log in with correct credentials
            resp = await client.post("/login", json={"email": email, "password": "correct-pw"})
            assert resp.status_code == 200
            assert "session" in resp.cookies
            data = resp.json()
            assert data["email"] == email
            assert "id" in data
    finally:
        await _cleanup(pool, email)


async def test_18_post_login_failure_returns_401_no_cookie():
    """Criterion 3: POST /login returns 401 for bad credentials, no cookie set."""
    pool = await _pool()
    store = AuthStore(pool)
    await store.ensure_schema()
    email = "test-18-login-failure@example.com"

    try:
        async with _async_client(_app_with_auth(store)) as client:
            # Create account
            await client.post("/signup", json={"email": email, "password": "correct-pw"})

            # Try to log in with wrong password
            resp = await client.post("/login", json={"email": email, "password": "wrong-pw"})
            assert resp.status_code == 401
            assert "session" not in resp.cookies
            data = resp.json()
            assert "error" in data
            assert "invalid email or password" in data["error"].lower()
    finally:
        await _cleanup(pool, email)


# ---- Criterion 1: Form inputs exist (grep check) ----------------------------


def test_18_index_html_has_login_form_email_input():
    """Criterion 1: index.html contains login form with email input."""
    # Look for <input type="email" id="login-email"
    assert re.search(r'<input[^>]*type="email"[^>]*id="login-email"', _INDEX_HTML), \
        "index.html missing email input"


def test_18_index_html_has_login_form_password_input():
    """Criterion 1: index.html contains login form with password input."""
    # Look for <input type="password" id="login-password"
    assert re.search(r'<input[^>]*type="password"[^>]*id="login-password"', _INDEX_HTML), \
        "index.html missing password input"


# ---- Criterion 2: Submit handler calls fetch('/login', ...) (grep check) -----


def test_18_index_html_submit_handler_calls_fetch_login():
    """Criterion 2: Submit handler calls fetch('/login', ...) with POST."""
    # Look for fetch('/login' or fetch("/login" within the login-ui region
    login_region = re.search(
        r'// region: login-ui \(#18\).*?// endregion: login-ui \(#18\)',
        _INDEX_HTML,
        re.DOTALL
    )
    assert login_region, "Could not find login-ui region in index.html"

    region_text = login_region.group(0)
    # Look for fetch with '/login' path
    assert re.search(r'fetch\(["\']\/login["\']', region_text), \
        "login-ui region missing fetch('/login') call"


# ---- Criterion 2: Success branch calls afterLogin() (grep check) -----


def test_18_index_html_success_branch_calls_afterlogin():
    """Criterion 2: On successful login, submit handler calls window.afterLogin()."""
    login_region = re.search(
        r'// region: login-ui \(#18\).*?// endregion: login-ui \(#18\)',
        _INDEX_HTML,
        re.DOTALL
    )
    assert login_region, "Could not find login-ui region in index.html"

    region_text = login_region.group(0)

    # Look for resp.ok branch followed by afterLogin call
    # Pattern: if (resp.ok) ... window.afterLogin()
    # More lenient: find a block that mentions both resp.ok and window.afterLogin
    has_resp_ok = "resp.ok" in region_text
    has_afterlogin_call = "window.afterLogin()" in region_text

    assert has_resp_ok, "login-ui region missing resp.ok check"
    assert has_afterlogin_call, "login-ui region missing window.afterLogin() call"

    # Verify that afterLogin is inside the success branch by checking structure:
    # After fetch, there should be a .ok check, and afterLogin should appear
    # in that block before any failure handling
    success_pattern = r'if\s*\(\s*resp\.ok\s*\).*?window\.afterLogin\(\)'
    assert re.search(success_pattern, region_text, re.DOTALL), \
        "window.afterLogin() not in success branch of fetch response"


# ---- Criterion 3: Failure branch does NOT call afterLogin (grep check) -------


def test_18_index_html_failure_branch_no_afterlogin():
    """Criterion 3: On login failure (401), submit handler does NOT call afterLogin()."""
    login_region = re.search(
        r'// region: login-ui \(#18\).*?// endregion: login-ui \(#18\)',
        _INDEX_HTML,
        re.DOTALL
    )
    assert login_region, "Could not find login-ui region in index.html"

    region_text = login_region.group(0)

    # Extract the if (resp.ok) success branch; extraction failure is a hard failure
    if_resp_ok_match = re.search(
        r'if\s*\(\s*resp\.ok\s*\)\s*\{[^}]*\}',
        region_text,
        re.DOTALL
    )
    assert if_resp_ok_match is not None, \
        "could not locate the if (resp.ok) success block in the login-ui region"

    if_block = if_resp_ok_match.group(0)
    # afterLogin should be in this if block
    assert "window.afterLogin()" in if_block, \
        "window.afterLogin() not in the if (resp.ok) success block"

    # The failure/error handling should NOT call afterLogin.
    # Verify afterLogin only appears exactly once in the entire handler,
    # and it's in the success branch
    afterlogin_count = region_text.count("window.afterLogin()")
    assert afterlogin_count == 1, \
        f"window.afterLogin() appears {afterlogin_count} times, expected exactly 1"

    # Criterion 3 part 2: A failed login must show an error message.
    # The else/failure branch (immediately after if resp.ok) must call showLoginError().
    # Verify the pattern: if (resp.ok) { ... } else { ... showLoginError ... }
    success_to_else_pattern = r'if\s*\(\s*resp\.ok\s*\)\s*\{[^}]*\}\s*else\s*\{.*?showLoginError\s*\('
    failure_shows_error = re.search(
        success_to_else_pattern,
        region_text,
        re.DOTALL
    )
    assert failure_shows_error is not None, \
        "if (resp.ok) {...} else {...showLoginError(...)} structure not found; " \
        "failure branch must call showLoginError(...) to display error on failed login"


# ---- Criterion 4: Form hidden when logged in (grep check) --------------------


def test_18_index_html_form_show_hide_logic():
    """Criterion 4: Login form is shown/hidden based on login state."""
    login_region = re.search(
        r'// region: login-ui \(#18\).*?// endregion: login-ui \(#18\)',
        _INDEX_HTML,
        re.DOTALL
    )
    assert login_region, "Could not find login-ui region in index.html"

    region_text = login_region.group(0)

    # Look for checkLoginStatus or similar function that checks /me and shows/hides overlay
    assert "checkLoginStatus" in region_text, \
        "login-ui region missing checkLoginStatus function"

    # Look for login-overlay element manipulation
    assert "login-overlay" in region_text, \
        "login-ui region missing reference to login-overlay element"

    # Look for classList operations (add/remove 'show')
    assert "classList" in region_text or "display" in region_text, \
        "login-ui region missing element visibility toggle"
