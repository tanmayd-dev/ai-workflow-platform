from datetime import datetime, timedelta
from typing import Optional
import hashlib
import secrets
from jose import JWTError, jwt
from app.core.config import get_settings

settings = get_settings()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash using SHA-256."""
    # Extract salt from stored hash (format: salt$hash)
    try:
        salt, stored_hash = hashed_password.split('$')
        # Hash the plain password with the same salt
        password_hash = hashlib.sha256((salt + plain_password).encode('utf-8')).hexdigest()
        return password_hash == stored_hash
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    """Hash a password using SHA-256 with random salt."""
    # Generate a random salt
    salt = secrets.token_hex(16)
    # Hash password with salt
    password_hash = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    # Return salt$hash format
    return f"{salt}${password_hash}"


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
