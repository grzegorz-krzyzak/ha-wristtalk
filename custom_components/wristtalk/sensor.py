"""Per watch: its status - paired, or waiting to be paired again ("Pair again" on the watch)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WristTalkConfigEntry
from .const import WATCH_NEEDS_PAIRING
from .entity import WatchEntity
from .select import entity_prefix

STATUS_PAIRED = "paired"
STATUS_NEEDS_PAIRING = "needs_pairing"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WristTalkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        async_add_entities(
            [WatchStatusSensor(subentry)], config_subentry_id=subentry.subentry_id
        )


class WatchStatusSensor(WatchEntity, SensorEntity):
    """From the watch's subentry; a change reloads the integration, so the state is set once."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, subentry: ConfigSubentry) -> None:
        super().__init__(subentry)
        self._attr_options = [STATUS_PAIRED, STATUS_NEEDS_PAIRING]
        self._attr_unique_id = f"{entity_prefix(subentry)}-status"
        self._attr_native_value = (
            STATUS_NEEDS_PAIRING
            if subentry.data.get(WATCH_NEEDS_PAIRING)
            else STATUS_PAIRED
        )
