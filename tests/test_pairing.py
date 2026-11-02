"""Add watch: the pairing window, the code, the key - and the endpoints the watch calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wristtalk.const import PAIRING_WINDOW, SUBENTRY_WATCH, WATCH_KEY_HASH, WATCH_MODEL
from custom_components.wristtalk.pairing import hash_key

HEADERS = {"X-WT-Protocol": "1", "X-WT-App": "0.1.0"}
WATCH = {"unique_id": "ac915d426451c88e8ea691fa412f9af9c21b4d12", "model": "fēnix 8 Solar 51 mm", "app": "0.1.0"}


async def _start_add_watch(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_WATCH), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_watch"
    return result


async def _pair(client, body: dict[str, Any] | None = None, headers=HEADERS):
    return await client.post("/api/wristtalk/v1/pair", json=body or WATCH, headers=headers)


async def _confirm(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    return result


async def test_add_watch_end_to_end(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)

    response = await _pair(client)
    assert response.status == 200
    pairing = await response.json()
    assert len(pairing["code"]) == 6 and pairing["code"].isdigit()
    assert pairing["protocol"] == 1

    # Before the owner confirms, the watch waits
    poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
    assert (await poll.json())["state"] == "pending"

    result = await _confirm(hass, flow["flow_id"])
    code = pairing["code"]
    assert result["description_placeholders"] == {"code": f"{code[:3]} {code[3:]}", "model": WATCH["model"]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The watch collects its key - again, if the first reply got lost on the way
    poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
    paired = await poll.json()
    assert paired["state"] == "paired"
    key = paired["key"]
    poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
    assert await poll.json() == {"state": "paired", "key": key, "protocol": 1}
    # ... but not after the window
    later = dt_util.utcnow() + PAIRING_WINDOW * 2
    with patch("custom_components.wristtalk.pairing.dt_util.utcnow", return_value=later):
        poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
    assert poll.status == 410

    # Home Assistant keeps only the hash
    (subentry,) = entry.subentries.values()
    assert subentry.unique_id == WATCH["unique_id"]
    assert subentry.data[WATCH_KEY_HASH] == hash_key(key)
    assert subentry.data[WATCH_MODEL] == WATCH["model"]
    assert key not in str(subentry.data)

    ping = await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key})
    assert ping.status == 200
    assert (await ping.json())["ok"] is True

    # The window closed with the confirmation
    assert (await _pair(client)).status == 403


async def test_wrong_or_missing_key(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    assert (await client.post("/api/wristtalk/v1/ping", headers=HEADERS)).status == 401
    bad = await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": "nope"})
    assert bad.status == 401
    assert (await bad.json())["error"] == "unauthorized"


async def test_pair_refused_outside_the_window(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    response = await _pair(client)
    assert response.status == 403
    assert (await response.json())["error"] == "pairing_closed"


async def test_closing_the_flow_closes_the_window(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    assert (await _pair(client)).status == 403


async def test_closing_the_flow_after_the_watch_asked(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    await _confirm(hass, flow["flow_id"])
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
    assert poll.status == 410
    assert not entry.subentries


async def test_one_pairing_at_a_time_and_call_limit(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    await _start_add_watch(hass, entry)
    assert (await _pair(client)).status == 200
    second = await _pair(client, {**WATCH, "unique_id": "other"})
    assert second.status == 409
    assert (await second.json())["error"] == "busy"
    statuses = [(await _pair(client)).status for _ in range(9)]
    assert statuses[-1] == 429


async def test_pair_needs_a_watch_id(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    await _start_add_watch(hass, entry)
    response = await _pair(client, {"model": "x"})
    assert response.status == 400
    response = await client.post("/api/wristtalk/v1/pair", data=b"not json", headers=HEADERS)
    assert response.status == 400


async def test_window_times_out(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch("custom_components.wristtalk.pairing.PAIRING_WINDOW", timedelta(milliseconds=50)):
        flow = await _start_add_watch(hass, entry)
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "timeout"
    assert (await _pair(client)).status == 403


async def test_window_taken_over_before_confirm(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    """A second "Add watch" replaces the window; the first flow cannot confirm any more."""
    client = await hass_client_no_auth()
    first = await _start_add_watch(hass, entry)
    await _pair(client)
    result = await _confirm(hass, first["flow_id"])
    await _start_add_watch(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "timeout"
    assert not entry.subentries


async def test_closing_the_flow_after_the_integration_unloaded(hass: HomeAssistant, entry) -> None:
    flow = await _start_add_watch(hass, entry)
    await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()


async def test_pairing_the_same_watch_again_replaces_it(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    keys = []
    for _ in range(2):
        flow = await _start_add_watch(hass, entry)
        pairing = await (await _pair(client)).json()
        result = await _confirm(hass, flow["flow_id"])
        await hass.config_entries.subentries.async_configure(result["flow_id"], {})
        poll = await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)
        keys.append((await poll.json())["key"])
    assert len(entry.subentries) == 1
    old = await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": keys[0]})
    new = await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": keys[1]})
    assert (old.status, new.status) == (401, 200)


async def test_removing_the_watch_revokes_its_key(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _confirm(hass, flow["flow_id"])
    await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    key = (await (await client.post(f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS)).json())["key"]
    (subentry_id,) = entry.subentries
    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    ping = await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key})
    assert ping.status == 401


@pytest.mark.parametrize("path", ["/api/wristtalk/v1/ping", "/api/wristtalk/v1/pair", "/api/wristtalk/v1/pair/x"])
async def test_protocol_versions(hass: HomeAssistant, entry, hass_client_no_auth, path: str) -> None:
    client = await hass_client_no_auth()
    for headers, status, error in (
        ({}, 400, "invalid_request"),
        ({"X-WT-Protocol": "0"}, 426, "update_app"),
        ({"X-WT-Protocol": "2"}, 501, "update_integration"),
    ):
        response = await client.post(path, headers=headers)
        assert response.status == status
        assert (await response.json())["error"] == error


async def test_reload_keeps_the_endpoints(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    """Views cannot be unregistered: a reload registers nothing twice and keeps answering."""
    client = await hass_client_no_auth()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await client.post("/api/wristtalk/v1/ping", headers=HEADERS)).status == 401


async def test_endpoints_without_the_integration(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    await hass.config_entries.async_unload(entry.entry_id)
    assert (await _pair(client)).status == 403
    assert (await client.post("/api/wristtalk/v1/pair/x", headers=HEADERS)).status == 410
    assert (await client.post("/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": "k"})).status == 401
