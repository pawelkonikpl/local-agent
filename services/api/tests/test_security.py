from argon2 import PasswordHasher

from api.auth.security import generate_session_token, hash_password, hash_token, verify_password


def test_hash_password_is_not_plaintext() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert hashed.startswith("$argon2")


def test_verify_password_accepts_correct_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    valid, rehash = verify_password(hashed, "correct-horse-battery-staple")
    assert valid is True
    assert rehash is None


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    valid, rehash = verify_password(hashed, "wrong-password")
    assert valid is False
    assert rehash is None


def test_verify_password_rehashes_outdated_parameters() -> None:
    outdated_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    outdated_hash = outdated_hasher.hash("correct-horse-battery-staple")

    valid, rehash = verify_password(outdated_hash, "correct-horse-battery-staple")

    assert valid is True
    assert rehash is not None
    assert rehash != outdated_hash

    # the new hash must itself verify the same password
    valid_again, _ = verify_password(rehash, "correct-horse-battery-staple")
    assert valid_again is True


def test_generate_session_token_is_random_and_url_safe() -> None:
    tokens = {generate_session_token() for _ in range(10)}
    assert len(tokens) == 10


def test_hash_token_is_deterministic_and_one_way() -> None:
    token = generate_session_token()
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token
