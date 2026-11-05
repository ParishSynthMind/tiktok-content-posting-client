"""Where the TikTok credentials live.

TikTok access tokens last about 2 hours and refresh tokens about 180 days. Refresh tokens
ROTATE: each refresh hands back a new one and the old one stops working, so the new token is
saved the moment it arrives, before any other call. Lose it and the owner has to sign in again.

- KeychainTokenStore: macOS Keychain generic-password items (used on the build machine).
- FileTokenStore: environment variables for the app keys plus a 0600 JSON file for the tokens.
Token values are never printed, logged or put in an error message.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Protocol

KEYS = ("client_key", "client_secret", "access_token", "refresh_token", "expires_at",
        "refresh_expires_at", "open_id")
WRITABLE = KEYS[2:]
ENV_NAMES = {k: f"TIKTOK_{k.upper()}" for k in KEYS}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._\-~+/=:!*]+$")


class TokenStoreError(RuntimeError):
    pass


class TokenStore(Protocol):
    def get(self, key: str) -> str: ...

    def save(self, **values: str) -> None: ...


def _check(key: str, value: str) -> None:
    if key not in WRITABLE:
        raise TokenStoreError(f"{key} is not something this tool writes")
    if not value or not SAFE_VALUE.match(value):
        raise TokenStoreError(f"refusing to store a {key} with unexpected characters")


class KeychainTokenStore:
    def __init__(self, account: str = "neo", prefix: str = "tiktok-", runner=subprocess.run):
        self.account = account
        self.prefix = prefix
        self.run = runner

    def item(self, key: str) -> str:
        return self.prefix + key.replace("_", "-")

    def get(self, key: str) -> str:
        proc = self.run(["security", "find-generic-password", "-a", self.account, "-s", self.item(key), "-w"],
                        capture_output=True, text=True)
        value = (proc.stdout or "").strip()
        if proc.returncode != 0 or not value:
            raise TokenStoreError(f"Keychain item {self.item(key)!r} (account {self.account!r}) not found")
        return value

    def save(self, **values: str) -> None:
        for key, value in values.items():
            _check(key, value)
        # The rotating refresh token goes first, so a failure part way never loses it.
        for key in sorted(values, key=lambda k: k != "refresh_token"):
            cmd = f'add-generic-password -U -a "{self.account}" -s "{self.item(key)}" -w "{values[key]}"\n'
            proc = self.run(["security", "-i"], input=cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise TokenStoreError(f"could not write {key} to the Keychain")


class FileTokenStore:
    def __init__(self, path: str | Path = ".tiktok_token.json", environ=None):
        self.path = Path(path)
        self.environ = os.environ if environ is None else environ

    def _file(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def get(self, key: str) -> str:
        value = self._file().get(key) or self.environ.get(ENV_NAMES[key], "")
        if not value:
            raise TokenStoreError(f"{ENV_NAMES[key]} is not set and {self.path} has no {key}")
        return value

    def save(self, **values: str) -> None:
        for key, value in values.items():
            _check(key, value)
        data = self._file()
        data.update(values)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)


def seconds_left(store: TokenStore, key: str = "expires_at", now: Optional[float] = None) -> float:
    try:
        return float(store.get(key)) - (time.time() if now is None else now)
    except (TokenStoreError, ValueError):
        return -1.0


def save_token_response(store: TokenStore, data: dict, now: Optional[float] = None) -> None:
    """Persist a /v2/oauth/token/ response (code exchange or refresh)."""
    now = time.time() if now is None else now
    store.save(refresh_token=data["refresh_token"], access_token=data["access_token"],
               expires_at=str(int(now + int(data["expires_in"]))),
               refresh_expires_at=str(int(now + int(data["refresh_expires_in"]))),
               open_id=data["open_id"])


def make_store(kind: str = "") -> TokenStore:
    kind = kind or os.environ.get("TIKTOK_TOKEN_STORE", "keychain")
    if kind == "keychain":
        return KeychainTokenStore(account=os.environ.get("TIKTOK_KEYCHAIN_ACCOUNT", "neo"))
    if kind == "file":
        return FileTokenStore(os.environ.get("TIKTOK_TOKEN_FILE", ".tiktok_token.json"))
    raise TokenStoreError("TIKTOK_TOKEN_STORE must be keychain or file")
