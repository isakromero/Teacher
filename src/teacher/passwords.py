"""Password hashing for user accounts (`teacher.managers.users`), with the standard library only.

A password is hashed with scrypt (`hashlib.scrypt`) and a random 16-byte salt of its own, and
stored as one string that carries its parameters:

    scrypt$ln=<log2 N>,r=<r>,p=<p>$<salt>$<key>

(salt and key in unpadded URL-safe base64; the key is 32 bytes). `verify_password` reads the
parameters from the stored string, so `DEFAULT_PARAMS` can be raised later without breaking
existing hashes: `needs_rehash` says when a stored hash uses other parameters, and the account
manager then stores a new hash after the next successful sign-in. Keys are compared in constant
time (`hmac.compare_digest`). A stored string that is not of this form (or has parameters
outside `MAX_LOG_N`, `MAX_R`, `MAX_P`) is a ValueError, never a failed sign-in: it means the
row is corrupt.

`dummy_hash()` is a hash of a random password with the current parameters, for spending one
verification when a sign-in names an email that has no account (so the time taken does not tell
which emails exist).

The password rules (`check_password_rules`): a str of `MIN_PASSWORD_CHARS` (8) to
`MAX_PASSWORD_CHARS` (1024) characters, used exactly as given (never stripped), that can be
encoded as UTF-8. A password is never logged and never part of an error message.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

SCHEME = "scrypt"
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_CHARS = 1024
#: Bounds on the parameters read from a stored hash (a corrupt row must not take all memory).
MAX_LOG_N = 20
MAX_R = 32
MAX_P = 16

_HASH_RE = re.compile(
    r"scrypt\$ln=([0-9]{1,2}),r=([0-9]{1,2}),p=([0-9]{1,2})\$([A-Za-z0-9_-]+)\$([A-Za-z0-9_-]+)"
)


@dataclass(frozen=True, slots=True)
class ScryptParams:
    """scrypt's cost: N = 2**log_n, block size r, parallelism p."""

    log_n: int
    r: int
    p: int

    def __post_init__(self) -> None:
        for name, value, top in (("log_n", self.log_n, MAX_LOG_N), ("r", self.r, MAX_R),
                                 ("p", self.p, MAX_P)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
                raise ValueError(f"scrypt {name} must be an integer from 1 to {top}")

    @property
    def n(self) -> int:
        return 2**self.log_n

    @property
    def maxmem(self) -> int:
        """The memory scrypt needs (OpenSSL: 128 * r * (N + 2 + p) bytes), plus 1 MiB."""
        return 128 * self.r * (self.n + 2 + self.p) + 1024 * 1024

    def encode(self) -> str:
        return f"ln={self.log_n},r={self.r},p={self.p}"


#: N = 2**15, r = 8, p = 1: 32 MiB and about 60 ms per hash on a laptop.
DEFAULT_PARAMS = ScryptParams(log_n=15, r=8, p=1)


def check_password_rules(password: object) -> str:
    """`password` when it follows the rules (see the module docstring), else a ValueError that
    says which rule (never the password)."""
    if not isinstance(password, str):
        raise ValueError("the password must be text")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"The password must have at least {MIN_PASSWORD_CHARS} characters.")
    if len(password) > MAX_PASSWORD_CHARS:
        raise ValueError(f"The password must have at most {MAX_PASSWORD_CHARS} characters.")
    _encode(password)
    return password


def _encode(password: str) -> bytes:
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("The password contains characters that cannot be stored.") from None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("the stored password hash is malformed") from None


def _derive(password: bytes, salt: bytes, params: ScryptParams, length: int) -> bytes:
    return hashlib.scrypt(
        password, salt=salt, n=params.n, r=params.r, p=params.p, maxmem=params.maxmem,
        dklen=length,
    )


def hash_password(password: str, params: ScryptParams = DEFAULT_PARAMS) -> str:
    """The stored form of `password` (which must follow the rules) with a new random salt."""
    secret = _encode(check_password_rules(password))
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(secret, salt, params, KEY_BYTES)
    return f"{SCHEME}${params.encode()}${_b64(salt)}${_b64(key)}"


def _parse(stored: str) -> tuple[ScryptParams, bytes, bytes]:
    if not isinstance(stored, str):
        raise ValueError("the stored password hash must be a string")
    match = _HASH_RE.fullmatch(stored)
    if match is None:
        raise ValueError("the stored password hash is malformed (expected scrypt$ln=..)")
    params = ScryptParams(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    salt, key = _unb64(match.group(4)), _unb64(match.group(5))
    if len(salt) < 8 or len(key) < 16:
        raise ValueError("the stored password hash is malformed (salt or key too short)")
    return params, salt, key


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` matches `stored` (constant-time comparison of the keys). Any str is
    checked as given, whatever its length; a malformed `stored` is a ValueError."""
    params, salt, key = _parse(stored)
    if not isinstance(password, str):
        raise TypeError("the password must be a str")
    try:
        secret = password.encode("utf-8")
    except UnicodeEncodeError:
        secret = password.encode("utf-8", "surrogatepass")  # never matches a stored password
    return hmac.compare_digest(_derive(secret, salt, params, len(key)), key)


def needs_rehash(stored: str, params: ScryptParams = DEFAULT_PARAMS) -> bool:
    """Whether `stored` was made with other parameters (or another key length) than `params`."""
    stored_params, _salt, key = _parse(stored)
    return stored_params != params or len(key) != KEY_BYTES


@functools.cache
def _dummy_hash(params: ScryptParams) -> str:
    return hash_password(secrets.token_urlsafe(24), params)


def dummy_hash(params: ScryptParams = DEFAULT_PARAMS) -> str:
    """A hash of a random password nobody knows, with `params` (made once per parameters)."""
    return _dummy_hash(params)


__all__ = [
    "DEFAULT_PARAMS",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "ScryptParams",
    "check_password_rules",
    "dummy_hash",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
