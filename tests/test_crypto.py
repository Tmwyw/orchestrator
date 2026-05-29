"""Fernet helper round-trip + masking. Wave PROVISION-1 ②."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from orchestrator import crypto


@pytest.fixture()
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ORCH_FERNET_KEY", key)
    return key


def test_encrypt_decrypt_round_trip(_fernet_key: str) -> None:
    plaintext = "VULTR-SECRET-abc123"
    enc = crypto.encrypt_secret(plaintext)
    assert enc != plaintext  # actually encrypted
    assert crypto.decrypt_secret(enc) == plaintext


def test_encrypt_is_nondeterministic(_fernet_key: str) -> None:
    # Fernet embeds a random IV → two encryptions differ but both decrypt back.
    a = crypto.encrypt_secret("same")
    b = crypto.encrypt_secret("same")
    assert a != b
    assert crypto.decrypt_secret(a) == crypto.decrypt_secret(b) == "same"


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_FERNET_KEY", raising=False)
    with pytest.raises(crypto.FernetKeyError):
        crypto.encrypt_secret("x")


def test_invalid_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_FERNET_KEY", "not-a-valid-fernet-key")
    with pytest.raises(crypto.FernetKeyError):
        crypto.encrypt_secret("x")


def test_decrypt_wrong_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_FERNET_KEY", Fernet.generate_key().decode())
    enc = crypto.encrypt_secret("payload")
    monkeypatch.setenv("ORCH_FERNET_KEY", Fernet.generate_key().decode())  # rotate
    with pytest.raises(crypto.FernetKeyError):
        crypto.decrypt_secret(enc)


def test_mask_secret() -> None:
    assert crypto.mask_secret("ABCDEFGH") == "****EFGH"
    assert crypto.mask_secret("abc") == "****"
    assert crypto.mask_secret("") == "****"
