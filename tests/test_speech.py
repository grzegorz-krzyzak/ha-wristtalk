"""The reply's voice stream on its own: Ogg pages as they come out of ffmpeg."""

from __future__ import annotations

import asyncio
import shutil
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.wristtalk.speech import (
    FfmpegEncoder,
    SpeechError,
    SpeechStream,
    create_encoder,
)

from .test_command import HEADER_PAGES, SECOND, ogg_page


class ScriptedEncoder:
    """ffmpeg's output as the test hands it out, whatever goes in."""

    def __init__(self) -> None:
        self.out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.ok = True

    async def start(self) -> None:
        pass

    async def feed(self, data: bytes) -> None:
        pass

    async def close_input(self) -> None:
        pass

    async def output(self):
        while (chunk := await self.out.get()) is not None:
            yield chunk

    async def succeeded(self) -> bool:
        return self.ok

    async def stop(self) -> None:
        self.ok = False
        await self.out.put(None)


async def _nothing(text: str):
    return
    yield


async def _stream(hass: HomeAssistant) -> tuple[SpeechStream, ScriptedEncoder]:
    encoder = ScriptedEncoder()
    speech = SpeechStream(
        encoder,
        _nothing,
        lambda coro: hass.async_create_background_task(coro, "test voice"),
        "t",
    )
    await speech.start()
    return speech, encoder


def _soon() -> float:
    return asyncio.get_running_loop().time() + 0.05


async def test_pages_cut_anywhere_across_reads(hass: HomeAssistant) -> None:
    speech, encoder = await _stream(hass)
    page = ogg_page(3 * SECOND, b"\x01" * 300)  # two lacing values
    data = HEADER_PAGES + page
    # Cut in the header, in the segment table, in the body
    cuts = [len(HEADER_PAGES) + 5, len(HEADER_PAGES) + 28, len(HEADER_PAGES) + 100]
    start = 0
    for cut in cuts:
        await encoder.out.put(data[start:cut])
        start = cut
        await asyncio.sleep(0)
        assert speech.size == len(HEADER_PAGES)
    await encoder.out.put(data[start:])
    assert await speech.piece(0, _soon()) == data
    await speech.close()


async def test_a_page_without_a_packet_end_keeps_the_speech_so_far(
    hass: HomeAssistant,
) -> None:
    speech, encoder = await _stream(hass)
    await encoder.out.put(
        HEADER_PAGES + ogg_page(2 * SECOND) + ogg_page(-1) + ogg_page(3 * SECOND)
    )
    await asyncio.sleep(0)
    # 3 s from the start: answered at once
    piece = await speech.piece(0, asyncio.get_running_loop().time() + 4)
    assert piece.endswith(ogg_page(3 * SECOND))
    await speech.close()


async def test_not_ogg_from_ffmpeg_cuts_the_voice(hass: HomeAssistant) -> None:
    speech, encoder = await _stream(hass)
    await encoder.out.put(HEADER_PAGES + b"garbage" * 10)
    await asyncio.sleep(0)
    assert speech.cut
    assert await speech.piece(0, _soon()) == HEADER_PAGES
    with pytest.raises(SpeechError) as err:
        await speech.piece(len(HEADER_PAGES), _soon())
    assert (err.value.code, err.value.status) == ("audio_cut", 410)
    await speech.close()


async def test_ffmpeg_failing_without_a_page_is_no_voice(hass: HomeAssistant) -> None:
    speech, encoder = await _stream(hass)
    encoder.ok = False
    await encoder.out.put(None)
    with pytest.raises(SpeechError) as err:
        await speech.piece(0, _soon())
    assert (err.value.code, err.value.status) == ("no_audio", 404)


async def test_a_page_larger_than_a_piece_is_sent_whole(hass: HomeAssistant) -> None:
    speech, encoder = await _stream(hass)
    big = ogg_page(SECOND, b"\x00" * 600)
    await encoder.out.put(big + ogg_page(2 * SECOND, eos=True))
    await encoder.out.put(None)
    with patch("custom_components.wristtalk.speech.MAX_PIECE_BYTES", 100):
        assert await speech.piece(0, _soon()) == big


async def test_an_offset_past_the_end(hass: HomeAssistant) -> None:
    speech, encoder = await _stream(hass)
    await encoder.out.put(HEADER_PAGES + ogg_page(SECOND, eos=True))
    await encoder.out.put(None)
    await speech.whole(asyncio.get_running_loop().time() + 1)
    with pytest.raises(SpeechError) as err:
        await speech.piece(speech.size + 1, _soon())
    assert err.value.status == 400


async def test_whole_waits_then_202(hass: HomeAssistant) -> None:
    speech, _ = await _stream(hass)
    with pytest.raises(SpeechError) as err:
        await speech.whole(_soon())
    assert err.value.status == 202
    await speech.close()


def test_ffmpeg_is_told_the_input_format() -> None:
    args = create_encoder("ffmpeg", "mp3", "10k").args()
    assert args[args.index("-i") - 8 : args.index("-i")] == [
        "-f",
        "mp3",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-fflags",
        "+nobuffer",
    ]
    # An unknown format: ffmpeg finds it itself
    args = FfmpegEncoder("ffmpeg", "xyz", "10k").args()
    assert args[args.index("-i") - 1] == "error"
    assert args[args.index("-b:a") + 1] == "10k"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed (runs in CI)"
)
async def test_closing_stops_a_running_ffmpeg(hass: HomeAssistant) -> None:
    """A new command while ffmpeg waits for the text: the process is killed, not left behind."""
    encoder = FfmpegEncoder("ffmpeg", "mp3", "10k")
    speech = SpeechStream(
        encoder,
        _nothing,
        lambda coro: hass.async_create_background_task(coro, "test voice"),
        "t",
    )
    await speech.start()
    await speech.close()
    assert encoder._process is not None
    assert encoder._process.returncode is not None
    assert speech.cut
