"""Per watch: its status - paired, or waiting to be paired again ("Pair again" on the watch)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WristTalkConfigEntry
from .const import WATCH_NEEDS_PAIRING
from .entity import WatchEntity
from .select import entity_prefix
from .watch_state import status_signal

STATUS_PAIRED = "paired"
STATUS_NEEDS_PAIRING = "needs_pairing"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WristTalkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        async_add_entities(
            [WatchStatusSensor(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class WatchStatusSensor(WatchEntity, SensorEntity):
    """From the watch's subentry, again whenever its key or status changes (a signal - such a
    change does not reload the integration)."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        super().__init__(subentry)
        self._entry = entry
        self._attr_options = [STATUS_PAIRED, STATUS_NEEDS_PAIRING]
        self._attr_unique_id = f"{entity_prefix(subentry)}-status"
        self._read(subentry)

    def _read(self, subentry: ConfigSubentry) -> None:
        self._attr_native_value = (
            STATUS_NEEDS_PAIRING
            if subentry.data.get(WATCH_NEEDS_PAIRING)
            else STATUS_PAIRED
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self.subentry.subentry_id), self._changed
            )
        )

    @callback
    def _changed(self) -> None:
        if (
            subentry := self._entry.subentries.get(self.subentry.subentry_id)
        ) is not None:
            self._read(subentry)
            self.async_write_ha_state()
