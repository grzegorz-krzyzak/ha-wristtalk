"""A command: chunks in, the command, the reply and its audio out - with a fake pipeline."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import shutil
import subprocess
import time
from typing import Any
from unittest.mock import patch

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.wristtalk import CONF_USER_ID
from custom_components.wristtalk.const import CONF_PIPELINE, DOMAIN
from custom_components.wristtalk.decoder import FfmpegDecoder, async_compress_speech

from .test_pairing import (
    HEADERS,
    WATCH,
    _confirm,
    _pair,
    _pair_with_code,
    _start_add_watch,
)

END = b"<silence>"  # in the fake decoder's input: the point where the fake VAD ends the command
SPEECH = b"<speech>"  # ... and where it hears speech start


class PassthroughDecoder:
    """Input = PCM, so the tests control exactly what the pipeline gets."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    async def start(self) -> None:
        pass

    async def feed(self, data: bytes) -> None:
        if not self.closed:
            await self._queue.put(data)

    async def close_input(self) -> None:
        if not self.closed:
            self.closed = True
            await self._queue.put(None)

    async def pcm(self) -> AsyncIterator[bytes]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk

    async def stop(self) -> None:
        await self.close_input()


@dataclass
class FakePipeline:
    """Stands in for async_pipeline_from_audio_stream behind the satellite."""

    heard: str = "Która jest godzina?"
    reply: str = "Jest 21:37."
    errors: list[str] = field(
        default_factory=list
    )  # one error code per run, then normal runs
    runs: list[dict[str, Any]] = field(default_factory=list)
    hold_reply: asyncio.Event | None = None
    hold_stt: asyncio.Event | None = None
    hold_tts: asyncio.Event | None = None
    tts_calls: list = field(default_factory=list)
    raise_error: bool = False
    tts_engine: str | None = "tts.fake"
    synthesized: list[str] = field(default_factory=list)
    nothing_on_cut: bool = False  # when cancelled, still reports "nothing recognised"

    async def __call__(
        self, hass, *, context, event_callback, stt_stream, end_stage, **kwargs
    ) -> None:
        run: dict[str, Any] = {
            "audio": b"",
            "end_stage": end_stage,
            "user_id": context.user_id,
            **kwargs,
        }
        self.runs.append(run)
        event_callback(PipelineEvent(PipelineEventType.RUN_START, {}))
        try:
            async for chunk in stt_stream:
                run["audio"] += chunk
                if SPEECH in run["audio"] and not run.get("speech"):
                    run["speech"] = True
                    event_callback(PipelineEvent(PipelineEventType.STT_VAD_START, {}))
                if END in run["audio"]:
                    event_callback(PipelineEvent(PipelineEventType.STT_VAD_END, {}))
                    break
        except asyncio.CancelledError:
            if self.nothing_on_cut:
                # A pipeline being cancelled may still report while it unwinds
                await asyncio.sleep(0)
                event_callback(
                    PipelineEvent(
                        PipelineEventType.ERROR, {"code": "stt-no-text-recognized"}
                    )
                )
            raise
        if self.raise_error:
            raise RuntimeError("pipeline broke")
        if self.hold_stt is not None:
            await self.hold_stt.wait()
        if self.errors:
            event_callback(
                PipelineEvent(
                    PipelineEventType.ERROR,
                    {"code": self.errors.pop(0), "message": "x"},
                )
            )
            return
        event_callback(
            PipelineEvent(
                PipelineEventType.STT_END, {"stt_output": {"text": self.heard}}
            )
        )
        if self.hold_reply is not None:
            try:
                await self.hold_reply.wait()
            except asyncio.CancelledError:
                run["cancelled"] = True  # the agent never acted
                raise
        event_callback(
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "intent_output": {
                        "response": {"speech": {"plain": {"speech": self.reply}}}
                    }
                },
            )
        )


class FakeTtsStream:
    """tts.async_create_stream: records what the integration asked to speak."""

    def __init__(self, fake: FakePipeline, engine, language, options) -> None:
        self._fake = fake
        self.options = options
        fake.tts_calls.append((engine, language, options))

    def async_set_message(self, message: str) -> None:
        self._fake.synthesized.append(message)

    async def async_stream_result(self):
        if self._fake.hold_tts is not None:
            await self._fake.hold_tts.wait()
        yield REPLY_AUDIO[:10]
        yield REPLY_AUDIO[10:]


@dataclass
class FakeTtsPipeline:
    tts_engine: str | None
    tts_language: str = "pl"
    tts_voice: str | None = "MarekNeural"


REPLY_AUDIO = b"OggS" + b"\x00" * 2000


@pytest.fixture
def pipeline() -> FakePipeline:
    fake = FakePipeline()
    with (
        patch(
            "homeassistant.components.assist_satellite.entity.async_pipeline_from_audio_stream",
            fake,
        ),
        patch(
            "custom_components.wristtalk.create_decoder",
            lambda hass: PassthroughDecoder(),
        ),
        patch(
            "homeassistant.components.tts.async_create_stream",
            lambda hass, engine, language=None, options=None: FakeTtsStream(
                fake, engine, language, options
            ),
        ),
        patch(
            "custom_components.wristtalk.assist_satellite.async_get_pipeline",
            lambda hass, pipeline_id=None: FakeTtsPipeline(fake.tts_engine),
        ),
    ):
        yield fake


@pytest.fixture
async def watch(
    hass: HomeAssistant, entry, pipeline, hass_client_no_auth
) -> dict[str, Any]:
    """A paired watch: its key and the client."""
    return await watch_fixture(hass, entry, hass_client_no_auth)


async def watch_fixture(
    hass: HomeAssistant, entry, hass_client_no_auth
) -> dict[str, Any]:
    client = await hass_client_no_auth()
    flow = await _start_add_watch(hass, entry)
    pairing = await (await _pair(client)).json()
    result = await _confirm(hass, flow["flow_id"])
    _, key = await _pair_with_code(hass, client, result["flow_id"], pairing)
    await hass.async_block_till_done()
    return {"client": client, "headers": {**HEADERS, "X-WT-Key": key}}


async def _chunk(
    watch, session: str, offset: int, body: bytes, **headers
) -> tuple[int, dict]:
    response = await watch["client"].post(
        f"/api/wristtalk/v1/command/{session}/{offset}",
        data=body,
        headers={**watch["headers"], **headers},
    )
    return response.status, await response.json()


async def test_watch_device_and_entities(hass: HomeAssistant, entry, watch) -> None:
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert (DOMAIN, WATCH["unique_id"]) in device.identifiers
    assert device.model == WATCH["model"]
    assert device.serial_number == WATCH["unique_id"][-8:]
    assert device.sw_version == WATCH["app"]
    entities = {
        e.domain for e in er.async_entries_for_device(er.async_get(hass), device.id)
    }
    assert entities == {"assist_satellite", "select", "sensor"}
    # A system user identifies the commands
    user = await hass.auth.async_get_user(entry.data[CONF_USER_ID])
    assert user is not None
    assert user.system_generated
    assert user.name == "WristTalk"


async def test_command_end_to_end(hass: HomeAssistant, watch, pipeline, entry) -> None:
    assert await _chunk(watch, "a1", 0, b"abc") == (
        200,
        {"state": "listening", "speech": False, "protocol": 3},
    )
    status, body = await _chunk(watch, "a1", 3, b"def" + END)
    assert (status, body["state"], body["heard"]) == (
        200,
        "heard",
        "Która jest godzina?",
    )

    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert await reply.json() == {"reply": "Jest 21:37.", "audio": True, "protocol": 3}

    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 200
    assert audio.headers["Content-Type"] == "application/octet-stream"
    assert await audio.read() == REPLY_AUDIO

    (run,) = pipeline.runs
    assert run["audio"] == b"abcdef" + END
    # The pipeline stops at the agent; the integration speaks the reply text itself (the
    # pipeline's streaming TTS stops at "Checking ..." before a tool - core#187933)
    assert run["end_stage"] == PipelineStage.INTENT
    assert run["user_id"] == entry.data[CONF_USER_ID]
    assert pipeline.synthesized == ["Jest 21:37."]
    ((engine, language, options),) = pipeline.tts_calls
    assert (engine, language) == ("tts.fake", "pl")
    assert options == {
        "preferred_format": "opus",
        "preferred_sample_rate": 16000,
        "preferred_sample_channels": 1,
        "voice": "MarekNeural",
    }
    # No instruction is added to the agent (like Voice PE)
    assert run["conversation_extra_system_prompt"] is None
    # Chunks after "heard" are ignored without an error
    assert (await _chunk(watch, "a1", 12, b"late"))[0] == 200


async def test_text_only_skips_tts(hass: HomeAssistant, watch, pipeline) -> None:
    await _chunk(watch, "a1", 0, b"x" + END, **{"X-WT-Text-Only": "1"})
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert (await reply.json())["audio"] is False
    assert pipeline.synthesized == []
    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 404


async def test_start_ends_the_command(hass: HomeAssistant, watch, pipeline) -> None:
    status, body = await _chunk(watch, "a1", 0, b"speech", **{"X-WT-Final": "1"})
    assert status == 200
    assert body["state"] == "heard"
    assert pipeline.runs[0]["audio"] == b"speech"


async def test_command_cap_closes_the_input(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """No silence (radio, wind): the cap closes the input and the command is what STT gives."""
    with patch("custom_components.wristtalk.session.MAX_COMMAND_SECONDS", 0.05):
        status, body = await _chunk(watch, "a1", 0, b"radio")
        assert status == 200
    assert body["state"] == "heard"
    assert (await _chunk(watch, "a1", 5, b"more"))[1]["state"] == "heard"
    assert pipeline.runs[0]["audio"] == b"radio"


async def test_chunks_out_of_order_and_duplicates(
    hass: HomeAssistant, watch, pipeline
) -> None:
    await _chunk(watch, "a1", 3, b"def")
    await _chunk(watch, "a1", 0, b"abc")
    await _chunk(watch, "a1", 0, b"abc")  # repeated after a lost response
    await _chunk(watch, "a1", 6, END)
    assert pipeline.runs[0]["audio"] == b"abcdef" + END


async def test_nothing_heard(hass: HomeAssistant, watch, pipeline) -> None:
    pipeline.errors = ["stt-no-text-recognized"]
    status, body = await _chunk(watch, "a1", 0, END)
    assert status == 200
    assert body["state"] == "nothing"
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert (await reply.json())["state"] == "nothing"


async def test_stt_failure_ends_the_command_with_one_run(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """No STT retry (removed 2026-11-07: it got the same recording and rescued none of 20
    commands): one pipeline run, the watch hears "nothing" at once."""
    pipeline.errors = ["stt-stream-failed"]
    status, body = await _chunk(watch, "a1", 0, b"abc" + END)
    assert (status, body["state"]) == (200, "nothing")
    assert [run["audio"] for run in pipeline.runs] == [b"abc" + END]


async def test_stt_failure_is_nothing_heard_for_the_reply_too(
    hass: HomeAssistant, watch, pipeline
) -> None:
    pipeline.errors = ["stt-stream-failed"]
    assert (await _chunk(watch, "a1", 0, END))[1]["state"] == "nothing"
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert (await reply.json())["state"] == "nothing"
    assert len(pipeline.runs) == 1


async def test_third_stt_failure_in_a_row_is_an_error(
    hass: HomeAssistant, watch, pipeline
) -> None:
    # One run per command; a recognised one in between resets the count
    pipeline.errors = ["stt-stream-failed"] * 2
    assert (await _chunk(watch, "a1", 0, END))[1]["state"] == "nothing"
    assert (await _chunk(watch, "a2", 0, END))[1]["state"] == "nothing"
    assert (await _chunk(watch, "a3", 0, END))[1]["state"] == "heard"
    pipeline.errors = ["stt-stream-failed"] * 3
    for session in ("a4", "a5"):
        assert (await _chunk(watch, session, 0, END))[1]["state"] == "nothing"
    assert await _chunk(watch, "a6", 0, END) == (
        424,
        {"error": "stt_failed", "protocol": 3},
    )


async def test_agent_failure(hass: HomeAssistant, watch, pipeline) -> None:
    pipeline.errors = ["intent-failed"]
    status, body = await _chunk(watch, "a1", 0, END)
    assert (status, body["error"]) == (503, "intent-failed")
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert reply.status == 503
    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 503


async def test_reply_long_poll_waits(hass: HomeAssistant, watch, pipeline) -> None:
    pipeline.hold_reply = asyncio.Event()
    await _chunk(watch, "a1", 0, END)
    with patch("custom_components.wristtalk.session.LONG_POLL_SECONDS", 0.05):
        reply = await watch["client"].post(
            "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
        )
        assert (await reply.json())["state"] == "waiting"
    pipeline.hold_reply.set()
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert (await reply.json())["reply"] == "Jest 21:37."


async def test_new_command_replaces_an_unfinished_one(
    hass: HomeAssistant, watch, pipeline
) -> None:
    # The watch gave up on silence by itself and the user pressed START again
    await _chunk(watch, "a1", 0, b"a")
    status, body = await _chunk(watch, "a2", 0, END)
    assert (status, body["state"]) == (200, "heard")
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert reply.status == 404
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a2", headers=watch["headers"]
    )
    assert (await reply.json())["reply"] == "Jest 21:37."


async def test_the_replaced_command_ends_before_the_new_one(
    hass: HomeAssistant, watch, pipeline
) -> None:
    # The given-up command's pipeline still reports "nothing recognised" while it is cancelled;
    # that must not reach the new command (silence, START, a command -> an error)
    pipeline.nothing_on_cut = True
    await _chunk(watch, "a1", 0, b"a")
    status, body = await _chunk(watch, "a2", 0, END)
    assert (status, body) == (
        200,
        {"state": "heard", "heard": "Która jest godzina?", "protocol": 3},
    )


async def test_rate_limit(hass: HomeAssistant, watch, pipeline) -> None:
    with patch("custom_components.wristtalk.session.COMMANDS_PER_MINUTE", 2):
        for session in ("a1", "a2"):
            await _chunk(watch, session, 0, END)
            await watch["client"].get(
                f"/api/wristtalk/v1/audio/{session}", headers=watch["headers"]
            )
            await hass.async_block_till_done()
        assert (await _chunk(watch, "a3", 0, END)) == (
            429,
            {"error": "too_many_commands", "protocol": 3},
        )


async def test_limits(hass: HomeAssistant, watch, pipeline) -> None:
    status, body = await _chunk(watch, "a1", 0, b"x" * (16 * 1024 + 1))
    assert (status, body["error"]) == (413, "chunk_too_large")
    await _chunk(watch, "a1", 0, END)
    with patch("custom_components.wristtalk.session.MAX_AUDIO_BYTES", 100):
        audio = await watch["client"].get(
            "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
        )
    assert (audio.status, (await audio.json())["error"]) == (413, "audio_too_large")


async def test_requests_need_the_key_and_a_known_session(
    hass: HomeAssistant, watch, pipeline
) -> None:
    client = watch["client"]
    assert (
        await client.post("/api/wristtalk/v1/command/a1/0", data=b"x", headers=HEADERS)
    ).status == 401
    assert (
        await client.post("/api/wristtalk/v1/reply/a1", headers=HEADERS)
    ).status == 401
    assert (
        await client.get("/api/wristtalk/v1/audio/a1", headers=HEADERS)
    ).status == 401
    assert (
        await client.post("/api/wristtalk/v1/reply/ff", headers=watch["headers"])
    ).status == 404
    assert (
        await client.get("/api/wristtalk/v1/audio/ff", headers=watch["headers"])
    ).status == 404
    bad = await client.post(
        "/api/wristtalk/v1/command/a1/x", data=b"x", headers=watch["headers"]
    )
    assert bad.status == 400
    assert (
        await client.post("/api/wristtalk/v1/command/a1/0", data=b"x", headers={})
    ).status == 400


async def test_new_watch_starts_with_the_default_pipeline(
    hass: HomeAssistant, watch, entry
) -> None:
    """Pipelines exist only as the fake ones here; the select keeps "preferred" if the default is
    unknown to Home Assistant."""
    (subentry,) = entry.subentries.values()
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{subentry.subentry_id}-pipeline"
    )
    assert hass.states.get(entity_id).state == "preferred"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed (runs in CI)"
)
async def test_real_ffmpeg_decodes_ogg_opus_in_chunks() -> None:
    """Home Assistant's decoding path with a real ffmpeg: Ogg/Opus 16 kHz mono in 777-byte cuts
    (inside Ogg pages) -> PCM 16 kHz, 16 bit, mono, while the input is still arriving."""
    ogg = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-f",
            "ogg",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    decoder = FfmpegDecoder("ffmpeg")
    await decoder.start()
    received = bytearray()
    before_end = 0

    async def read() -> None:
        async for chunk in decoder.pcm():
            received.extend(chunk)

    reader = asyncio.create_task(read())
    for start in range(0, len(ogg), 777):
        await decoder.feed(ogg[start : start + 777])
        await asyncio.sleep(0.05)
    before_end = len(received)
    await decoder.close_input()
    await reader
    await decoder.stop()
    assert abs(len(received) / 32000 - 2.0) < 0.1
    assert before_end > 0


async def test_pipeline_crash_is_reported(hass: HomeAssistant, watch, pipeline) -> None:
    pipeline.raise_error = True
    status, body = await _chunk(watch, "a1", 0, END)
    assert (status, body["error"]) == (503, "pipeline_failed")


async def test_stt_slower_than_the_wait(hass: HomeAssistant, watch, pipeline) -> None:
    pipeline.hold_stt = asyncio.Event()
    with patch("custom_components.wristtalk.session.HEARD_WAIT_SECONDS", 0.05):
        assert (await _chunk(watch, "a1", 0, END))[1]["state"] == "listening"
    pipeline.hold_stt.set()
    assert (await _chunk(watch, "a1", 9, b""))[1]["state"] == "heard"


async def test_pipeline_without_tts(hass: HomeAssistant, watch, pipeline) -> None:
    """A pipeline without a TTS engine: the reply text only."""
    pipeline.tts_engine = None
    await _chunk(watch, "a1", 0, END)
    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 404


async def test_satellite_not_ready(hass: HomeAssistant, watch, entry) -> None:
    entry.runtime_data.satellites.clear()
    status, body = await _chunk(watch, "a1", 0, END)
    assert (status, body["error"]) == (503, "not_ready")


async def test_satellite_has_no_wake_words(hass: HomeAssistant, watch, entry) -> None:
    (satellite,) = entry.runtime_data.satellites.values()
    config = satellite.async_get_configuration()
    assert config.available_wake_words == []
    assert config.max_active_wake_words == 0
    await satellite.async_set_configuration(config)


async def test_rate_window_slides(hass: HomeAssistant, watch, pipeline, entry) -> None:
    """A start older than a minute no longer counts."""
    (subentry_id,) = entry.subentries
    entry.runtime_data.sessions._starts[subentry_id] = deque([time.monotonic() - 120])
    with patch("custom_components.wristtalk.session.COMMANDS_PER_MINUTE", 1):
        assert (await _chunk(watch, "a1", 0, END))[0] == 200


async def test_removing_the_integration_removes_its_user(
    hass: HomeAssistant, entry
) -> None:
    user_id = entry.data[CONF_USER_ID]
    assert await hass.auth.async_get_user(user_id) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.auth.async_get_user(user_id) is None


async def test_new_watch_gets_the_integration_default_pipeline(
    hass: HomeAssistant, entry, pipeline, hass_client_no_auth
) -> None:
    """A real pipeline of the test Home Assistant as the integration's default."""
    real = async_get_pipelines(hass)[0]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PIPELINE: real.id}
    )
    await hass.async_block_till_done()
    await watch_fixture(hass, entry, hass_client_no_auth)
    (subentry,) = entry.subentries.values()
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{subentry.subentry_id}-pipeline"
    )
    assert hass.states.get(entity_id).state == real.name


async def test_speech_start_is_reported(hass: HomeAssistant, watch, pipeline) -> None:
    """The watch's "nothing said" timer needs to know that speech has started."""
    assert (await _chunk(watch, "a1", 0, b"..."))[1]["speech"] is False
    status, body = await _chunk(watch, "a1", 3, SPEECH)
    assert status == 200
    assert (body["state"], body["speech"]) == ("listening", True)


async def test_synthesis_failure_leaves_the_text(
    hass: HomeAssistant, watch, pipeline
) -> None:
    with patch(
        "homeassistant.components.tts.async_create_stream",
        side_effect=RuntimeError("tts down"),
    ):
        await _chunk(watch, "a1", 0, END)
        reply = await watch["client"].post(
            "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
        )
        assert (await reply.json())["reply"] == "Jest 21:37."
        audio = await watch["client"].get(
            "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
        )
    assert audio.status == 404


# --- a watch removed in Home Assistant (its key revoked) at each stage of a command ---------------


async def _remove_watch(hass: HomeAssistant, entry) -> None:
    (subentry_id,) = entry.subentries
    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()


async def test_removed_while_listening(
    hass: HomeAssistant, entry, watch, pipeline
) -> None:
    assert (await _chunk(watch, "a1", 0, b"abc"))[0] == 200
    await _remove_watch(hass, entry)
    assert await _chunk(watch, "a1", 3, b"def") == (
        401,
        {"error": "unauthorized", "protocol": 3},
    )


async def test_removed_while_the_agent_thinks(
    hass: HomeAssistant, entry, watch, pipeline
) -> None:
    # The command is heard and with the agent: removing the watch must stop it before it acts
    pipeline.hold_reply = asyncio.Event()
    assert (await _chunk(watch, "a1", 0, END))[1]["state"] == "heard"
    await _remove_watch(hass, entry)
    assert pipeline.runs[0].get("cancelled") is True
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert reply.status == 401


async def test_removed_before_the_audio(
    hass: HomeAssistant, entry, watch, pipeline
) -> None:
    await _chunk(watch, "a1", 0, END)
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert (await reply.json())["reply"] == "Jest 21:37."
    await _remove_watch(hass, entry)
    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 401


async def test_removed_then_the_next_command(
    hass: HomeAssistant, entry, watch, pipeline
) -> None:
    await _remove_watch(hass, entry)
    ping = await watch["client"].post(
        "/api/wristtalk/v1/ping", headers=watch["headers"]
    )
    assert ping.status == 401
    assert (await _chunk(watch, "a2", 0, b"a"))[0] == 401
    assert pipeline.runs == []


async def test_removed_while_recognising(
    hass: HomeAssistant, entry, watch, pipeline
) -> None:
    # START sent the end of the command; STT still runs - the command never reaches the agent
    pipeline.hold_stt = asyncio.Event()
    request = asyncio.ensure_future(_chunk(watch, "a1", 0, END, **{"X-WT-Final": "1"}))
    await asyncio.sleep(0.05)
    await _remove_watch(hass, entry)
    pipeline.hold_stt.set()
    await hass.async_block_till_done()
    # Answered at once (not after the STT wait), with what makes the watch pair again
    assert await request == (401, {"error": "unauthorized", "protocol": 3})
    assert pipeline.synthesized == []
    reply = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"]
    )
    assert reply.status == 401


async def test_reply_audio_stays_as_it_came_when_ffmpeg_cannot_compress_it() -> None:
    """No ffmpeg, or audio it cannot read: the watch still gets the reply's voice."""
    assert (
        await async_compress_speech("/nonexistent/ffmpeg", REPLY_AUDIO, "16k")
        == REPLY_AUDIO
    )
    if shutil.which("ffmpeg"):
        assert await async_compress_speech("ffmpeg", b"not ogg", "16k") == b"not ogg"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed (runs in CI)"
)
async def test_reply_audio_is_compressed_to_speech_bitrate() -> None:
    """A reply as a TTS engine gives it (Ogg/Opus at a music bitrate) -> Ogg/Opus 16 kbit/s, smaller."""
    ogg = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            "-f",
            "ogg",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    compressed = await async_compress_speech("ffmpeg", ogg, "16k")
    assert compressed.startswith(b"OggS")
    assert len(compressed) < len(ogg) / 3


async def test_audio_answers_waiting_while_the_voice_is_made(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """No request is held long through the phone: 202 while synthesis runs, the watch asks again."""
    pipeline.hold_tts = asyncio.Event()
    await _chunk(watch, "a1", 0, END)
    await watch["client"].post("/api/wristtalk/v1/reply/a1", headers=watch["headers"])
    with patch("custom_components.wristtalk.session.LONG_POLL_SECONDS", 0.05):
        audio = await watch["client"].get(
            "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
        )
        assert audio.status == 202
    pipeline.hold_tts.set()
    audio = await watch["client"].get(
        "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
    )
    assert audio.status == 200


async def test_reply_voice_quality_per_watch(
    hass: HomeAssistant, watch, pipeline, entry
) -> None:
    """The watch's select sets the Opus bitrate; "Fast" (10 kbit/s) until changed."""
    (subentry_id,) = entry.subentries
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{subentry_id}-speech_quality"
    )
    assert hass.states.get(entity_id).state == "fast"
    bitrates: list[str] = []

    async def compress(binary: str, audio: bytes, bitrate: str) -> bytes:
        bitrates.append(bitrate)
        return audio

    with patch(
        "custom_components.wristtalk.assist_satellite.async_compress_speech", compress
    ):
        await _chunk(watch, "a1", 0, END)
        await watch["client"].get(
            "/api/wristtalk/v1/audio/a1", headers=watch["headers"]
        )
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "best"},
            blocking=True,
        )
        await _chunk(watch, "a2", 0, END)
        await watch["client"].get(
            "/api/wristtalk/v1/audio/a2", headers=watch["headers"]
        )
    assert bitrates == ["10k", "24k"]


async def test_a_late_chunk_of_an_ended_command_starts_nothing(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """Through the phone a chunk of the last command can arrive after the next one has started:
    it gets 410 and neither ends the current command nor runs the pipeline again."""
    pipeline.hold_reply = asyncio.Event()
    assert (await _chunk(watch, "a1", 0, b"abc" + END))[1]["state"] == "heard"
    assert (await _chunk(watch, "a2", 0, b"x"))[1]["state"] == "listening"
    for offset in (12, 0):
        status, body = await _chunk(watch, "a1", offset, b"late" + END)
        assert status == 410
        assert body["error"] == "session_ended"
    assert len(pipeline.runs) == 2
    # The current command goes on
    assert (await _chunk(watch, "a2", 1, END))[1]["state"] == "heard"
    pipeline.hold_reply.set()


async def test_a_repeated_final_runs_the_pipeline_once(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """The watch repeats a chunk whose answer was lost - "final" too, during the run and after it."""
    pipeline.hold_stt = asyncio.Event()
    first = asyncio.ensure_future(_chunk(watch, "a1", 0, b"abc", **{"X-WT-Final": "1"}))
    await asyncio.sleep(0.05)
    again = asyncio.ensure_future(_chunk(watch, "a1", 0, b"abc", **{"X-WT-Final": "1"}))
    await asyncio.sleep(0.05)
    pipeline.hold_stt.set()
    for answer in (await first, await again):
        assert answer[1]["state"] == "heard"
    # After the run: the same answer again, no second run
    status, body = await _chunk(watch, "a1", 0, b"abc", **{"X-WT-Final": "1"})
    assert (status, body["state"], body["heard"]) == (
        200,
        "heard",
        "Która jest godzina?",
    )
    assert len(pipeline.runs) == 1
    assert pipeline.runs[0]["audio"] == b"abc"


async def test_each_run_names_its_command(hass: HomeAssistant, watch, pipeline) -> None:
    """A log of pipeline runs can count them per command: the event carries the watch's session."""
    events = async_capture_events(hass, "wristtalk_command_run")
    pipeline.errors = ["stt-stream-failed"]  # a failed run is a run too
    await _chunk(watch, "a1", 0, END)
    await _chunk(watch, "a2", 0, END)
    await hass.async_block_till_done()
    assert [e.data["session_id"] for e in events] == ["a1", "a2"]
    assert all(e.data["run_start"] for e in events)
    assert events[0].data["entity_id"].startswith("assist_satellite.")


async def test_recording_limit(hass: HomeAssistant, watch, pipeline) -> None:
    """The whole recording, chunks waiting for a missing one included, stays within the limit."""
    with patch("custom_components.wristtalk.session.MAX_RECORDING_BYTES", 10):
        assert (await _chunk(watch, "a1", 0, b"abcdef"))[0] == 200
        assert await _chunk(watch, "a1", 6, b"ghijk") == (
            413,
            {"error": "recording_too_large", "protocol": 3},
        )
    # The input is closed - the command is what came before
    assert (await _chunk(watch, "a1", 6, b""))[1]["state"] == "heard"
    assert pipeline.runs[0]["audio"] == b"abcdef"


async def test_recording_limit_counts_waiting_chunks(
    hass: HomeAssistant, watch, pipeline
) -> None:
    with patch("custom_components.wristtalk.session.MAX_RECORDING_BYTES", 10):
        assert (await _chunk(watch, "a1", 3, b"defg"))[0] == 200  # waits for 0-2
        status, body = await _chunk(watch, "a1", 20, b"x")
    assert (status, body["error"]) == (413, "recording_too_large")


async def test_waiting_chunks_limit(hass: HomeAssistant, watch, pipeline) -> None:
    """Chunks out of order wait for the missing one - six at most (the watch sends two at a time)."""
    for n in range(1, 7):
        assert (await _chunk(watch, "a1", n * 10, b"x"))[0] == 200
    # The same offset again replaces the waiting chunk
    assert (await _chunk(watch, "a1", 60, b"x"))[0] == 200
    assert await _chunk(watch, "a1", 70, b"x") == (
        413,
        {"error": "too_many_chunks", "protocol": 3},
    )


async def test_session_id_and_offset_are_checked(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """Only the hex session ids the watch draws (up to 32 characters) and offsets from 0."""
    client = watch["client"]
    for session in ("not-hex", "S1", "a" * 33, "a%0Ab"):
        response = await client.post(
            f"/api/wristtalk/v1/command/{session}/0",
            data=b"x",
            headers=watch["headers"],
        )
        assert response.status == 400, session
        assert (
            await client.post(
                f"/api/wristtalk/v1/reply/{session}", headers=watch["headers"]
            )
        ).status == 400
        assert (
            await client.get(
                f"/api/wristtalk/v1/audio/{session}", headers=watch["headers"]
            )
        ).status == 400
    assert (await _chunk(watch, "a1", -1, b"x"))[0] == 400
    assert pipeline.runs == []
    # Without the key the session is not looked at: 401 as before
    assert (
        await client.post(
            "/api/wristtalk/v1/command/not-hex/0", data=b"x", headers=HEADERS
        )
    ).status == 401


class SlowStartDecoder(PassthroughDecoder):
    """Like ffmpeg: starting takes a moment, and feeding it before that fails."""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self._release = release
        self.started = False

    async def start(self) -> None:
        await self._release.wait()
        self.started = True

    async def feed(self, data: bytes) -> None:
        assert self.started, "fed before the process started"
        await super().feed(data)


async def test_two_first_chunks_at_once_wait_for_the_decoder(
    hass: HomeAssistant, watch, pipeline
) -> None:
    """The watch can send its first two chunks at once: the second must wait until the command's
    decoder has started, not fail with 500 (seen twice in 50 commands, 2026-11-06/07)."""
    release = asyncio.Event()
    with patch(
        "custom_components.wristtalk.create_decoder",
        lambda hass: SlowStartDecoder(release),
    ):
        # Two at a time through the phone: the second chunk overtook the first and started the
        # command; the first one (offset 0) goes straight to the decoder
        second = asyncio.ensure_future(_chunk(watch, "a1", 3, b"def" + END))
        await asyncio.sleep(0.05)
        first = asyncio.ensure_future(_chunk(watch, "a1", 0, b"abc"))
        await asyncio.sleep(0.05)
        release.set()
        results = [await second, await first]
    assert [status for status, _ in results] == [200, 200]
    assert "heard" in {body["state"] for _, body in results}
    assert pipeline.runs[0]["audio"] == b"abcdef" + END
