"""The device of a paired watch (one per "Watch" subentry)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, WATCH_APP, WATCH_MODEL


def watch_device_info(subentry: ConfigSubentry) -> DeviceInfo:
    """Model and app version as the watch reported them; the Connect IQ device identifier,
    shortened, as the serial number (Garmin gives apps no real serial number)."""
    unique_id = subentry.unique_id or subentry.subentry_id
    return DeviceInfo(
        identifiers={(DOMAIN, unique_id)},
        name=subentry.title,
        manufacturer="Garmin",
        model=subentry.data.get(WATCH_MODEL) or None,
        sw_version=subentry.data.get(WATCH_APP) or None,
        serial_number=unique_id[-8:],
    )


class WatchEntity(Entity):
    """An entity of a paired watch."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, subentry: ConfigSubentry) -> None:
        self.subentry = subentry
        self._attr_device_info = watch_device_info(subentry)
