"""Password auth: signup, login, logout, session cookie.

ADR-0003 (docs/adr/0003-optional-cookie-auth-split-identities.md): auth is
additive, not gating — every route resolves `User | None` via
get_current_user_optional, which never raises, so anonymous chat keeps
working exactly as today. Identity is split into `users` and
`auth_identities` (one row per login method) so a password account and a
future Google account sharing the same email can later link into one
`users` row.

Same module shape as app/store.py's PostgresStore and app/traces.py's
TraceStore: one typed exception, an ensure_schema(), Postgres errors caught
narrowly and re-raised as that typed exception.
"""

import argon2
from argon2.exceptions import InvalidHash, VerifyMismatchError
import asyncpg
from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, field_validator

_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)

SESSION_COOKIE_NAME = "session"
_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # sliding 30-day idle expiry
_COOKIE_SALT = "rag-engine.session"

_hasher = argon2.PasswordHasher(type=argon2.Type.ID)


class AuthError(Exception):
    """Raised for any Postgres failure while creating the auth schema or
    reading/writing a users/auth_identities row, and for a signup whose
    email is already registered."""


class User(BaseModel):
    id: int
    email: str


class SignupRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("email is required")
        return v

    @field_validator("password")
    @classmethod
    def _require_password(cls, v: str) -> str:
        if not v:
            raise ValueError("password is required")
        return v


class LoginRequest(SignupRequest):
    """Same shape as SignupRequest; kept as a distinct type so the two
    routes' request bodies can diverge later without a shared rename."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    return True


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=_COOKIE_SALT)


def sign_session_cookie(secret_key: str, user_id: int) -> str:
    return _serializer(secret_key).dumps({"user_id": user_id})


def verify_session_cookie(secret_key: str, cookie_value: str) -> int | None:
    """Returns the signed-in user_id, or None for a missing/tampered/expired
    cookie. Never raises — get_current_user_optional below depends on that."""
    try:
        data = _serializer(secret_key).loads(cookie_value, max_age=_SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None
    user_id = data.get("user_id") if isinstance(data, dict) else None
    return user_id if isinstance(user_id, int) else None


def set_session_cookie(response: Response, secret_key: str, user_id: int, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        sign_session_cookie(secret_key, user_id),
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, samesite="lax")


class AuthStore:
    """Postgres-backed users + auth_identities store."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id bigserial PRIMARY KEY,
                        email text NOT NULL UNIQUE,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_identities (
                        id bigserial PRIMARY KEY,
                        user_id bigint NOT NULL REFERENCES users(id),
                        provider text NOT NULL,
                        provider_uid text NOT NULL,
                        password_hash text,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (provider, provider_uid)
                    )
                    """
                )
        except _DB_ERRORS as e:
            raise AuthError(f"ensure_schema failed: {e}") from e

    async def create_user_with_password(self, email: str, password: str) -> User:
        password_hash = hash_password(password)
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "INSERT INTO users (email) VALUES ($1) RETURNING id, email",
                        email,
                    )
                    await conn.execute(
                        """
                        INSERT INTO auth_identities (user_id, provider, provider_uid, password_hash)
                        VALUES ($1, 'password', $2, $3)
                        """,
                        row["id"],
                        email,
                        password_hash,
                    )
        except asyncpg.UniqueViolationError as e:
            raise AuthError(f"email already registered: {email!r}") from e
        except _DB_ERRORS as e:
            raise AuthError(f"create_user_with_password failed for email={email!r}: {e}") from e
        return User(id=row["id"], email=row["email"])

    async def authenticate_password(self, email: str, password: str) -> User | None:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT u.id, u.email, ai.password_hash
                    FROM users u
                    JOIN auth_identities ai ON ai.user_id = u.id
                    WHERE u.email = $1 AND ai.provider = 'password'
                    """,
                    email,
                )
        except _DB_ERRORS as e:
            raise AuthError(f"authenticate_password failed for email={email!r}: {e}") from e
        if row is None or row["password_hash"] is None:
            return None
        if not verify_password(row["password_hash"], password):
            return None
        return User(id=row["id"], email=row["email"])

    async def get_user(self, user_id: int) -> User | None:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id, email FROM users WHERE id = $1", user_id)
        except _DB_ERRORS as e:
            raise AuthError(f"get_user failed for user_id={user_id}: {e}") from e
        if row is None:
            return None
        return User(id=row["id"], email=row["email"])

    async def find_or_create_user_with_google_identity(self, email: str, provider_uid: str) -> User:
        """Find an existing user by email or create a new one, then ensure a google
        auth_identity row exists. Returns the user if successful.

        On success: user exists (possibly new) with a google auth_identity row.
        On failure: raises AuthError (DB error, etc.).

        Criterion 3 behavior: if the email already has a password identity, this
        still succeeds — it adds the google identity to the existing user. The
        user is not duplicated.
        """
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Try to find existing user by email
                    user_row = await conn.fetchrow("SELECT id, email FROM users WHERE email = $1", email)

                    if user_row is None:
                        # Create new user
                        user_row = await conn.fetchrow(
                            "INSERT INTO users (email) VALUES ($1) RETURNING id, email",
                            email,
                        )

                    user_id = user_row["id"]

                    # Insert or ignore the google identity (UPSERT pattern)
                    # Try to insert; if it already exists, silently ignore
                    try:
                        await conn.execute(
                            """
                            INSERT INTO auth_identities (user_id, provider, provider_uid, password_hash)
                            VALUES ($1, 'google', $2, NULL)
                            """,
                            user_id,
                            provider_uid,
                        )
                    except asyncpg.UniqueViolationError:
                        # google identity already exists for this user, that's fine
                        pass

                    return User(id=user_row["id"], email=user_row["email"])
        except _DB_ERRORS as e:
            raise AuthError(f"find_or_create_user_with_google_identity failed for email={email!r}: {e}") from e

    async def is_google_only_user(self, email: str) -> bool:
        """Returns True if the email exists as a user AND has a google auth_identity
        AND does NOT have a password auth_identity (password_hash is NULL for all rows).

        Criterion 4: used to detect when a signup attempt targets a Google-only email.
        """
        try:
            async with self._pool.acquire() as conn:
                # Check if user exists
                user_row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
                if user_row is None:
                    return False

                user_id = user_row["id"]

                # Check if user has google identity
                google_row = await conn.fetchrow(
                    "SELECT id FROM auth_identities WHERE user_id = $1 AND provider = 'google'",
                    user_id,
                )
                if google_row is None:
                    return False

                # Check if user has password identity (password_hash is NOT NULL)
                password_row = await conn.fetchrow(
                    "SELECT id FROM auth_identities WHERE user_id = $1 AND provider = 'password' AND password_hash IS NOT NULL",
                    user_id,
                )

                # Return True only if google exists AND password does NOT exist
                return password_row is None
        except _DB_ERRORS as e:
            raise AuthError(f"is_google_only_user failed for email={email!r}: {e}") from e


def make_get_current_user_optional(auth_store: AuthStore, secret_key: str):
    """Builds the `Depends(get_current_user_optional)` dependency for a
    specific AuthStore/secret_key pair (main.py's other dependencies are
    wired the same closure way — see create_app()). Never raises: a missing
    cookie, a tampered/expired one, or a DB failure while loading the user
    all resolve to None rather than propagating, so an anonymous request
    is never turned into a 500 by this dependency alone (ADR-0003)."""

    async def get_current_user_optional(request: Request) -> User | None:
        cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie_value:
            return None
        user_id = verify_session_cookie(secret_key, cookie_value)
        if user_id is None:
            return None
        try:
            return await auth_store.get_user(user_id)
        except AuthError:
            return None

    return get_current_user_optional
