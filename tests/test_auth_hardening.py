"""Hardening tests for auth: argon2id hashing, legacy upgrade-on-login,
failed-login rate limiting, SECRET_KEY strength, JWT user_id claim binding.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import get_session_maker
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    """Rate-limit counters live in a process-wide dict; isolate each test."""
    import app.core.rate_limit as rl

    rl._failed_attempts.clear()
    yield
    rl._failed_attempts.clear()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _legacy_sha256_hash(password: str) -> str:
    """Produce a hash in the legacy ``salt$hex_sha256(salt+password)`` format."""
    import hashlib
    import secrets

    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


async def _create_user(email: str, hashed_password: str) -> User:
    """Insert a user row directly (bypassing the register endpoint's hashing)."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        user = User(
            id=uuid4(),
            email=email,
            hashed_password=hashed_password,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _read_user(email: str) -> User:
    session_maker = get_session_maker()
    async with session_maker() as session:
        return (await session.execute(
            select(User).where(User.email == email)
        )).scalar_one()


def _register(client: TestClient, email: str, password: str):
    return client.post("/api/v1/auth/register", json={
        "email": email,
        "password": password,
        "full_name": "Hardening Test User",
    })


async def test_login_with_argon2_registered_user(client):
    """A user hashed with the default register flow (argon2) can log in."""
    email = f"argon2_login_{uuid4()}@example.com"
    r = _register(client, email, "S3curePass!!")
    assert r.status_code == 201, r.text

    r = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "S3curePass!!",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    # The token authenticates against /me.
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email

    # Registration stored an argon2id hash, not the legacy format.
    user = await _read_user(email)
    assert user.hashed_password.startswith("$argon2id$")


async def test_legacy_sha256_hash_upgrade_on_login(client):
    """Legacy salt$sha256 hashes verify and are rehashed to argon2id on login."""
    email = f"legacy_upgrade_{uuid4()}@example.com"
    password = "OldPassword123"
    await _create_user(email, _legacy_sha256_hash(password))
    stored_before = (await _read_user(email)).hashed_password
    assert stored_before.startswith("$argon2") is False  # legacy format

    r = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": password,
    })
    assert r.status_code == 200, r.text

    # Fresh session read: the stored hash was upgraded in place.
    stored_after = (await _read_user(email)).hashed_password
    assert stored_after.startswith("$argon2id$")
    assert verify_password(password, stored_after)


async def test_failed_login_rate_limit(client):
    """5 failed logins for one (IP, email) -> 429 with Retry-After."""
    email = f"ratelimited_{uuid4()}@example.com"
    password = "Whatever123"

    await _create_user(email, hash_password(password))

    # 5 failed attempts -> 401 each.
    for i in range(5):
        r = client.post("/api/v1/auth/login", data={
            "username": email,
            "password": "definitely-not-the-password",
        })
        assert r.status_code == 401, (i, r.status_code, r.text)

    # 6th attempt is blocked even before its password is checked.
    blocked = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "definitely-not-the-password",
    })
    assert blocked.status_code == 429, blocked.text
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) >= 1

    # A *correct* password is also rejected while the key is blocked
    # (blocking happens before verification).
    still_blocked = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": password,
    })
    assert still_blocked.status_code == 429, still_blocked.text


async def test_successful_login_resets_rate_limit(client):
    """Success resets the counter: 4 failures + success + 1 failure = 401, not 429."""
    email = f"reset_on_success_{uuid4()}@example.com"
    password = "CorrectHorse1"
    await _create_user(email, hash_password(password))

    for _ in range(4):
        r = client.post("/api/v1/auth/login", data={
            "username": email,
            "password": "wrong",
        })
        assert r.status_code == 401, r.text

    ok = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": password,
    })
    assert ok.status_code == 200, ok.text

    # Counter was reset, so one more failure is 401 rather than 429.
    r = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "wrong",
    })
    assert r.status_code == 401, r.text


async def test_rate_limit_is_per_email(client):
    """A blocked email does not block a different email from the same IP."""
    blocked_email = f"blocked_{uuid4()}@example.com"
    other_email = f"other_{uuid4()}@example.com"
    await _create_user(other_email, hash_password("OtherPass123"))

    for _ in range(5):
        r = client.post("/api/v1/auth/login", data={
            "username": blocked_email,
            "password": "wrong",
        })
        assert r.status_code == 401, r.text
    assert client.post(
        "/api/v1/auth/login", data={"username": blocked_email, "password": "wrong"}
    ).status_code == 429

    r = client.post("/api/v1/auth/login", data={
        "username": other_email,
        "password": "OtherPass123",
    })
    assert r.status_code == 200, r.text


def test_secret_key_minimum_length():
    """Settings rejects SECRET_KEY shorter than 32 chars at startup."""
    from app.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, SECRET_KEY="too-short-secret")
    assert "SECRET_KEY must be at least 32 characters" in str(exc_info.value)

    # 31 chars is still rejected; exactly 32 passes.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SECRET_KEY="x" * 31, OPENROUTER_API_KEY="k")
    Settings(_env_file=None, SECRET_KEY="x" * 32, OPENROUTER_API_KEY="k")


async def test_token_with_mismatched_user_id_claim_is_rejected(client):
    """A token whose user_id claim doesn't match the user record -> 401."""
    email = f"claim_binding_{uuid4()}@example.com"
    r = _register(client, email, "S3curePass!!")
    assert r.status_code == 201, r.text

    # Craft a token with a wrong user_id (some other user's id).
    wrong_token = create_access_token(
        data={"sub": email, "user_id": str(uuid4())}
    )
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {wrong_token}"},
    )
    assert resp.status_code == 401, resp.text

    # A token missing the user_id claim entirely is rejected too.
    no_claim = create_access_token(data={"sub": email})
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {no_claim}"},
    )
    assert resp.status_code == 401, resp.text

    # Sanity: a properly issued token still authenticates.
    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "S3curePass!!",
    })
    assert login.status_code == 200, login.text
    user = await _read_user(email)
    good_token = create_access_token(
        data={"sub": email, "user_id": str(user.id)}
    )
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {good_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == email
