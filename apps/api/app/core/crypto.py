import base64
import hashlib
import os
import uuid
from typing import Optional
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


class SecretDecryptionError(ValueError):
    """Raised when AES-256-GCM authentication tag or AAD verification fails."""


def _derive_aes_key(raw_key: Optional[str] = None) -> bytes:
    key_str = (raw_key or settings.SECRET_ENCRYPTION_KEY).strip()
    try:
        padded = key_str + "=" * ((4 - len(key_str) % 4) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    return hashlib.sha256(key_str.encode("utf-8")).digest()


def _build_aad(workspace_id: Optional[uuid.UUID] = None, secret_name: Optional[str] = None) -> Optional[bytes]:
    if workspace_id is not None and secret_name is not None:
        return f"{workspace_id}:{secret_name}".encode("utf-8")
    return None


def encrypt_secret(
    plaintext: str,
    workspace_id: Optional[uuid.UUID] = None,
    secret_name: Optional[str] = None,
    key: Optional[str] = None,
) -> str:
    """Encrypt secret value using AES-256-GCM with a random 96-bit nonce and optional workspace:name AAD."""
    aes_key = _derive_aes_key(key)
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    aad = _build_aad(workspace_id, secret_name)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
    blob = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"v1:{blob}"


def decrypt_secret(
    token: str,
    workspace_id: Optional[uuid.UUID] = None,
    secret_name: Optional[str] = None,
    key: Optional[str] = None,
) -> str:
    """Decrypt a v1 AES-256-GCM secret ciphertext back to plaintext, verifying AAD."""
    if not token or not token.startswith("v1:"):
        raise SecretDecryptionError("Unsupported or malformed encrypted secret format")
    b64_part = token[3:]
    padded = b64_part + "=" * ((4 - len(b64_part) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise SecretDecryptionError("Invalid base64 secret payload") from exc
    if len(raw) < 12 + 16:
        raise SecretDecryptionError("Encrypted secret payload is truncated")
    nonce = raw[:12]
    ciphertext = raw[12:]
    aes_key = _derive_aes_key(key)
    aesgcm = AESGCM(aes_key)
    aad = _build_aad(workspace_id, secret_name)
    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SecretDecryptionError("AES-256-GCM tag or AAD verification failed") from exc
    return plaintext_bytes.decode("utf-8")
