"""HTTP endpoints for the watches."""

from __future__ import annotations

from http import HTTPStatus
import json
import re
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    API_BASE,
    DOMAIN,
    HEADER_FINAL,
    HEADER_KEY,
    HEADER_PROTOCOL,
    HEADER_TEXT_ONLY,
    MIN_PROTOCOL,
    PROTOCOL,
    RECOGNIZING_PROTOCOL,
    SUBENTRY_WATCH,
    WATCH_KEY_HASH,
)
from .entity import entity_prefix
from .pairing import PairingError, key_matches
from .session import MAX_CHUNK_BYTES, SessionError
from .watch_state import async_needs_pairing

MAX_REQUEST_BYTES = MAX_CHUNK_BYTES
# A pairing request is a few short fields; anything larger is not read (no key needed to send it)
MAX_PAIR_BODY_BYTES = 1024
# The watch draws its session ids as hex (timer + random); anything else goes nowhere - not into the
# run event, the database or the log
SESSION_ID = re.compile(r"[0-9a-f]{1,32}")

if TYPE_CHECKING:
    from . import WristTalkData


def _reply(data: dict[str, Any], status: int = HTTPStatus.OK) -> web.Response:
    return web.json_response({**data, "protocol": PROTOCOL}, status=status)


def _error(code: str, status: int) -> web.Response:
    return _reply({"error": code}, status)


def _protocol_error(request: web.Request) -> web.Response | None:
    """HTTP 426 when the app is too old, 501 when the integration is.

    Two status codes, not one with two error texts: Connect IQ may hand the app no body for a
    status other than 200 (seen in the simulator for the chunk requests, which have a binary
    body; a JSON request got its 401 body).
    """
    try:
        version = int(request.headers.get(HEADER_PROTOCOL, ""))
    except ValueError:
        return _error("invalid_request", HTTPStatus.BAD_REQUEST)
    if version < MIN_PROTOCOL:
        return _error("update_app", HTTPStatus.UPGRADE_REQUIRED)
    if version > PROTOCOL:
        return _error("update_integration", HTTPStatus.NOT_IMPLEMENTED)
    return None


def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _no_access(hass: HomeAssistant, request: web.Request) -> web.Response:
    """The answer when no loaded entry has the request's watch.

    While the entry is not loaded (a reload after an options change or a new watch, a setup retry,
    the integration disabled) a watch whose key is still stored gets 503 `not_ready`: it retries
    and keeps its key - 401 makes it delete the key and pair again. 401 for a key no entry has:
    a watch removed, or never paired.
    """
    key = request.headers.get(HEADER_KEY, "")
    if key and _entry(hass) is None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            for subentry in entry.subentries.values():
                if subentry.subentry_type == SUBENTRY_WATCH and key_matches(
                    key, subentry.data.get(WATCH_KEY_HASH, "")
                ):
                    return _error("not_ready", HTTPStatus.SERVICE_UNAVAILABLE)
    return _error("unauthorized", HTTPStatus.UNAUTHORIZED)


def _data(hass: HomeAssistant) -> WristTalkData | None:
    entry = _entry(hass)
    return entry.runtime_data if entry is not None else None


def authenticated_watch(
    hass: HomeAssistant, request: web.Request
) -> ConfigSubentry | None:
    """The watch whose key the request carries, or None."""
    key = request.headers.get(HEADER_KEY, "")
    entry = _entry(hass)
    if not key or entry is None:
        return None
    found = None
    # Every watch is compared, so the time does not tell which one matched
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_WATCH and key_matches(
            key, subentry.data.get(WATCH_KEY_HASH, "")
        ):
            found = subentry
    return found


async def _read_body(request: web.Request, limit: int) -> bytes | None:
    """The whole body, or None when it is larger than `limit`.

    `content.read(n)` gives only what has arrived so far: a body in two TCP segments (through a
    proxy or the cloud) would be cut, so it is read to its end, never past the limit.
    """
    if request.content_length is not None and request.content_length > limit:
        return None
    body = bytearray()
    while part := await request.content.read(limit + 1 - len(body)):
        body += part
        if len(body) > limit:
            return None
    return bytes(body)


async def _pair_body(request: web.Request) -> dict[str, Any] | None:
    """The pairing request's JSON object; {} when it is not one, None when it is too large."""
    raw = await _read_body(request, MAX_PAIR_BODY_BYTES)
    if raw is None:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class PairView(HomeAssistantView):
    """A watch asks to pair while the owner's "Add watch" window is open."""

    url = f"{API_BASE}/pair"
    name = "api:wristtalk:pair"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (error := _protocol_error(request)) is not None:
            return error
        # The window first: outside it nothing of the request is read
        if (data := _data(hass)) is None or not data.pairing.is_open:
            return _error("pairing_closed", HTTPStatus.FORBIDDEN)
        if (body := await _pair_body(request)) is None:
            return _error("request_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            pairing = data.pairing.request(body)
        except PairingError as err:
            return _error(err.code, err.status)
        return _reply({"pair_id": pairing.pair_id, "code": pairing.code})


class PairPollView(HomeAssistantView):
    """The watch polls its pairing until the owner confirms."""

    url = API_BASE + "/pair/{pair_id}"
    name = "api:wristtalk:pair:poll"
    requires_auth = False

    async def post(self, request: web.Request, pair_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (error := _protocol_error(request)) is not None:
            return error
        if (data := _data(hass)) is None:
            return _error("expired", HTTPStatus.GONE)
        try:
            return _reply(data.pairing.poll(pair_id))
        except PairingError as err:
            return _error(err.code, err.status)


class PingView(HomeAssistantView):
    """Warms the connection when the app opens; also tells the app its key still works."""

    url = f"{API_BASE}/ping"
    name = "api:wristtalk:ping"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (error := _protocol_error(request)) is not None:
            return error
        if authenticated_watch(hass, request) is None:
            # A watch proving it collected its key: the pairing flow stores it now
            data = _data(hass)
            key = request.headers.get(HEADER_KEY, "")
            if data is None or not key or not data.pairing.acknowledge(key):
                return _no_access(hass, request)
        return _reply({"ok": True})


class RepairView(HomeAssistantView):
    """The watch's "Pair again" (protocol 3): with its key, before it drops it - Home Assistant
    revokes the key at once and shows that the watch needs pairing."""

    url = f"{API_BASE}/repair"
    name = "api:wristtalk:repair"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (error := _protocol_error(request)) is not None:
            return error
        entry = _entry(hass)
        watch = authenticated_watch(hass, request)
        if entry is None or watch is None:
            return _no_access(hass, request)
        async_needs_pairing(hass, entry, watch)
        return _reply({"ok": True})


def _answer(
    hass: HomeAssistant, request: web.Request, response: web.Response
) -> web.Response:
    """A request that waited may outlive its watch (removed meanwhile): then 401, re-pair - or
    the entry (reloaded meanwhile): then 503, retry."""
    if authenticated_watch(hass, request) is None:
        return _no_access(hass, request)
    return response


def _satellite_disabled(hass: HomeAssistant, watch: ConfigSubentry) -> bool:
    entity_id = er.async_get(hass).async_get_entity_id(
        "assist_satellite", DOMAIN, f"{entity_prefix(watch)}-satellite"
    )
    entity = er.async_get(hass).async_get(entity_id) if entity_id else None
    return entity is not None and entity.disabled


def _watch_context(hass: HomeAssistant, request: web.Request, session_id: str):
    """(runtime data, watch subentry) of an authenticated request, or an error response."""
    if (error := _protocol_error(request)) is not None:
        return None, None, error
    data = _data(hass)
    watch = authenticated_watch(hass, request)
    if data is None or watch is None:
        return None, None, _no_access(hass, request)
    if not SESSION_ID.fullmatch(session_id):
        return None, None, _error("invalid_request", HTTPStatus.BAD_REQUEST)
    return data, watch, None


class CommandView(HomeAssistantView):
    """A chunk of the command's Ogg/Opus at its byte offset; the first one starts the command."""

    url = API_BASE + "/command/{session_id}/{offset}"
    name = "api:wristtalk:command"
    requires_auth = False

    async def post(
        self, request: web.Request, session_id: str, offset: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data, watch, error = _watch_context(hass, request, session_id)
        if error is not None:
            return error
        try:
            position = int(offset)
        except ValueError:
            position = -1
        if position < 0:
            return _error("invalid_request", HTTPStatus.BAD_REQUEST)
        if (satellite := data.satellites.get(watch.subentry_id)) is None:
            # The watch's satellite is not running: disabled by the owner, or still starting
            code = "watch_disabled" if _satellite_disabled(hass, watch) else "not_ready"
            return _error(code, HTTPStatus.SERVICE_UNAVAILABLE)
        if (body := await _read_body(request, MAX_REQUEST_BYTES)) is None:
            return _answer(
                hass,
                request,
                _error("chunk_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
            )
        try:
            session = await data.sessions.get_or_start(
                watch.subentry_id,
                session_id,
                satellite,
                text_only=request.headers.get(HEADER_TEXT_ONLY) == "1",
            )
            await session.feed(
                position, body, final=request.headers.get(HEADER_FINAL) == "1"
            )
            early = int(request.headers[HEADER_PROTOCOL]) >= RECOGNIZING_PROTOCOL
            response = _reply(await session.chunk_reply(early_signal=early))
        except SessionError as err:
            response = _error(err.code, err.status)
        return _answer(hass, request, response)


class ReplyView(HomeAssistantView):
    """Long poll for the reply text."""

    url = API_BASE + "/reply/{session_id}"
    name = "api:wristtalk:reply"
    requires_auth = False

    async def post(self, request: web.Request, session_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data, watch, error = _watch_context(hass, request, session_id)
        if error is not None:
            return error
        try:
            response = _reply(
                await data.sessions.get(watch.subentry_id, session_id).reply_result()
            )
        except SessionError as err:
            response = _error(err.code, err.status)
        return _answer(hass, request, response)


class AudioView(HomeAssistantView):
    """The ready reply audio, Ogg/Opus, as application/octet-stream (what Connect IQ accepts)."""

    url = API_BASE + "/audio/{session_id}"
    name = "api:wristtalk:audio"
    requires_auth = False

    async def get(self, request: web.Request, session_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data, watch, error = _watch_context(hass, request, session_id)
        if error is not None:
            return error
        try:
            audio = await data.sessions.get(watch.subentry_id, session_id).audio()
        except SessionError as err:
            return _answer(hass, request, _error(err.code, err.status))
        return _answer(
            hass,
            request,
            web.Response(body=audio, content_type="application/octet-stream"),
        )


class AudioPieceView(HomeAssistantView):
    """The reply audio from an offset while it is being made (protocol 6), whole Ogg pages."""

    url = API_BASE + "/audio/{session_id}/{offset}"
    name = "api:wristtalk:audio:piece"
    requires_auth = False

    async def get(
        self, request: web.Request, session_id: str, offset: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data, watch, error = _watch_context(hass, request, session_id)
        if error is not None:
            return error
        try:
            position = int(offset)
        except ValueError:
            position = -1
        if position < 0:
            return _error("invalid_request", HTTPStatus.BAD_REQUEST)
        try:
            piece = await data.sessions.get(watch.subentry_id, session_id).audio_piece(
                position
            )
        except SessionError as err:
            return _answer(hass, request, _error(err.code, err.status))
        return _answer(
            hass,
            request,
            web.Response(body=piece, content_type="application/octet-stream"),
        )


def async_register_views(hass: HomeAssistant) -> None:
    """Once per Home Assistant run - views cannot be unregistered."""
    if hass.data.get(f"{DOMAIN}_views"):
        return
    hass.data[f"{DOMAIN}_views"] = True
    for view in (
        PairView(),
        PairPollView(),
        PingView(),
        RepairView(),
        CommandView(),
        ReplyView(),
        AudioView(),
        AudioPieceView(),
    ):
        hass.http.register_view(view)
