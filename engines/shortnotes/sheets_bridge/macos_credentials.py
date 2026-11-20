"""macOS credential encryption. Only the AES key lives in the login Keychain.

Ciphertexts can be saved beside application settings; copying them to another
computer cannot transfer a sign-in. A locked/unavailable Keychain fails closed.
No shell command or command-line argument carries a credential.
"""
from __future__ import annotations

import base64
import os
import sys
import threading

SERVICE = "live.pw.NoteCraft"
_LOCK = threading.Lock()


def _key() -> bytes:
    if sys.platform != "darwin":
        raise ValueError("macOS Keychain is required for this credential.")
    from keyring.backends.macOS import Keyring
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # Explicit native backend: never select a plaintext fallback plugin.
    from pathlib import Path
    import fcntl
    folder = Path.home() / "Library" / "Application Support" / SERVICE
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _LOCK, (folder / "key.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        backend = Keyring()
        value = backend.get_password(SERVICE, "credential-encryption-v1")
        if value is None:
            value = base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")
            backend.set_password(SERVICE, "credential-encryption-v1", value)
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise ValueError("Invalid Keychain encryption key; sign in again.")
        return key


def protect(text: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    blob = nonce + AESGCM(_key()).encrypt(nonce, text.encode("utf-8"), SERVICE.encode())
    return {"enc": "keychain-aesgcm-v1", "value": base64.b64encode(blob).decode("ascii")}


def unprotect(record: dict) -> str:
    if record.get("enc") != "keychain-aesgcm-v1":
        raise ValueError("Credentials belong to another platform. Sign in on this computer.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    blob = base64.b64decode(record["value"], validate=True)
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], SERVICE.encode()).decode("utf-8")
