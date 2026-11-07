"""WristTalk - voice assistant for Home Assistant on a Garmin watch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .decoder import create_decoder
from .http import async_register_views
from .pairing import PairingWindow
from .session import SessionManager
from .watch_state import async_sync_issues

if TYPE_CHECKING:
    from .assist_satellite import WristTalkSatellite

PLATFORMS = [Platform.ASSIST_SATELLITE, Platform.SELECT, Platform.SENSOR]
CONF_USER_ID = "user_id"
SYSTEM_USER_NAME = "WristTalk"


@dataclass
class DomainData:
    """Kept across reloads of the entry: a reload follows each new watch, and the watch must
    still be able to collect its key from the pairing window afterwards."""

    pairing: PairingWindow
    sessions: SessionManager


@dataclass
class WristTalkData:
    """Runtime data of the config entry."""

    pairing: PairingWindow
    sessions: SessionManager
    user_id: str
    satellites: dict[str, WristTalkSatellite] = field(default_factory=dict)


type WristTalkConfigEntry = ConfigEntry[WristTalkData]


def _domain_data(hass: HomeAssistant) -> DomainData:
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = DomainData(
            pairing=PairingWindow(hass),
            # Looked up at each command, so a test can patch create_decoder
            sessions=SessionManager(hass, lambda h: create_decoder(h)),  # noqa: PLW0108
        )
    return hass.data[DOMAIN]


async def _system_user_id(hass: HomeAssistant, entry: WristTalkConfigEntry) -> str:
    """Commands run as the system user "WristTalk" - it shows where a command came from; it
    restricts nothing."""
    if (user_id := entry.data.get(CONF_USER_ID)) and await hass.auth.async_get_user(
        user_id
    ):
        return user_id
    user = await hass.auth.async_create_system_user(
        SYSTEM_USER_NAME, group_ids=[GROUP_ID_USER]
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_ID: user.id}
    )
    return user.id


async def async_setup_entry(hass: HomeAssistant, entry: WristTalkConfigEntry) -> bool:
    """Set up WristTalk."""
    domain = _domain_data(hass)
    entry.runtime_data = WristTalkData(
        pairing=domain.pairing,
        sessions=domain.sessions,
        user_id=await _system_user_id(hass, entry),
    )
    async_register_views(hass)
    async_sync_issues(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: WristTalkConfigEntry) -> None:
    """A watch added or removed, or the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: WristTalkConfigEntry) -> bool:
    """Unload; the endpoints stay registered but answer as if nothing were set up."""
    await entry.runtime_data.sessions.async_close_all()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: WristTalkConfigEntry) -> None:
    """Removing the integration removes its system user too."""
    if (user_id := entry.data.get(CONF_USER_ID)) and (
        user := await hass.auth.async_get_user(user_id)
    ):
        await hass.auth.async_remove_user(user)
    hass.data.pop(DOMAIN, None)
