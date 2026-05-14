"""Credential verification for the single-user login."""
from __future__ import annotations

import os

_USERNAME = os.getenv("APP_USERNAME", "admin")
_PASSWORD = os.getenv("APP_PASSWORD", "")


def check_credentials(username: str, password: str) -> bool:
    if not _PASSWORD:
        return False  # block all logins if password env var not set
    return username == _USERNAME and password == _PASSWORD
