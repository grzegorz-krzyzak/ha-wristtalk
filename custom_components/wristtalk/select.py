"""Per watch: its Assist pipeline and end-of-speech sensitivity (Home Assistant's standard
satellite selects, as for Voice PE), and the quality of the reply's voice."""

from __future__ import annotations

from homeassistant.components.assist_pipeline import (
    AssistPipelineSelect,
    VadSensitivitySelect,
)
from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import WristTalkConfigEntry
from .const import CONF_PIPELINE, SPEECH_QUALITY_BITRATES, SPEECH_QUALITY_DEFAULT
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
                WatchSpeechQualitySelect(subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )


class WatchPipelineSelect(WatchEntity, AssistPipelineSelect):
    """The watch's pipeline; a new watch starts with the integration's default pipeline."""

    def __init__(
        self,
        hass: HomeAssistant,
        subentry: ConfigSubentry,
        default_pipeline: str | None,
    ) -> None:
        WatchEntity.__init__(self, subentry)
        AssistPipelineSelect.__init__(self, hass, "wristtalk", entity_prefix(subentry))
        self._default_pipeline = default_pipeline

    async def async_added_to_hass(self) -> None:
        first_time = await self.async_get_last_state() is None
        await super().async_added_to_hass()
        if first_time and self._default_pipeline:
            for pipeline in async_get_pipelines(self.hass):
                if (
                    pipeline.id == self._default_pipeline
                    and pipeline.name in self.options
                ):
                    await self.async_select_option(pipeline.name)


class WatchVadSensitivitySelect(WatchEntity, VadSensitivitySelect):
    """When the command ends: Home Assistant's VAD sensitivity (0.25 / 0.7 / 1.25 s of silence)."""

    def __init__(self, hass: HomeAssistant, subentry: ConfigSubentry) -> None:
        WatchEntity.__init__(self, subentry)
        VadSensitivitySelect.__init__(self, hass, entity_prefix(subentry))


class WatchSpeechQualitySelect(WatchEntity, SelectEntity, RestoreEntity):
    """The reply's voice: fast (smaller), balanced or best (larger) - the Opus bitrate."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "speech_quality"
    _attr_current_option = SPEECH_QUALITY_DEFAULT

    def __init__(self, subentry: ConfigSubentry) -> None:
        WatchEntity.__init__(self, subentry)
        self._attr_options = list(SPEECH_QUALITY_BITRATES)
        self._attr_unique_id = f"{entity_prefix(subentry)}-speech_quality"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in SPEECH_QUALITY_BITRATES:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
