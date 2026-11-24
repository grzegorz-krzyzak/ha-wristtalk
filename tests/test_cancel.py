"""An interrupted command never runs by itself (protocol 7): cancel with a confirmation, and layer 2 -
a session the watch left (no "final") is cancelled after the cap. Safety tests: they always run."""

# ruff: noqa: F811 - the fixtures `watch` and `pipeline` come from test_command

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from .test_command import (  # the fixtures too: a paired watch and the fake pipeline
    END,
    SPEECH,
    FakePipeline,
    PassthroughDecoder,
    _chunk,
    pipeline,  # noqa: F401
    watch,  # noqa: F401
)


async def _cancel(watch, session: str) -> tuple[int, dict]:
    response = await watch["client"].post(
        f"/api/wristtalk/v1/cancel/{session}", headers=watch["headers"]
    )
    return response.status, await response.json()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_cancel_while_listening_stops_the_command_before_the_agent(
    hass: HomeAssistant, watch, pipeline: FakePipeline
) -> None:
    status, body = await _chunk(watch, "a1", 0, SPEECH + b"otworz brame")
    assert body["state"] == "listening"
    assert await _cancel(watch, "a1") == (200, {"state": "cancelled", "protocol": 7})
    await _settle()
    assert len(pipeline.runs) == 1
    assert not pipeline.runs[0].get("agent")
    # Nothing of it starts again
    status, body = await _chunk(watch, "a1", 100, b"x")
    assert (status, body["error"]) == (410, "session_ended")
    assert len(pipeline.runs) == 1


async def test_cancel_after_the_text_reached_the_agent_is_too_late(
    hass: HomeAssistant, watch, pipeline: FakePipeline
) -> None:
    pipeline.hold_reply = asyncio.Event()
    _, body = await _chunk(watch, "a1", 0, SPEECH + b"x" + END)
    assert body["state"] == "heard"
    assert await _cancel(watch, "a1") == (
        200,
        {"state": "too_late", "heard": pipeline.heard, "protocol": 7},
    )
    # The command goes on: the agent is not cut, its reply comes as usual
    pipeline.hold_reply.set()
    response = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"], json={}
    )
    assert (await response.json())["reply"] == pipeline.reply
    assert not pipeline.runs[0].get("cancelled")


async def test_cancel_of_an_unknown_session_keeps_its_late_chunks_from_starting_it(
    hass: HomeAssistant, watch, pipeline: FakePipeline
) -> None:
    # BACK right after the start: cancel reaches the house before the first chunk
    assert await _cancel(watch, "a9") == (200, {"state": "cancelled", "protocol": 7})
    status, body = await _chunk(watch, "a9", 0, SPEECH + b"x" + END)
    assert (status, body["error"]) == (410, "session_ended")
    assert pipeline.runs == []


async def test_cancel_twice_gives_the_same_answer(
    hass: HomeAssistant, watch, pipeline: FakePipeline
) -> None:
    await _chunk(watch, "a1", 0, SPEECH + b"x")
    assert (await _cancel(watch, "a1"))[1]["state"] == "cancelled"
    assert (await _cancel(watch, "a1"))[1]["state"] == "cancelled"
    pipeline.hold_reply = asyncio.Event()
    await _chunk(watch, "a2", 0, SPEECH + b"x" + END)
    assert (await _cancel(watch, "a2"))[1]["state"] == "too_late"
    assert (await _cancel(watch, "a2"))[1]["state"] == "too_late"
    pipeline.hold_reply.set()


@pytest.fixture
def short_cap():
    """The cap and the wait for "final" after it, short (the first chunk itself answers after 0.2 s)."""
    with (
        patch("custom_components.wristtalk.session.MAX_COMMAND_SECONDS", 0.3),
        patch("custom_components.wristtalk.session.FINAL_WAIT_SECONDS", 0.5),
    ):
        yield


@pytest.mark.parametrize("left_at", [0.0, 0.25])
async def test_a_command_the_watch_left_is_cancelled_after_the_cap(
    hass: HomeAssistant, watch, pipeline: FakePipeline, short_cap, left_at: float
) -> None:
    # The watch stops sending (BACK without a connection, the menu, the background, a crash) - at
    # once, or just before the cap (the watch stopped a few seconds before it); no "final" comes
    await _chunk(watch, "a1", 0, SPEECH + b"otworz brame")
    if left_at:
        await asyncio.sleep(left_at - 0.2)  # the first chunk's answer took 0.2 s
        await _chunk(
            watch, "a1", 20, b" wjazdowa"
        )  # the last chunk, just before the cap
    await asyncio.sleep(1.2)
    await _settle()
    assert len(pipeline.runs) == 1
    assert not pipeline.runs[0].get("agent")


async def test_final_within_the_wait_after_the_cap_runs_the_command(
    hass: HomeAssistant, watch, pipeline: FakePipeline, short_cap
) -> None:
    # Noise: no silence for the VAD; the watch ends at its own limit and sends "final"
    await _chunk(watch, "a1", 0, SPEECH + b"noise")
    await asyncio.sleep(0.4)  # past the cap, within the wait
    status, body = await _chunk(watch, "a1", 11, b"more", **{"X-WT-Final": "1"})
    assert body.get("state") == "heard", (status, body)
    assert pipeline.runs[0].get("agent")


async def test_a_late_final_after_the_watch_was_considered_gone_leaves_a_warning(
    hass: HomeAssistant,
    watch,
    pipeline: FakePipeline,
    short_cap,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _chunk(watch, "a1", 0, SPEECH + b"noise")
    await asyncio.sleep(1.3)
    await _settle()
    with caplog.at_level(logging.WARNING):
        status, body = await _chunk(watch, "a1", 11, b"more", **{"X-WT-Final": "1"})
    assert (status, body["error"]) == (410, "session_ended")
    assert "after the watch was considered gone" in caplog.text
    assert not pipeline.runs[0].get("agent")


async def test_the_cap_never_cuts_a_command_already_with_the_agent(
    hass: HomeAssistant, watch, pipeline: FakePipeline, short_cap
) -> None:
    # Speech-to-text ended by itself (noise, the cloud) and the agent still works at the cap
    pipeline.hold_reply = asyncio.Event()
    _, body = await _chunk(watch, "a1", 0, SPEECH + b"x" + END)
    assert body["state"] == "heard"
    await asyncio.sleep(1.3)
    assert not pipeline.runs[0].get("cancelled")
    pipeline.hold_reply.set()
    response = await watch["client"].post(
        "/api/wristtalk/v1/reply/a1", headers=watch["headers"], json={}
    )
    assert (await response.json())["reply"] == pipeline.reply


class _SlowStoppingDecoder(PassthroughDecoder):
    """ffmpeg's stop takes a moment (kill, then wait), and its input closes cleanly first - the end
    of the audio looks to the pipeline like the end of a recording."""

    async def stop(self) -> None:
        await self.close_input()
        for _ in range(5):
            await asyncio.sleep(0)
        await super().stop()


@pytest.fixture
def slow_stop():
    with patch(
        "custom_components.wristtalk.create_decoder",
        lambda hass: _SlowStoppingDecoder(),
    ):
        yield


async def test_a_cancel_never_hands_the_cut_audio_to_the_agent(
    hass: HomeAssistant, watch, pipeline: FakePipeline, slow_stop
) -> None:
    # Closing the input before the pipeline is stopped would let speech-to-text take what came
    _, body = await _chunk(watch, "a1", 0, SPEECH + b"otworz brame")
    assert body["state"] == "listening"
    assert await _cancel(watch, "a1") == (200, {"state": "cancelled", "protocol": 7})
    await _settle()
    assert not pipeline.runs[0].get("agent")


async def test_layer_2_never_hands_the_cut_audio_to_the_agent(
    hass: HomeAssistant, watch, pipeline: FakePipeline, short_cap, slow_stop
) -> None:
    await _chunk(watch, "a1", 0, SPEECH + b"otworz brame")
    await asyncio.sleep(1.2)
    await _settle()
    assert not pipeline.runs[0].get("agent")


async def test_a_repeated_cancel_that_was_too_late_still_carries_the_heard_text(
    hass: HomeAssistant, watch, pipeline: FakePipeline
) -> None:
    # The watch sends the cancel again when its first answer was lost on the way
    _, body = await _chunk(watch, "a1", 0, SPEECH + b"x" + END)
    assert body["state"] == "heard"
    await hass.async_block_till_done()
    expected = (200, {"state": "too_late", "heard": pipeline.heard, "protocol": 7})
    assert await _cancel(watch, "a1") == expected
    await _chunk(watch, "a2", 0, SPEECH)  # a new command: a1 is closed and remembered
    assert await _cancel(watch, "a1") == expected
