import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, str | None]:
    """Verify a password against its hash.

    Returns (is_valid, rehash) where `rehash` is the new hash to persist if the
    stored hash was created with outdated argon2 parameters, else None.
    """
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False, None
    if _hasher.check_needs_rehash(password_hash):
        return True, _hasher.hash(password)
    return True, None


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
