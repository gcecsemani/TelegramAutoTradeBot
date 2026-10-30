"""
core/crypto.py
==============
Encrypts broker API keys, secrets and access tokens at rest.

The Fernet key lives in CREDENTIALS_KEY or state/credentials.key.
That file is created on first use and is covered by .gitignore via state/.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

import config

_fernet = None


def _key() -> bytes:
    env = os.getenv("CREDENTIALS_KEY", "").strip()
    if env:
        return env.encode()
    path = os.path.join(config.STATE_DIR, "credentials.key")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()
    os.makedirs(config.STATE_DIR, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "rb") as fh:
            return fh.read().strip()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def reset_cipher() -> None:
    """Tests swap CREDENTIALS_KEY and need a fresh cipher."""
    global _fernet
    _fernet = None


def encrypt(value: str) -> str:
    if not value:
        return ""
    return cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt a stored credential. CREDENTIALS_KEY does not match.") from exc
