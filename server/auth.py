"""Login for the app: one owner, a password plus a 6-digit code from an
authenticator app (TOTP), and a signed session cookie.

Secrets come from the environment (see .env.example):
  APP_PASSWORD_HASH  made by `python -m server setup`, never the password itself
  APP_TOTP_SECRET    the authenticator app's secret, also from setup
  APP_SECRET_KEY     signs session cookies

Anyone who gets past this login can trade the account, so it fails closed:
with any of the three missing, nobody can log in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from urllib.parse import quote

SESSION_SECONDS = 7 * 86400
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
_SCRYPT = (2**15, 8, 1)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    n, r, p = _SCRYPT
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=2**26)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                             p=int(p), maxmem=2**26)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def check_totp(secret: str, code: str, at: float | None = None) -> bool:
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    now = time.time() if at is None else at
    # Allow one step either side for clock drift.
    return any(hmac.compare_digest(totp(secret, now + d), code) for d in (-30, 0, 30))


def otpauth_uri(secret: str, label: str = "Trading Bot") -> str:
    return f"otpauth://totp/{quote(label)}?secret={secret}&issuer={quote(label)}"


class Auth:
    def __init__(self, password_hash: str, totp_secret: str, secret_key: str,
                 clock=time.time):
        self.password_hash = password_hash
        self.totp_secret = totp_secret
        self.key = secret_key.encode()
        self.clock = clock
        self._failures: list[float] = []
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.password_hash and self.totp_secret and len(self.key) >= 32)

    def locked_for(self) -> int:
        with self._lock:
            now = self.clock()
            self._failures = [t for t in self._failures if now - t < LOCKOUT_SECONDS]
            if len(self._failures) >= MAX_FAILURES:
                return int(LOCKOUT_SECONDS - (now - self._failures[0])) + 1
            return 0

    def login(self, password: str, code: str) -> str | None:
        """Return a session token, or None. Too many failures lock everyone
        out for a while, the right side to err on for a one-person app."""
        if not self.configured or self.locked_for():
            return None
        ok = check_password(password or "", self.password_hash)
        ok = check_totp(self.totp_secret, code, self.clock()) and ok
        if not ok:
            with self._lock:
                self._failures.append(self.clock())
            return None
        with self._lock:
            self._failures.clear()
        return self._sign(int(self.clock()) + SESSION_SECONDS)

    def _sign(self, expires: int) -> str:
        payload = f"{expires}.{secrets.token_hex(8)}"
        mac = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{mac}"

    def valid(self, token: str | None) -> bool:
        if not token or not self.configured:
            return False
        try:
            expires, nonce, mac = token.split(".")
        except ValueError:
            return False
        want = hmac.new(self.key, f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, mac):
            return False
        return expires.isdigit() and int(expires) > self.clock()
