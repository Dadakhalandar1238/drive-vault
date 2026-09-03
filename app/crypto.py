"""
Everything sensitive (refresh tokens, the vault file) is encrypted with a
key derived from SECRET_KEY. Nothing is ever stored in plaintext, whether
that's in the session cookie or in a Drive appDataFolder file.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from . import config


def _fernet() -> Fernet:
    digest = hashlib.sha256(config.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
