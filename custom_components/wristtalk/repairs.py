"""Repairs: a watch that asked to be paired again - "Fix" pairs it in place."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant

from . import watch_state
from .config_flow import PairingSteps
from .pairing import WatchRequest


class WatchPairAgainFlow(PairingSteps, RepairsFlow):
    """The same steps as "Pair again" in the watch's menu, started from Repairs."""

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id

    def _wristtalk_entry(self) -> ConfigEntry:
        return self._entry

    def _target(self) -> ConfigSubentry | None:
        return self._entry.subentries.get(self._subentry_id)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        if self._target() is None or self._entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="watch_removed")
        return self._wait("init")

    def _finish(self, watch: WatchRequest) -> Any:
        if (subentry := self._target()) is None:
            return self.async_abort(reason="watch_removed")
        watch_state.async_paired_again(self.hass, self._entry, subentry, watch)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry = hass.config_entries.async_get_entry(str((data or {}).get("entry_id")))
    if entry is None:
        raise ValueError(f"WristTalk is not set up for {issue_id}")
    return WatchPairAgainFlow(entry, str((data or {}).get("subentry_id")))
