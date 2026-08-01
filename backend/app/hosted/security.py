"""Password and opaque-token primitives for hosted authentication."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-password-value")


def normalized_email(value: str) -> tuple[str, str]:
    try:
        result = validate_email(value.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError("Enter a valid email address.") from exc
    display_email = result.normalized
    return display_email, display_email.casefold()


def validate_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must contain at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(value) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must not exceed {MAX_PASSWORD_LENGTH} characters.")
    return value


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(validate_password(password))


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, candidate)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def verify_dummy_password(candidate: str) -> None:
    verify_password(_DUMMY_PASSWORD_HASH, candidate)


def new_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
