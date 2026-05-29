#!/usr/bin/env python3
"""Encrypt a secret with ORCH_FERNET_KEY → Fernet ciphertext. Wave PROVISION-1 ②.

Used to fill the __IMPORTED_API_KEY_ENC__ placeholder in
migrations/046_seed_vultr_account_import.sql, or any ad-hoc encrypt op.

    export ORCH_FERNET_KEY=...        # same key the orchestrator runs with
    python scripts/encrypt_secret.py "VULTR_API_KEY_HERE"
    # or:  echo -n "secret" | python scripts/encrypt_secret.py

Run from the repo root so `orchestrator` is importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.crypto import FernetKeyError, encrypt_secret  # noqa: E402


def main() -> int:
    plaintext = sys.argv[1] if len(sys.argv) >= 2 else sys.stdin.read().strip()
    if not plaintext:
        print("usage: encrypt_secret.py <secret>   (or pipe the secret on stdin)", file=sys.stderr)
        return 2
    try:
        print(encrypt_secret(plaintext))
    except FernetKeyError as exc:
        print(f"error: {exc} (is ORCH_FERNET_KEY set?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
