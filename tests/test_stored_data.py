"""What the first public release (1.6.1) stored keeps working: its config entry, its watches and
their settings load in every later version without pairing the watches again.

The stored entry below has the shape 1.6.1 writes (the entry data, the options, a watch subentry);
its values are synthetic. A change that needs this data in another shape needs a migration, and
this test is where that shows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.wristtalk.const import DOMAIN
from custom_components.wristtalk.pairing import hash_key

from .conftest import EXTERNAL_URL

KEY = "stored-key-of-the-paired-watch"  # synthetic
PAIRED_ID = "stored-paired-watch"  # synthetic subentry ids
WAITING_ID = "stored-waiting-watch"
PAIRED_AT = datetime(2026, 11, 16, 23, 22, 50, tzinfo=UTC).isoformat()
HEADERS = {"X-WT-Protocol": "6", "X-WT-App": "1.6.0", "X-WT-Key": KEY}


def _stored_entry(user_id: str) -> MockConfigEntry:
    """The entry as 1.6.1 stores it: one watch paired, one waiting to be paired again."""
    watches: list[ConfigSubentryData] = [
        ConfigSubentryData(
            subentry_id=PAIRED_ID,
            subentry_type="watch",
            title="fēnix 8 Solar 51 mm",
            unique_id="stored-watch-unique-id-1",
            data={
                "key_hash": hash_key(KEY),
                "model": "fēnix 8 Solar 51 mm",
                "app_version": "1.6.0",
                "paired_at": PAIRED_AT,
                "needs_pairing": False,
            },
        ),
        ConfigSubentryData(
            subentry_id=WAITING_ID,
            subentry_type="watch",
            title="fēnix 8 Solar 51 mm (2)",
            unique_id="stored-watch-unique-id-2",
            data={
                "key_hash": "",
                "model": "fēnix 8 Solar 51 mm",
                "app_version": "1.6.0",
                "paired_at": PAIRED_AT,
                "needs_pairing": True,
            },
        ),
    ]
    return MockConfigEntry(
        domain=DOMAIN,
        title="WristTalk",
        version=1,
        minor_version=1,
        data={"user_id": user_id},
        options={"pipeline": "p-claude", "url": EXTERNAL_URL},
        subentries_data=watches,
    )


async def _set_up(hass: HomeAssistant, restored: list[State] | None = None) -> Any:
    await async_setup_component(hass, "http", {})
    user = await hass.auth.async_create_system_user("WristTalk")
    if restored:
        mock_restore_cache(hass, restored)
    entry = _stored_entry(user.id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, user


async def test_stored_entry_loads_and_the_paired_watch_keeps_its_key(
    hass: HomeAssistant, urls, hass_client_no_auth
) -> None:
    entry, user = await _set_up(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options == {"pipeline": "p-claude", "url": EXTERNAL_URL}
    # The same system user, not a new one
    assert entry.data["user_id"] == user.id

    client = await hass_client_no_auth()
    response = await client.post("/api/wristtalk/v1/ping", headers=HEADERS)
    assert response.status == 200
    # A key that was never stored is still refused
    response = await client.post(
        "/api/wristtalk/v1/ping", headers={**HEADERS, "X-WT-Key": "another-key"}
    )
    assert response.status == 401


async def test_stored_watches_keep_their_state_and_repair_issue(
    hass: HomeAssistant, urls
) -> None:
    await _set_up(hass)
    states = er.async_get(hass)
    paired = states.async_get_entity_id("sensor", DOMAIN, f"{PAIRED_ID}-status")
    waiting = states.async_get_entity_id("sensor", DOMAIN, f"{WAITING_ID}-status")
    assert paired
    assert waiting
    assert hass.states.get(paired).state == "paired"
    assert hass.states.get(waiting).state == "needs_pairing"

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"needs_pairing_{WAITING_ID}")
    assert not issues.async_get_issue(DOMAIN, f"needs_pairing_{PAIRED_ID}")


async def test_stored_watches_keep_their_devices_and_entities(
    hass: HomeAssistant, urls
) -> None:
    entry, _ = await _set_up(hass)
    # The ids 1.6.1 gave the entities: a changed one would leave the old entity orphaned and make a
    # new one, losing the owner's names, areas and automations
    expected = {
        f"{watch}-{key}"
        for watch in (PAIRED_ID, WAITING_ID)
        for key in (
            "satellite",
            "status",
            "speech_quality",
            "pipeline",
            "vad_sensitivity",
        )
    }
    registry = er.async_get(hass)
    found = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert found == expected

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    # The device is the watch's Connect IQ identifier, as stored
    assert {frozenset(d.identifiers) for d in devices} == {
        frozenset({(DOMAIN, "stored-watch-unique-id-1")}),
        frozenset({(DOMAIN, "stored-watch-unique-id-2")}),
    }
    assert {d.sw_version for d in devices} == {"1.6.0"}


async def test_stored_voice_quality_is_restored(hass: HomeAssistant, urls) -> None:
    """The reply voice quality the owner chose survives the update."""
    registry = er.async_get(hass)
    entity_id = "select.fenix_8_solar_51_mm_reply_voice_quality"
    registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{PAIRED_ID}-speech_quality",
        suggested_object_id="fenix_8_solar_51_mm_reply_voice_quality",
    )
    await _set_up(hass, [State(entity_id, "fast")])
    assert hass.states.get(entity_id).state == "fast"
