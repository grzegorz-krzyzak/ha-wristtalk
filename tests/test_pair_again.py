"""Pairing a known watch again: from Add watch, from the watch's menu in Home Assistant, and from
Repairs after the watch's own "Pair again" - always a new key in the same subentry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.wristtalk.const import DOMAIN, SUBENTRY_WATCH

from .test_command import watch_fixture
from .test_pairing import HEADERS, WATCH, _enter_code, _pair, _poll, _start_add_watch

HEADERS_3 = {**HEADERS, "X-WT-Protocol": "3"}


def _entity_id(hass: HomeAssistant, entry, domain: str, key: str) -> str:
    (subentry_id,) = entry.subentries
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{subentry_id}-{key}"
    )
    assert entity_id
    return entity_id


async def _choose_best_voice(hass: HomeAssistant, entry) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": _entity_id(hass, entry, "select", "speech_quality"),
            "option": "best",
        },
        blocking=True,
    )


async def _ping(client, key: str) -> int:
    response = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": key}
    )
    return response.status


async def _code_step(
    hass: HomeAssistant, result: dict[str, Any], flows
) -> dict[str, Any]:
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await flows.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "code"
    return result


async def _key_of(client, pair_id: str) -> str:
    return (await (await _poll(client, pair_id)).json())["key"]


def _status(hass: HomeAssistant, entry) -> str:
    return hass.states.get(_entity_id(hass, entry, "sensor", "status")).state


async def test_add_watch_for_a_known_watch_keeps_it(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Option B: Add watch for a watch Home Assistant knows - a new key, the same device and
    settings."""
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry_id,) = entry.subentries
    await _choose_best_voice(hass, entry)
    client = old["client"]
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _code_step(hass, flow, hass.config_entries.subentries)
    result = await _enter_code(hass, result["flow_id"], pairing["code"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "paired_again"
    await hass.async_block_till_done()
    assert list(entry.subentries) == [subentry_id]
    new_key = await _key_of(client, pairing["pair_id"])
    assert await _ping(client, old["headers"]["X-WT-Key"]) == 401
    assert await _ping(client, new_key) == 200
    speech = hass.states.get(_entity_id(hass, entry, "select", "speech_quality"))
    assert speech.state == "best"
    assert _status(hass, entry) == "paired"


async def test_pair_again_from_the_watch_menu(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Option A: the watch's menu in Home Assistant - "Pair again" (a subentry reconfigure)."""
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry_id,) = entry.subentries
    await _choose_best_voice(hass, entry)
    client = old["client"]
    flows = hass.config_entries.subentries
    result = await flows.async_init(
        (entry.entry_id, SUBENTRY_WATCH),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert result["progress_action"] == "wait_watch_again"
    assert result["description_placeholders"]["watch"] == WATCH["model"]
    pairing = await (await _pair(client)).json()
    result = await _code_step(hass, result, flows)
    result = await _enter_code(hass, result["flow_id"], pairing["code"])
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "paired_again")
    await hass.async_block_till_done()
    assert list(entry.subentries) == [subentry_id]
    assert await _ping(client, old["headers"]["X-WT-Key"]) == 401
    assert await _ping(client, await _key_of(client, pairing["pair_id"])) == 200
    speech = hass.states.get(_entity_id(hass, entry, "select", "speech_quality"))
    assert speech.state == "best"


async def test_pair_again_takes_only_that_watch(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """Another watch asking in the same window: its (right) code is refused, no try used."""
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry_id,) = entry.subentries
    client = old["client"]
    flows = hass.config_entries.subentries
    result = await flows.async_init(
        (entry.entry_id, SUBENTRY_WATCH),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    other = await (await _pair(client, {**WATCH, "unique_id": "other"})).json()
    mine = await (await _pair(client)).json()
    result = await _code_step(hass, result, flows)
    for _ in range(3):
        result = await _enter_code(hass, result["flow_id"], other["code"])
        assert result["errors"] == {"code": "wrong_watch"}
    result = await _enter_code(hass, result["flow_id"], mine["code"])
    assert result["reason"] == "paired_again"
    assert (await _poll(client, other["pair_id"])).status == 410
    assert len(entry.subentries) == 1


async def test_the_watch_asks_to_be_paired_again(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    """The watch's own "Pair again" (protocol 3): the key is revoked at once, the status says so,
    Repairs offer "Fix", which pairs the watch in place."""
    assert await async_setup_component(hass, "repairs", {})
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry_id,) = entry.subentries
    await _choose_best_voice(hass, entry)
    client = old["client"]
    key = old["headers"]["X-WT-Key"]
    assert _status(hass, entry) == "paired"

    # Without the key nothing happens
    assert (
        await client.post("/api/wristtalk/v1/repair", headers=HEADERS_3)
    ).status == 401
    response = await client.post(
        "/api/wristtalk/v1/repair", headers={**HEADERS_3, "X-WT-Key": key}
    )
    assert response.status == 200
    await hass.async_block_till_done()
    assert await _ping(client, key) == 401
    assert _status(hass, entry) == "needs_pairing"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"needs_pairing_{subentry_id}")
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"watch": WATCH["model"]}

    # Still there after a restart of the integration
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"needs_pairing_{subentry_id}")

    # "Fix" in Repairs: the same pairing, for this watch
    flows = repairs_flow_manager(hass)
    result = await flows.async_init(DOMAIN, context={"issue_id": issue.issue_id})
    assert result["progress_action"] == "wait_watch_again"
    pairing = await (await _pair(client)).json()
    result = await _code_step(hass, result, flows)
    result = await flows.async_configure(result["flow_id"], {"code": pairing["code"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert list(entry.subentries) == [subentry_id]
    assert await _ping(client, await _key_of(client, pairing["pair_id"])) == 200
    assert _status(hass, entry) == "paired"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue.issue_id) is None
    speech = hass.states.get(_entity_id(hass, entry, "select", "speech_quality"))
    assert speech.state == "best"


async def test_removing_a_watch_that_needs_pairing_removes_its_issue(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry_id,) = entry.subentries
    await old["client"].post(
        "/api/wristtalk/v1/repair",
        headers={**HEADERS_3, "X-WT-Key": old["headers"]["X-WT-Key"]},
    )
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"needs_pairing_{subentry_id}")
    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"needs_pairing_{subentry_id}")
        is None
    )


async def test_an_app_on_protocol_2_keeps_working(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> None:
    old = await watch_fixture(hass, entry, hass_client_no_auth)
    assert old["headers"]["X-WT-Protocol"] == "2"
    assert await _ping(old["client"], old["headers"]["X-WT-Key"]) == 200
