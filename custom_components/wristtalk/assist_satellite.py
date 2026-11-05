"""Each watch is an Assist satellite: the pipeline and VAD come from its selects, runs show in
the Assist debug under the device. Home Assistant cannot reach the watch, so announcements and
starting a conversation are not supported."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineStage
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.components.assist_satellite import (
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WristTalkConfigEntry
from .const import DOMAIN, SPEECH_QUALITY_BITRATES, SPEECH_QUALITY_DEFAULT
from .decoder import async_compress_speech
from .entity import WatchEntity
from .select import entity_prefix

if TYPE_CHECKING:
    from .session import CommandSession

# The watch plays Ogg/Opus only ("ogg" would give Vorbis)
TTS_OPTIONS = {
    tts.ATTR_PREFERRED_FORMAT: "opus",
    tts.ATTR_PREFERRED_SAMPLE_RATE: 16000,
    tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WristTalkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    # Every subentry is a watch (the only subentry type)
    for subentry in entry.subentries.values():
        satellite = WristTalkSatellite(entry, subentry)
        entry.runtime_data.satellites[subentry.subentry_id] = satellite
        async_add_entities([satellite], config_subentry_id=subentry.subentry_id)


class WristTalkSatellite(WatchEntity, AssistSatelliteEntity):
    """A paired watch."""

    _attr_name = None
    _attr_supported_features = AssistSatelliteEntityFeature(0)

    def __init__(self, entry: WristTalkConfigEntry, subentry: ConfigSubentry) -> None:
        WatchEntity.__init__(self, subentry)
        self._entry = entry
        self._attr_unique_id = f"{entity_prefix(subentry)}-satellite"
        self._session: CommandSession | None = None

    def _select_entity_id(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"{entity_prefix(self.subentry)}-{key}"
        )

    @property
    def pipeline_entity_id(self) -> str | None:
        return self._select_entity_id("pipeline")

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return self._select_entity_id("vad_sensitivity")

    @property
    def tts_options(self) -> dict[str, Any] | None:
        return TTS_OPTIONS

    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """No wake words - the watch starts a command with a button."""
        return AssistSatelliteConfiguration(
            available_wake_words=[], active_wake_words=[], max_active_wake_words=0
        )

    async def async_set_configuration(
        self, config: AssistSatelliteConfiguration
    ) -> None:
        """Nothing to configure on the watch from Home Assistant."""

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        if self._session is not None:
            self._session.on_event(event)

    async def async_run_command(
        self, session: CommandSession, stream: AsyncIterator[bytes]
    ) -> None:
        """Run the pipeline up to the agent's reply (as the WristTalk system user).

        The pipeline's own streaming TTS stops at the agent's first words before a tool ("Checking
        ...", Home Assistant core#187933) while the reply text is complete - so the integration
        speaks the reply text itself (async_synthesize): the voice always matches the screen.
        """
        self._session = session
        try:
            await self.async_accept_pipeline_from_satellite(
                stream,
                end_stage=PipelineStage.INTENT,
                context=Context(user_id=self._entry.runtime_data.user_id),
            )
        finally:
            if self._session is session:
                self._session = None

    async def async_synthesize(self, text: str) -> bytes | None:
        """The reply text spoken with the watch pipeline's TTS engine and voice, as Ogg/Opus."""
        pipeline = async_get_pipeline(self.hass, self._resolve_pipeline())
        if not pipeline.tts_engine or not text:
            return None
        options = dict(TTS_OPTIONS)
        if pipeline.tts_voice:
            options[tts.ATTR_VOICE] = pipeline.tts_voice
        stream = tts.async_create_stream(
            self.hass, pipeline.tts_engine, pipeline.tts_language, options
        )
        stream.async_set_message(text)
        audio = b"".join([chunk async for chunk in stream.async_stream_result()])
        # Home Assistant's own bitrate option is for MP3 only; the reply goes to the watch through
        # the phone at about 13 kB/s (measured on a fēnix 8, 2026-11-04 - a status reply was 114 kB)
        return await async_compress_speech(
            get_ffmpeg_manager(self.hass).binary, audio, self._speech_bitrate()
        )

    def _speech_bitrate(self) -> str:
        """The watch's "Reply voice quality" select, the default when it is not there yet."""
        entity_id = self._select_entity_id("speech_quality")
        state = self.hass.states.get(entity_id) if entity_id else None
        quality = state.state if state is not None else SPEECH_QUALITY_DEFAULT
        return SPEECH_QUALITY_BITRATES.get(
            quality, SPEECH_QUALITY_BITRATES[SPEECH_QUALITY_DEFAULT]
        )
