"""Fernet encryption for at-rest secrets (Vultr API keys). Wave PROVISION-1 ②.

The orchestrator had no encryption layer before this wave. Vultr API keys are
stored Fernet-encrypted in vultr_accounts.api_key_enc; the symmetric key comes
from the env var ORCH_FERNET_KEY (NOT committed). Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If ORCH_FERNET_KEY is unset/invalid, encrypt/decrypt raise FernetKeyError — the
account CRUD surfaces this as HTTP 500 and /register still registers the node
(only the Vultr instance-id lookup is skipped).
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class FernetKeyError(RuntimeError):
    """ORCH_FERNET_KEY missing, malformed, or token undecryptable."""


def _fernet() -> Fernet:
    key = os.getenv("ORCH_FERNET_KEY", "").strip()
    if not key:
        raise FernetKeyError("fernet_key_not_configured")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise FernetKeyError(f"invalid_fernet_key: {exc}") from exc


def encrypt_secret(plaintext: str) -> str:
    """Fernet-encrypt a plaintext secret → URL-safe ciphertext string."""
    return str(_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8"))


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string back to plaintext."""
    try:
        return str(_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8"))
    except InvalidToken as exc:
        raise FernetKeyError("decrypt_failed_invalid_token") from exc


def mask_secret(plaintext: str) -> str:
    """Mask a key for display: last 4 chars only (``****abcd``)."""
    s = plaintext or ""
    return "****" + s[-4:] if len(s) > 4 else "****"
