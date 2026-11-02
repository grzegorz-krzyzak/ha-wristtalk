"""The pairing window and the watch keys.

The owner opens the window with "Add watch" (a subentry flow); while it is open one watch may ask to
pair. The flow shows the code the watch shows, the owner confirms, and the watch collects its key on
its next poll - once. Nothing else is stored than the key's SHA-256 hash in the watch's subentry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CODE_DIGITS, KEY_BYTES, PAIR_CALLS_PER_WINDOW, PAIRING_WINDOW


def hash_key(key: str) -> str:
    """The stored form of a key."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_matches(key: str, stored_hash: str) -> bool:
    """Compare in constant time."""
    return hmac.compare_digest(hash_key(key), stored_hash)


class PairingError(Exception):
    """A pairing request is refused; `code` is the error the watch receives."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class WatchRequest:
    """A watch that asked to pair in the open window."""

    pair_id: str
    code: str
    unique_id: str
    model: str
    app_version: str
    requested_at: datetime = field(default_factory=dt_util.utcnow)
    key: str | None = None  # set on confirm, handed to the watch once


class PairingWindow:
    """One window at a time, owned by one "Add watch" flow."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._flow_id: str | None = None
        self._opened_at: datetime | None = None
        self._calls = 0
        self._request: WatchRequest | None = None
        self._requested: asyncio.Event = asyncio.Event()
        # The key waits here for the watch's poll after the flow has finished
        self._collected: dict[str, WatchRequest] = {}

    @property
    def is_open(self) -> bool:
        return (
            self._flow_id is not None
            and self._opened_at is not None
            and dt_util.utcnow() - self._opened_at < PAIRING_WINDOW
        )

    def open(self, flow_id: str) -> None:
        """Open the window for this flow, closing any earlier one."""
        self._flow_id = flow_id
        self._opened_at = dt_util.utcnow()
        self._calls = 0
        self._request = None
        self._requested = asyncio.Event()

    def close(self, flow_id: str) -> None:
        """Close the window if this flow owns it (closing the flow closes the window)."""
        if self._flow_id == flow_id:
            self._flow_id = None
            self._opened_at = None
            if self._request is not None and self._request.key is None:
                self._request = None
            self._requested.set()  # wake a waiting flow step

    async def async_wait_for_watch(self) -> WatchRequest | None:
        """For the flow's progress step: the watch's request, or None at the window's end."""
        remaining = (
            PAIRING_WINDOW - (dt_util.utcnow() - self._opened_at)
            if self._opened_at
            else PAIRING_WINDOW
        )
        try:
            async with asyncio.timeout(max(remaining.total_seconds(), 0)):
                await self._requested.wait()
        except TimeoutError:
            return None
        return self._request if self.is_open else None

    def request(self, data: dict[str, Any]) -> WatchRequest:
        """A watch asks to pair (POST pair)."""
        if not self.is_open:
            raise PairingError("pairing_closed", 403)
        self._calls += 1
        if self._calls > PAIR_CALLS_PER_WINDOW:
            raise PairingError("too_many_requests", 429)
        if self._request is not None:
            raise PairingError("busy", 409)
        unique_id = str(data.get("unique_id") or "").strip()
        if not unique_id:
            raise PairingError("invalid_request", 400)
        self._request = WatchRequest(
            pair_id=secrets.token_urlsafe(16),
            code=f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}",
            unique_id=unique_id[:64],
            model=str(data.get("model") or "")[:64],
            app_version=str(data.get("app") or "")[:32],
        )
        self._requested.set()
        return self._request

    def confirm(self, flow_id: str) -> str:
        """The owner confirmed: make the key; the watch collects it on its next poll."""
        request = self._request
        if self._flow_id != flow_id or request is None:
            raise PairingError("pairing_closed", 403)
        request.key = secrets.token_urlsafe(KEY_BYTES)
        self._collected[request.pair_id] = request
        self.close(flow_id)
        return request.key

    def poll(self, pair_id: str) -> dict[str, Any]:
        """The watch polls its pairing (POST pair/<pair_id>)."""
        if (collected := self._collected.pop(pair_id, None)) is not None:
            if dt_util.utcnow() - collected.requested_at < PAIRING_WINDOW * 2:
                return {"state": "paired", "key": collected.key}
        if self.is_open and self._request is not None and self._request.pair_id == pair_id:
            return {"state": "pending"}
        raise PairingError("expired", 410)
