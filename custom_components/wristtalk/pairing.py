"""The pairing window and the watch keys.

The owner opens the window with "Add watch" (a subentry flow); while it is open watches may ask to
pair, each gets its own code. The owner types the code the watch shows into the flow: only that
watch gets a key - the others are refused and the window closes. Anyone who knows the address can
ask to pair, so a watch the owner did not type the code of never gets a key (security review
2026-11-07: compare-and-confirm let a stranger's request be confirmed by mistake). The watch
collects its key on its next poll. Nothing else is stored than the key's SHA-256 hash in the
watch's subentry.
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

from .const import (
    CODE_DIGITS,
    CODE_TRIES,
    KEY_BYTES,
    PAIR_CALLS_PER_WINDOW,
    PAIRING_WINDOW,
)


def hash_key(key: str) -> str:
    """The stored form of a key."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_matches(key: str, stored_hash: str) -> bool:
    """Compare in constant time."""
    return hmac.compare_digest(hash_key(key), stored_hash)


def _new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


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
    key: str | None = None  # set on confirm, handed to the watch until the window's end


class PairingWindow:
    """One window at a time, owned by one "Add watch" flow."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._flow_id: str | None = None
        self._target: str | None = None  # "Pair again": only this watch (its unique ID)
        self._opened_at: datetime | None = None
        self._calls = 0
        self._wrong_codes = 0
        self._requests: dict[str, WatchRequest] = {}  # by pair_id, the open window's
        self._requested: asyncio.Event = asyncio.Event()
        # The key waits here for the watch's polls after the flow has finished
        self._collected: dict[str, WatchRequest] = {}

    @property
    def is_open(self) -> bool:
        return (
            self._flow_id is not None
            and self._opened_at is not None
            and dt_util.utcnow() - self._opened_at < PAIRING_WINDOW
        )

    @property
    def requests(self) -> list[WatchRequest]:
        """The watches that asked in the open window, oldest first."""
        return sorted(self._requests.values(), key=lambda r: r.requested_at)

    def open(self, flow_id: str, unique_id: str | None = None) -> None:
        """Open the window for this flow, closing any earlier one; with a unique ID only that watch
        can be paired ("Pair again")."""
        self._flow_id = flow_id
        self._target = unique_id
        self._opened_at = dt_util.utcnow()
        self._calls = 0
        self._wrong_codes = 0
        self._requests = {}
        self._requested = asyncio.Event()

    def close(self, flow_id: str) -> None:
        """Close the window if this flow owns it (closing the flow closes the window)."""
        if self._flow_id == flow_id:
            self._flow_id = None
            self._opened_at = None
            self._requests = {}
            self._requested.set()  # wake a waiting flow step

    async def async_wait_for_watch(self) -> bool:
        """For the flow's progress step: True once a watch has asked, False at the window's end."""
        remaining = (
            PAIRING_WINDOW - (dt_util.utcnow() - self._opened_at)
            if self._opened_at
            else PAIRING_WINDOW
        )
        try:
            async with asyncio.timeout(max(remaining.total_seconds(), 0)):
                await self._requested.wait()
        except TimeoutError:
            return False
        return self.is_open and bool(self._requests)

    def request(self, data: dict[str, Any]) -> WatchRequest:
        """A watch asks to pair (POST pair)."""
        if not self.is_open:
            raise PairingError("pairing_closed", 403)
        self._calls += 1
        if self._calls > PAIR_CALLS_PER_WINDOW:
            raise PairingError("too_many_requests", 429)
        unique_id = str(data.get("unique_id") or "").strip()[:64]
        if not unique_id:
            raise PairingError("invalid_request", 400)
        # A watch asking again (it starts over after a lost reply) replaces its earlier request
        for pair_id in [
            p for p, r in self._requests.items() if r.unique_id == unique_id
        ]:
            del self._requests[pair_id]
        taken = {r.code for r in self._requests.values()}
        code = _new_code()
        while code in taken:
            code = _new_code()
        request = WatchRequest(
            pair_id=secrets.token_urlsafe(16),
            code=code,
            unique_id=unique_id,
            model=str(data.get("model") or "")[:64],
            app_version=str(data.get("app") or "")[:32],
        )
        self._requests[request.pair_id] = request
        self._requested.set()
        return request

    def confirm(self, flow_id: str, code: str) -> WatchRequest:
        """The owner typed a watch's code: that watch gets the key, the window closes.

        A wrong code counts; after CODE_TRIES the window closes ("too_many_codes"). A code that is not
        six digits (empty, short, a letter - spaces are fine) is "invalid_format" and does not count:
        an Enter pressed too early must not use up a try.
        """
        if self._flow_id != flow_id or not self.is_open:
            raise PairingError("pairing_closed", 403)
        typed = code.replace(" ", "")
        if len(typed) != CODE_DIGITS or not typed.isascii() or not typed.isdigit():
            raise PairingError("invalid_format", 400)
        found = None
        # Every request is compared, in constant time, so the time does not tell which one matched
        for request in self._requests.values():
            if hmac.compare_digest(request.code, typed):
                found = request
        if found is None:
            self._wrong_codes += 1
            if self._wrong_codes >= CODE_TRIES:
                self.close(flow_id)
                raise PairingError("too_many_codes", 403)
            raise PairingError("invalid_code", 400)
        if self._target is not None and found.unique_id != self._target:
            # A code that is right, of another watch asking in the same window
            raise PairingError("wrong_watch", 400)
        found.key = secrets.token_urlsafe(KEY_BYTES)
        self._collected[found.pair_id] = found
        self.close(flow_id)
        return found

    def poll(self, pair_id: str) -> dict[str, Any]:
        """The watch polls its pairing (POST pair/<pair_id>)."""
        # The same key again while the window lasts: a reply lost on the way (Bluetooth, the phone)
        # must not leave a watch Home Assistant has added without its key. pair_id is a secret
        # only that watch has.
        now = dt_util.utcnow()
        for old in [
            p
            for p, r in self._collected.items()
            if now - r.requested_at >= PAIRING_WINDOW * 2
        ]:
            del self._collected[old]
        if (collected := self._collected.get(pair_id)) is not None:
            return {"state": "paired", "key": collected.key}
        if self.is_open and pair_id in self._requests:
            return {"state": "pending"}
        raise PairingError("expired", 410)
