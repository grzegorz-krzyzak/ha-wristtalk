"""Each watch is an Assist satellite: the pipeline and VAD come from its selects, runs show in
the Assist debug under the device. Home Assistant cannot reach the watch, so announcements and
starting a conversation are not supported."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from homeassistant.components import tts
from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
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
from .const import (
    DOMAIN,
    EVENT_COMMAND_RUN,
    SPEECH_QUALITY_BITRATES,
    SPEECH_QUALITY_DEFAULT,
)
from .entity import WatchEntity
from .select import entity_prefix
from .speech import SpeechStream, create_encoder

if TYPE_CHECKING:
    from .session import CommandSession


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
        if self._session is None:
            return
        if event.type is PipelineEventType.RUN_START:
            # Which command this run is for - a log of pipeline runs counts them per command
            self.hass.bus.async_fire(
                EVENT_COMMAND_RUN,
                {
                    "session_id": self._session.session_id,
                    "entity_id": self.entity_id,
                    "run_start": event.timestamp,
                },
            )
        self._session.on_event(event)

    async def async_run_command(
        self, session: CommandSession, stream: AsyncIterator[bytes]
    ) -> None:
        """Run the pipeline up to the agent's reply (as the WristTalk system user).

        The pipeline's own streaming TTS stops at the agent's first words before a tool ("Checking
        ...", Home Assistant core#187933) while the reply text is complete - so the integration
        speaks the reply text itself (async_prepare_speech): the voice always matches the screen.
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

    async def async_prepare_speech(self, session_id: str) -> SpeechStream | None:
        """The reply's voice, ready to speak: the TTS stream and ffmpeg started, waiting for the text.

        No format is asked of the TTS: Home Assistant then gives MP3 - the Home Assistant Cloud
        engine's own format, so nothing converts it before ffmpeg (asking for Opus cost a second
        lossy pass and about 0.25 s, measured 2026-11-10). ffmpeg makes the watch's Ogg/Opus at the
        watch's "Reply voice quality" - Home Assistant's own bitrate option is for MP3 only.
        """
        pipeline = async_get_pipeline(self.hass, self._resolve_pipeline())
        if not pipeline.tts_engine:
            return None
        options = {tts.ATTR_VOICE: pipeline.tts_voice} if pipeline.tts_voice else {}
        stream = tts.async_create_stream(
            self.hass, pipeline.tts_engine, pipeline.tts_language, options
        )

        def say(text: str) -> AsyncIterator[bytes]:
            stream.async_set_message(text)
            return stream.async_stream_result()

        speech = SpeechStream(
            create_encoder(
                get_ffmpeg_manager(self.hass).binary,
                stream.extension,
                self._speech_bitrate(),
            ),
            say,
            lambda coro: self.hass.async_create_background_task(
                coro, f"wristtalk voice {session_id}"
            ),
            session_id,
        )
        await speech.start()
        return speech

    def _speech_bitrate(self) -> str:
        """The watch's "Reply voice quality" select, the default when it is not there yet."""
        entity_id = self._select_entity_id("speech_quality")
        state = self.hass.states.get(entity_id) if entity_id else None
        quality = state.state if state is not None else SPEECH_QUALITY_DEFAULT
        return SPEECH_QUALITY_BITRATES.get(
            quality, SPEECH_QUALITY_BITRATES[SPEECH_QUALITY_DEFAULT]
        )
