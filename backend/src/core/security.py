"""Password hashing and stateless auth tokens — standard library only.

Deliberately no passlib / bcrypt / python-jose. This project ships with a
"install deps, press Run" promise, and every extra wheel is one more thing that
fails to build on someone's machine. PBKDF2-HMAC-SHA256 is in ``hashlib`` and is
an acceptable password KDF at a high iteration count; HMAC-SHA256 over a JSON
payload gives us the same shape as a JWT without the dependency.

Two things here are load-bearing and easy to break:

1. Every comparison of secret material goes through ``hmac.compare_digest``.
   ``==`` on a hash leaks its prefix through timing.
2. Tokens carry a ``pv`` ("password version") claim — the first 16 hex chars of
   ``sha256(password_hash)``. :func:`decode_token` only returns the payload; the
   caller re-derives ``pv`` from the DB row and compares. That's what makes
   "change the password → every other device is logged out" work without any
   server-side session store.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Password hashing
# ---------------------------------------------------------------------------

_ALGO = "pbkdf2_sha256"
# OWASP's 2023 floor for PBKDF2-HMAC-SHA256 is 600k; 260k is the Django default
# and takes ~80ms on the machines this runs on. Login is not a hot path, but the
# admin console also verifies on every password change, so we stay at 260k and
# leave headroom to raise it — stored hashes carry their own iteration count, so
# bumping this constant does not invalidate existing passwords.
_ITERATIONS = 260_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a plaintext password into ``pbkdf2_sha256$iters$salt$hash``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` string.

    Returns False (never raises) on a malformed or empty stored value, so a
    corrupted row locks the account out instead of crashing the login endpoint.
    """
    if not password or not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def password_version(password_hash: str) -> str:
    """Short fingerprint of a stored hash, embedded in tokens as ``pv``.

    Truncated to 16 hex chars: long enough that it won't collide across two
    passwords in practice, short enough to keep the token small. It is not a
    secret — it's derived from a hash and only used for equality.
    """
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
#  Tokens
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised by :func:`decode_token` for anything that isn't a valid token."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def create_token(
    *,
    user_id: int,
    username: str,
    password_hash: str,
    secret: str,
    ttl_seconds: int,
) -> Tuple[str, int]:
    """Sign a token for ``username``. Returns ``(token, expires_in_seconds)``."""
    now = int(time.time())
    payload = {
        "sub": username,
        "uid": user_id,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "pv": password_version(password_hash),
    }
    # separators: no whitespace, so the payload is as short as possible.
    # sort_keys: deterministic output, which makes the tests readable.
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(body, secret)}", int(ttl_seconds)


def decode_token(token: str, secret: str) -> Dict[str, Any]:
    """Verify signature + expiry and return the payload.

    Raises :class:`TokenError` on a malformed, tampered or expired token. The
    caller still has to check ``pv`` against the current password hash — this
    function has no DB access and cannot do it.
    """
    if not token or "." not in token:
        raise TokenError("malformed token")
    body, _, sig = token.rpartition(".")
    # compare_digest on the signature: a plain == would let an attacker
    # brute-force the MAC one byte at a time.
    if not hmac.compare_digest(_sign(body, secret), sig):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError("undecodable payload") from e
    if not isinstance(payload, dict):
        raise TokenError("undecodable payload")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        raise TokenError("token expired")
    return payload


def new_secret() -> str:
    """A fresh random secret, for the 'you left APP_SECRET_KEY at the default' path."""
    return secrets.token_urlsafe(32)


# The value documented in .env.example — public knowledge to anyone who has
# read the repo. Admin tokens are HMAC-signed with APP_SECRET_KEY, so a server
# left on this key can be taken over by anyone with a text editor.
DEFAULT_SECRET = "change-me-in-production"

# Where an auto-generated secret is persisted, relative to the working
# directory (backend/) — the same convention as ./data/app.db and
# ./data/chroma_db, and covered by the same .gitignore entry.
SECRET_FILE = "data/.secret_key"


class DefaultSecretError(RuntimeError):
    """Production startup refused because APP_SECRET_KEY is the repo default."""


def resolve_secret_key(current: str, env: str, secret_file: str = SECRET_FILE) -> str:
    """Never run with the documented default secret.

    * a custom key is returned unchanged;
    * ``env == "production"`` refuses to start (DefaultSecretError) — silently
      rotating the key there would invalidate tokens across restarts behind
      the operator's back;
    * anywhere else a random key is generated once and persisted next to the
      database, so restarts and relogins behave normally. Concurrent workers
      race on O_EXCL creation; the loser adopts the winner's key.
    """
    if current != DEFAULT_SECRET:
        return current
    if env == "production":
        raise DefaultSecretError(
            "APP_SECRET_KEY 仍是仓库默认值，生产环境拒绝启动。请生成随机值写入 .env："
            'python -c "import secrets;print(secrets.token_urlsafe(48))"'
        )
    from pathlib import Path

    path = Path(secret_file)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing and existing != DEFAULT_SECRET:
            return existing
    except FileNotFoundError:
        pass
    generated = new_secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(generated)
        return generated
    except FileExistsError:
        winner = path.read_text(encoding="utf-8").strip()
        return winner or generated
