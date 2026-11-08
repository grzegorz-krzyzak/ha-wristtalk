"""Add watch: the pairing window, the code, the key - and the endpoints the watch calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wristtalk.const import (
    PAIRING_WINDOW,
    SUBENTRY_WATCH,
    WATCH_KEY_HASH,
    WATCH_MODEL,
)
from custom_components.wristtalk.pairing import hash_key

HEADERS = {"X-WT-Protocol": "2", "X-WT-App": "0.2.9"}
WATCH = {
    "unique_id": "ac915d426451c88e8ea691fa412f9af9c21b4d12",
    "model": "fēnix 8 Solar 51 mm",
    "app": "0.1.0",
}


async def _start_add_watch(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_WATCH), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_watch"
    return result


async def _pair(client, body: dict[str, Any] | None = None, headers=HEADERS):
    return await client.post(
        "/api/wristtalk/v1/pair", json=body or WATCH, headers=headers
    )


async def _confirm(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """The flow past waiting: the form for the code the watch shows."""
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "code"
    return result


async def _enter_code(hass: HomeAssistant, flow_id: str, code: str) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_configure(flow_id, {"code": code})


async def _poll(client, pair_id: str):
    return await client.post(f"/api/wristtalk/v1/pair/{pair_id}", headers=HEADERS)


async def _collect(
    hass: HomeAssistant, client, flow_id: str, pairing: dict[str, Any], flows=None
) -> tuple[dict[str, Any], str]:
    """After the right code: the watch collects its key and proves it with a ping signed by it;
    then the flow ends (stores the watch). Returns the flow's result and the key."""
    flows = flows or hass.config_entries.subentries
    key = (await (await _poll(client, pairing["pair_id"])).json())["key"]
    ping = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key}
    )
    assert ping.status == 200
    await hass.async_block_till_done()
    return await flows.async_configure(flow_id), key


async def _pair_with_code(
    hass: HomeAssistant,
    client,
    flow_id: str,
    pairing: dict[str, Any],
    *,
    code=None,
    flows=None,
) -> tuple[dict[str, Any], str]:
    """Type the code (the watch's own unless given), wait for the key's collection, finish."""
    flows = flows or hass.config_entries.subentries
    result = await flows.async_configure(flow_id, {"code": code or pairing["code"]})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_key"
    return await _collect(hass, client, flow_id, pairing, flows)


async def test_add_watch_end_to_end(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)

    response = await _pair(client)
    assert response.status == 200
    pairing = await response.json()
    assert len(pairing["code"]) == 6
    assert pairing["code"].isdigit()
    assert pairing["protocol"] == 4

    # Before the owner confirms, the watch waits
    poll = await client.post(
        f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS
    )
    assert (await poll.json())["state"] == "pending"

    result = await _confirm(hass, flow["flow_id"])
    # The flow lists who asked; the code itself is only on the watch
    assert WATCH["model"] in result["description_placeholders"]["watches"]
    assert pairing["code"] not in str(result)
    # Typed as the watch shows it, with the space
    code = pairing["code"]
    result = await _enter_code(hass, result["flow_id"], f"{code[:3]} {code[3:]}")
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_key"
    # As every waiting step, with its placeholders (the browser failed on a step without them)
    assert result["description_placeholders"] == {"watch": WATCH["model"]}
    # Nothing is stored until the watch proves it has the key
    assert not entry.subentries

    # The watch collects its key - again, if the first reply got lost on the way
    poll = await client.post(
        f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS
    )
    paired = await poll.json()
    assert paired["state"] == "paired"
    key = paired["key"]
    poll = await client.post(
        f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS
    )
    assert await poll.json() == {"state": "paired", "key": key, "protocol": 4}
    assert not entry.subentries
    # ... and proves it: the first request signed with it
    ping = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key}
    )
    assert ping.status == 200
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The pairing is over
    poll = await client.post(
        f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS
    )
    assert poll.status == 410

    # Home Assistant keeps only the hash
    (subentry,) = entry.subentries.values()
    assert subentry.unique_id == WATCH["unique_id"]
    assert subentry.data[WATCH_KEY_HASH] == hash_key(key)
    assert subentry.data[WATCH_MODEL] == WATCH["model"]
    assert key not in str(subentry.data)

    ping = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key}
    )
    assert ping.status == 200
    assert (await ping.json())["ok"] is True

    # The window closed with the confirmation
    assert (await _pair(client)).status == 403


async def test_wrong_or_missing_key(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    assert (await client.post("/api/wristtalk/v1/ping", headers=HEADERS)).status == 401
    bad = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": "nope"}
    )
    assert bad.status == 401
    assert (await bad.json())["error"] == "unauthorized"


async def test_pair_refused_outside_the_window(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    response = await _pair(client)
    assert response.status == 403
    assert (await response.json())["error"] == "pairing_closed"


async def test_closing_the_flow_closes_the_window(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    assert (await _pair(client)).status == 403


async def test_closing_the_flow_after_the_watch_asked(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    await _confirm(hass, flow["flow_id"])
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    poll = await client.post(
        f"/api/wristtalk/v1/pair/{pairing['pair_id']}", headers=HEADERS
    )
    assert poll.status == 410
    assert not entry.subentries


async def test_only_the_watch_whose_code_is_typed_gets_a_key(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Anyone who knows the address can ask to pair while the window is open: a stranger's request
    neither blocks the owner's watch nor gets a key."""
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    stranger = await (await _pair(client, {**WATCH, "unique_id": "stranger"})).json()
    mine = await (await _pair(client)).json()
    assert stranger["code"] != mine["code"]
    result = await _confirm(hass, flow["flow_id"])
    result = await _enter_code(hass, result["flow_id"], mine["code"])
    # The stranger is refused at once; my watch collects its key
    assert (await _poll(client, stranger["pair_id"])).status == 410
    result, _ = await _collect(hass, client, result["flow_id"], mine)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.unique_id == WATCH["unique_id"]
    # The window closed with it
    assert (await _pair(client)).status == 403


async def test_call_limit(hass: HomeAssistant, entry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    await _start_add_watch(hass, entry)
    statuses = [
        (await _pair(client, {**WATCH, "unique_id": f"w{n}"})).status for n in range(11)
    ]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


async def test_a_watch_asking_again_replaces_its_request(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    first = await (await _pair(client)).json()
    second = await (await _pair(client)).json()
    assert (await _poll(client, first["pair_id"])).status == 410
    result = await _confirm(hass, flow["flow_id"])
    assert result["description_placeholders"]["watches"].count(WATCH["model"]) == 1
    result = await _enter_code(hass, result["flow_id"], first["code"])
    assert result["errors"] == {"code": "invalid_code"}
    result, _ = await _pair_with_code(hass, client, result["flow_id"], second)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_wrong_code_can_be_corrected(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    wrong = "000000" if pairing["code"] != "000000" else "111111"
    result = await _confirm(hass, flow["flow_id"])
    result = await _enter_code(hass, result["flow_id"], wrong)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"code": "invalid_code"}
    poll = await _poll(client, pairing["pair_id"])
    assert (await poll.json())["state"] == "pending"
    result, _ = await _pair_with_code(hass, client, result["flow_id"], pairing)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_code_that_is_not_six_digits_does_not_count(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Empty, short, long, with a letter or full-width digits: "the code has 6 digits", no try used."""
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _confirm(hass, flow["flow_id"])
    for typed in ("", "   ", "12345", "1234567", "12a456", "\uff11" * 6):
        result = await _enter_code(hass, result["flow_id"], typed)
        assert result["type"] is FlowResultType.FORM, typed
        assert result["errors"] == {"code": "invalid_format"}, typed
    # Six tries above, none counted: the right code still works
    result, _ = await _pair_with_code(hass, client, result["flow_id"], pairing)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_three_wrong_codes_close_the_window(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    wrong = "000000" if pairing["code"] != "000000" else "111111"
    result = await _confirm(hass, flow["flow_id"])
    for _ in range(2):
        result = await _enter_code(hass, result["flow_id"], wrong)
        assert result["errors"] == {"code": "invalid_code"}
    result = await _enter_code(hass, result["flow_id"], wrong)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "too_many_codes"
    assert (await _poll(client, pairing["pair_id"])).status == 410
    assert (await _pair(client)).status == 403
    assert not entry.subentries


async def test_code_after_the_window_ended(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _confirm(hass, flow["flow_id"])
    later = dt_util.utcnow() + PAIRING_WINDOW
    with patch(
        "custom_components.wristtalk.pairing.dt_util.utcnow", return_value=later
    ):
        result = await _enter_code(hass, result["flow_id"], pairing["code"])
    assert result["type"] is FlowResultType.ABORT
    # Not "no watch asked": one did, the time ran out while the code was typed
    assert result["reason"] == "expired"
    assert not entry.subentries


async def test_pair_body_is_read_only_inside_the_window_and_small(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    big = {**WATCH, "model": "x" * 2000}
    # Outside the window: refused before anything is read
    assert (await _pair(client, big)).status == 403
    await _start_add_watch(hass, entry)
    response = await _pair(client, big)
    assert response.status == 413
    assert (await response.json())["error"] == "request_too_large"
    assert (await _pair(client)).status == 200


async def test_pair_needs_a_watch_id(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    await _start_add_watch(hass, entry)
    response = await _pair(client, {"model": "x"})
    assert response.status == 400
    response = await client.post(
        "/api/wristtalk/v1/pair", data=b"not json", headers=HEADERS
    )
    assert response.status == 400


async def test_window_times_out(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    with patch(
        "custom_components.wristtalk.pairing.PAIRING_WINDOW", timedelta(milliseconds=50)
    ):
        flow = await _start_add_watch(hass, entry)
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "timeout"
    assert (await _pair(client)).status == 403


async def test_window_taken_over_before_confirm(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """A second "Add watch" replaces the window; the first flow cannot confirm any more."""
    client = await hass_client_no_auth()
    first = await _start_add_watch(hass, entry)
    await _pair(client)
    result = await _confirm(hass, first["flow_id"])
    await _start_add_watch(hass, entry)
    result = await _enter_code(hass, result["flow_id"], "123456")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "expired"
    assert not entry.subentries


async def test_closing_the_flow_after_the_integration_unloaded(
    hass: HomeAssistant, entry
) -> None:
    flow = await _start_add_watch(hass, entry)
    await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.subentries.async_abort(flow["flow_id"])
    await hass.async_block_till_done()


async def test_pairing_the_same_watch_again_replaces_it(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    keys = []
    for _ in range(2):
        flow = await _start_add_watch(hass, entry)
        pairing = await (await _pair(client)).json()
        result = await _confirm(hass, flow["flow_id"])
        _, key = await _pair_with_code(hass, client, result["flow_id"], pairing)
        keys.append(key)
    assert len(entry.subentries) == 1
    old = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": keys[0]}
    )
    new = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": keys[1]}
    )
    assert (old.status, new.status) == (401, 200)


async def test_removing_the_watch_revokes_its_key(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _confirm(hass, flow["flow_id"])
    _, key = await _pair_with_code(hass, client, result["flow_id"], pairing)
    (subentry_id,) = entry.subentries
    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    ping = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key}
    )
    assert ping.status == 401


@pytest.mark.parametrize(
    "path",
    ["/api/wristtalk/v1/ping", "/api/wristtalk/v1/pair", "/api/wristtalk/v1/pair/x"],
)
async def test_protocol_versions(
    hass: HomeAssistant, entry, hass_client_no_auth, path: str
) -> None:
    client = await hass_client_no_auth()
    for headers, status, error in (
        ({}, 400, "invalid_request"),
        ({"X-WT-Protocol": "1"}, 426, "update_app"),
        ({"X-WT-Protocol": "5"}, 501, "update_integration"),
    ):
        response = await client.post(path, headers=headers)
        assert response.status == status
        assert (await response.json())["error"] == error


async def test_reload_keeps_the_endpoints(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Views cannot be unregistered: a reload registers nothing twice and keeps answering."""
    client = await hass_client_no_auth()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await client.post("/api/wristtalk/v1/ping", headers=HEADERS)).status == 401


async def test_endpoints_without_the_integration(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    await hass.config_entries.async_unload(entry.entry_id)
    assert (await _pair(client)).status == 403
    assert (
        await client.post("/api/wristtalk/v1/pair/x", headers=HEADERS)
    ).status == 410
    assert (
        await client.post(
            "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": "k"}
        )
    ).status == 401


async def test_the_window_is_three_minutes(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Long enough to go to Home Assistant and type the code; no longer (0.1.6)."""
    client = await hass_client_no_auth()
    opened = dt_util.utcnow()
    with patch(
        "custom_components.wristtalk.pairing.dt_util.utcnow", return_value=opened
    ):
        await _start_add_watch(hass, entry)
    for after, status in (
        (timedelta(minutes=2, seconds=59), 200),
        (timedelta(minutes=3), 403),
    ):
        with patch(
            "custom_components.wristtalk.pairing.dt_util.utcnow",
            return_value=opened + after,
        ):
            assert (
                await _pair(client, {**WATCH, "unique_id": str(after)})
            ).status == status
