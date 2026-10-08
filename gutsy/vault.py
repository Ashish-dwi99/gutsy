"""Vault secrets in the OS keychain.

The store keeps only safe metadata (handle, label, allowed origins, a masked
username hint). The secret itself is written to the
keychain under the item's handle and read back only by `browser.fill`, so it
never passes through a model, a tool result, or a log.

Cards are deliberately not supported: a filled card number is plain page text
that the browser tool's snapshot would show the model. Purchases use the
merchant's own saved payment method, after the owner approves.
"""

from __future__ import annotations

import json
from typing import Any

import keyring

KEYCHAIN_SERVICE = "gutsy-vault"
LOGIN_FIELDS = ("username", "password")


class VaultError(RuntimeError):
    pass


def put_secret(handle: str, secret: dict[str, str]) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, handle, json.dumps(secret))


def get_secret(handle: str) -> dict[str, Any]:
    raw = keyring.get_password(KEYCHAIN_SERVICE, handle)
    if raw is None:
        raise VaultError(f"the keychain has no secret for {handle}; add it again with `gutsy vault add`")
    return json.loads(raw)


def delete_secret(handle: str) -> None:
    if keyring.get_password(KEYCHAIN_SERVICE, handle) is not None:
        keyring.delete_password(KEYCHAIN_SERVICE, handle)


def normalize_origin(value: str) -> str:
    """`https://Example.com/login` -> `https://example.com`. Plain http is refused."""
    from urllib.parse import urlsplit

    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise VaultError(f"{value!r} is not an https origin")
    port = f":{parts.port}" if parts.port else ""
    return f"https://{parts.hostname.lower()}{port}"
