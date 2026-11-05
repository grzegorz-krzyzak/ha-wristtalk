"""HTTP endpoints for the watches."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .const import (
    API_BASE,
    DOMAIN,
    HEADER_FINAL,
    HEADER_KEY,
    HEADER_PROTOCOL,
    HEADER_TEXT_ONLY,
    MIN_PROTOCOL,
    PROTOCOL,
    SUBENTRY_WATCH,
    WATCH_KEY_HASH,
)
from .pairing import PairingError, key_matches
from .session import MAX_CHUNK_BYTES, SessionError

MAX_REQUEST_BYTES = MAX_CHUNK_BYTES

if TYPE_CHECKING:
    from . import WristTalkData


def _reply(data: dict[str, Any], status: int = HTTPStatus.OK) -> web.Response:
    return web.json_response({**data, "protocol": PROTOCOL}, status=status)


def _error(code: str, status: int) -> web.Response:
    return _reply({"error": code}, status)


def _protocol_error(request: web.Request) -> web.Response | None:
    """HTTP 426 when the app is too old, 501 when the integration is.

    Two status codes, not one with two error texts: Connect IQ may hand the app no body for a
    status other than 200 (measured in the simulator for the chunk requests, which have a binary
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


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json(loads=json.loads)
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
        if (data := _data(hass)) is None:
            return _error("pairing_closed", HTTPStatus.FORBIDDEN)
        try:
            pairing = data.pairing.request(await _json_body(request))
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
            return _error("unauthorized", HTTPStatus.UNAUTHORIZED)
        return _reply({"ok": True})


def _answer(
    hass: HomeAssistant, request: web.Request, response: web.Response
) -> web.Response:
    """A request that waited may outlive its watch (removed meanwhile): then 401, re-pair."""
    if authenticated_watch(hass, request) is None:
        return _error("unauthorized", HTTPStatus.UNAUTHORIZED)
    return response


def _watch_context(hass: HomeAssistant, request: web.Request):
    """(runtime data, watch subentry) of an authenticated request, or an error response."""
    if (error := _protocol_error(request)) is not None:
        return None, None, error
    data = _data(hass)
    watch = authenticated_watch(hass, request)
    if data is None or watch is None:
        return None, None, _error("unauthorized", HTTPStatus.UNAUTHORIZED)
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
        data, watch, error = _watch_context(hass, request)
        if error is not None:
            return error
        try:
            position = int(offset)
        except ValueError:
            return _error("invalid_request", HTTPStatus.BAD_REQUEST)
        if (satellite := data.satellites.get(watch.subentry_id)) is None:
            return _error("not_ready", HTTPStatus.SERVICE_UNAVAILABLE)
        body = await request.content.read(MAX_REQUEST_BYTES + 1)
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
            response = _reply(await session.chunk_reply())
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
        data, watch, error = _watch_context(hass, request)
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
        data, watch, error = _watch_context(hass, request)
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


def async_register_views(hass: HomeAssistant) -> None:
    """Once per Home Assistant run - views cannot be unregistered."""
    if hass.data.get(f"{DOMAIN}_views"):
        return
    hass.data[f"{DOMAIN}_views"] = True
    for view in (
        PairView(),
        PairPollView(),
        PingView(),
        CommandView(),
        ReplyView(),
        AudioView(),
    ):
        hass.http.register_view(view)
