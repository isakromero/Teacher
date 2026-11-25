"""`teacher.passwords`: the scrypt hash format, verification, the rules, rehash detection and
refusing a malformed stored hash."""

from __future__ import annotations

import pytest

from teacher import passwords
from teacher.passwords import (
    DEFAULT_PARAMS,
    ScryptParams,
    check_password_rules,
    dummy_hash,
    hash_password,
    needs_rehash,
    verify_password,
)

CHEAP = ScryptParams(log_n=4, r=8, p=1)


def test_hash_format_and_verify() -> None:
    stored = hash_password("correct horse")
    scheme, params, salt, key = stored.split("$")
    assert (scheme, params) == ("scrypt", "ln=15,r=8,p=1")
    assert len(salt) == 22 and len(key) == 43  # 16 and 32 bytes, unpadded base64
    assert "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("correct horsE", stored)
    assert not verify_password("correct horse ", stored)  # never stripped
    assert not verify_password("", stored)


def test_every_hash_has_its_own_salt() -> None:
    assert hash_password("same password", CHEAP) != hash_password("same password", CHEAP)


def test_parameters_travel_with_the_hash() -> None:
    cheap = hash_password("pass word!", CHEAP)
    assert cheap.startswith("scrypt$ln=4,r=8,p=1$")
    assert verify_password("pass word!", cheap)  # read from the string, not the defaults
    assert needs_rehash(cheap)
    assert not needs_rehash(cheap, CHEAP)
    assert not needs_rehash(hash_password("pass word!"))


def test_unicode_passwords() -> None:
    stored = hash_password("pässwörd ✓ 中文", CHEAP)
    assert verify_password("pässwörd ✓ 中文", stored)
    assert not verify_password("passwörd ✓ 中文", stored)
    # Not encodable as UTF-8: no match, and no error.
    assert not verify_password("pässwörd \udcff 中文", stored)
    with pytest.raises(ValueError, match="cannot be stored"):
        hash_password("bad \udcff surrogate", CHEAP)


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("seven..", "at least 8 characters"),
        ("", "at least 8 characters"),
        ("x" * 1025, "at most 1024 characters"),
        (None, "must be text"),
        (b"bytes password", "must be text"),
    ],
)
def test_password_rules(password: object, message: str) -> None:
    with pytest.raises(ValueError, match=message) as info:
        check_password_rules(password)
    if isinstance(password, str) and password:
        assert password not in str(info.value)


def test_password_limits_are_inclusive() -> None:
    assert check_password_rules("eight...") == "eight..."
    assert check_password_rules("x" * 1024) == "x" * 1024
    assert check_password_rules("  spaced  ") == "  spaced  "


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "plain text",
        "bcrypt$ln=15,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43,
        "scrypt$ln=15,r=8$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43,
        "scrypt$ln=15,r=8,p=1$AAAA$" + "A" * 43,  # salt too short
        "scrypt$ln=15,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAA",  # key too short
        "scrypt$ln=15,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43 + "=",
        "scrypt$ln=40,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43,  # would take all memory
        "scrypt$ln=0,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43,
        "scrypt$ln=15,r=99,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43,
        "scrypt$ln=15,r=8,p=1$A$" + "A" * 43,  # not base64
    ],
)
def test_a_malformed_stored_hash_is_an_error(stored: str) -> None:
    with pytest.raises(ValueError, match="scrypt|malformed|must be"):
        verify_password("any password", stored)


def test_params_are_checked() -> None:
    with pytest.raises(ValueError, match="log_n"):
        ScryptParams(log_n=21, r=8, p=1)
    with pytest.raises(ValueError, match="r must"):
        ScryptParams(log_n=10, r=0, p=1)
    with pytest.raises(ValueError, match="p must"):
        ScryptParams(log_n=10, r=8, p=True)
    assert DEFAULT_PARAMS.maxmem > 128 * DEFAULT_PARAMS.r * DEFAULT_PARAMS.n


def test_dummy_hash_is_made_once_per_parameters() -> None:
    assert dummy_hash(CHEAP) is dummy_hash(CHEAP)
    assert dummy_hash(CHEAP).startswith("scrypt$ln=4,")
    assert dummy_hash().startswith("scrypt$ln=15,")
    assert not verify_password("password", dummy_hash(CHEAP))


def test_errors_never_name_the_password() -> None:
    secret = "my secret password"
    with pytest.raises(ValueError) as info:
        verify_password(secret, "nonsense")
    assert secret not in str(info.value)
    with pytest.raises(ValueError) as info:
        check_password_rules(secret * 100)
    assert secret not in str(info.value)
    assert (passwords.MIN_PASSWORD_CHARS, passwords.MAX_PASSWORD_CHARS) == (8, 1024)
