"""Credential verification and direct cookie-based session signing.

No middleware — cookies are signed with itsdangerous and verified per-request.
This avoids BaseHTTPMiddleware wrapping SSE streaming responses.
"""
from __future__ import annotations

import os
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_USERNAME = os.getenv("APP_USERNAME", "admin")
_PASSWORD = os.getenv("APP_PASSWORD", "")
_SECRET   = os.getenv("SESSION_SECRET", "dev-only-secret-change-me-in-prod")
_MAX_AGE  = 60 * 60 * 24 * 30   # 30 days

_signer = URLSafeTimedSerializer(_SECRET)


def check_credentials(username: str, password: str) -> bool:
    if not _PASSWORD:
        return False
    return username == _USERNAME and password == _PASSWORD


def make_token(username: str) -> str:
    return _signer.dumps(username)


def verify_token(token: str) -> Optional[str]:
    try:
        return _signer.loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
