"""Password hashing, JWT creation/verification, and login rate limiting helpers.

Migration path (SHA-256+salt -> Argon2id):
- New passwords (registration, seeding) are hashed with Argon2id via
  argon2-cffi's ``PasswordHasher`` using its default parameters.
- Legacy stored hashes use the old ``salt$hash`` format: ``salt`` is 32 hex
  chars (16 random bytes) and ``hash`` is the SHA-256 hex digest of
  ``salt + password`` (one round, unsalted-key derivation) as a 64-char hex
  string.
- ``verify_password`` detects that legacy format and verifies against it;
  on a successful legacy verification the caller should persist an Argon2id
  rehash (see ``rehash_if_legacy`` / ``upgrade_legacy_hash``), i.e. the
  classic upgrade-on-login pattern. Hashes migrate in place: no SQL schema
  change is needed since ``hashed_password`` is a plain string column and
  Argon2id hashes are longer but still just strings.
"""
from datetime import datetime, timedelta
from typing import Optional
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()

_argon2Hasher = PasswordHasher()

# Length of the legacy scheme's SHA-256 hex digest.
_LEGACY_HASH_HEX_LEN = 64


def _is_legacy_hash(hashed_password: str) -> bool:
    """True if the stored hash uses the legacy ``salt$hex_sha256`` format."""
    parts = hashed_password.split("$")
    if len(parts) != 2:
        return False
    salt, stored_hash = parts
    if not salt or len(stored_hash) != _LEGACY_HASH_HEX_LEN:
        return False
    try:
        int(stored_hash, 16)
    except ValueError:
        return False
    return True


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its stored hash (argon2id or legacy)."""
    if _is_legacy_hash(hashed_password):
        salt, stored_hash = hashed_password.split("$")
        digest = hashlib.sha256((salt + plain_password).encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, stored_hash)
    try:
        return _argon2Hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(hashed_password: str) -> bool:
    """True if the stored hash is legacy and should be upgraded to argon2id."""
    return _is_legacy_hash(hashed_password)


def hash_password(password: str) -> str:
    """Hash a password with Argon2id (argon2-cffi default parameters)."""
    return _argon2Hasher.hash(password)


def get_password_hash(password: str) -> str:
    """Backwards-compatible alias for :func:`hash_password`."""
    return hash_password(password)


def upgrade_legacy_hash(plain_password: str, hashed_password: str) -> Optional[str]:
    """Return a fresh Argon2id hash when the stored one is legacy.

    Call this after a *successful* legacy verification; persist the returned
    value into the user's ``hashed_password``. Returns ``None`` when the
    stored hash is already Argon2id.
    """
    if not needs_rehash(hashed_password):
        return None
    return hash_password(plain_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and verify a JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None
