"""Senhas com salt individual e tokens opacos revogáveis."""

import hashlib
import hmac
import secrets


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    return hmac.compare_digest(hash_password(password, encoded.split("$")[0]), encoded)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
