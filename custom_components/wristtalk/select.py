"""Per watch: its Assist pipeline and end-of-speech sensitivity (Home Assistant's standard
satellite selects, as for Voice PE)."""

from __future__ import annotations

from homeassistant.components.assist_pipeline import AssistPipelineSelect, VadSensitivitySelect
from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WristTalkConfigEntry
from .const import CONF_PIPELINE
from .entity import WatchEntity


def entity_prefix(subentry: ConfigSubentry) -> str:
    """The unique ID prefix of a watch's entities."""
    return f"{subentry.subentry_id}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WristTalkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    default_pipeline = entry.options.get(CONF_PIPELINE)
    # Every subentry is a watch (the only subentry type)
    for subentry in entry.subentries.values():
        async_add_entities(
            [
                WatchPipelineSelect(hass, subentry, default_pipeline),
                WatchVadSensitivitySelect(hass, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )


class WatchPipelineSelect(WatchEntity, AssistPipelineSelect):
    """The watch's pipeline; a new watch starts with the integration's default pipeline."""

    def __init__(self, hass: HomeAssistant, subentry: ConfigSubentry, default_pipeline: str | None) -> None:
        WatchEntity.__init__(self, subentry)
        AssistPipelineSelect.__init__(self, hass, "wristtalk", entity_prefix(subentry))
        self._default_pipeline = default_pipeline

    async def async_added_to_hass(self) -> None:
        first_time = await self.async_get_last_state() is None
        await super().async_added_to_hass()
        if first_time and self._default_pipeline:
            for pipeline in async_get_pipelines(self.hass):
                if pipeline.id == self._default_pipeline and pipeline.name in self.options:
                    await self.async_select_option(pipeline.name)


class WatchVadSensitivitySelect(WatchEntity, VadSensitivitySelect):
    """When the command ends: Home Assistant's VAD sensitivity (0.25 / 0.7 / 1.25 s of silence)."""

    def __init__(self, hass: HomeAssistant, subentry: ConfigSubentry) -> None:
        WatchEntity.__init__(self, subentry)
        VadSensitivitySelect.__init__(self, hass, entity_prefix(subentry))
