"""The reply's voice while it is being made: TTS -> ffmpeg -> Ogg/Opus pages for the watch.

The TTS engine's audio goes into ffmpeg as it comes, and ffmpeg's small Ogg pages (0.2 s each) are
kept as they come out, so the watch can start playing while the rest is still being made. ffmpeg
is started before the reply text is known (when the agent starts): its start takes about 0.45 s on
a Home Assistant Green, measured 2026-11-10, hidden behind the agent's thinking.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import AsyncIterator, Callable, Coroutine
import contextlib
import logging
import struct
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)

# Ogg/Opus granules count 48 kHz samples
GRANULES_PER_SECOND = 48000
# The first answer waits for this much speech (or the end): the watch starts playing with it in
# hand and no stall followed at the phone's pace (watch test 3d, 2026-11-10); the cloud gives whole
# sentences, so it is there 0-0.06 s after the first byte (measured 2026-11-10)
START_MARGIN_SECONDS = 3
# One answer carries at most this much (whole pages)
MAX_PIECE_BYTES = 64 * 1024
# ... and once the watch plays (a later answer) at most this much: received while playing, pieces of
# 12-26 kB crackled now and then on the fēnix 8, up to 6.5 kB never (2026-11-10/11); 8 kB is the
# watch player's buffer (about 5.5 s of speech at 10 kbit/s)
MAX_LATER_PIECE_BYTES = 8 * 1024
# The whole voice; past it the voice is cut (the simulator accepted 400 kB; 256 kB is about 3.5 min
# of speech at 10 kbit/s)
MAX_AUDIO_BYTES = 256 * 1024
OUTPUT_CHUNK = 4096
# Synthesis giving nothing this long is given up: what came is a cut voice (the watch gives up on
# its own sooner; this ends ffmpeg and the old request's wait)
SYNTHESIS_STALL_SECONDS = 10.0
# Ogg page header: "OggS", version, flags, granule (int64), serial, sequence, CRC, segment count
_PAGE_HEADER = 27
_FLAG_END_OF_STREAM = 0x04
# The TTS's extension -> ffmpeg's input format: named, ffmpeg starts encoding at once instead of
# probing the input first (0.3 s, measured on a Mac with MP3, 2026-11-10)
INPUT_FORMATS = {
    "mp3": "mp3",
    "wav": "wav",
    "ogg": "ogg",
    "opus": "ogg",
    "flac": "flac",
}


class Encoder(Protocol):
    """Feed the TTS engine's audio in, read Ogg/Opus out."""

    async def start(self) -> None: ...

    async def feed(self, data: bytes) -> None: ...

    async def close_input(self) -> None: ...

    def output(self) -> AsyncIterator[bytes]: ...

    async def succeeded(self) -> bool: ...

    async def stop(self) -> None: ...


class FfmpegEncoder:
    """One ffmpeg process: stdin the TTS engine's format, stdout Ogg/Opus 16 kHz mono in small pages."""

    def __init__(self, binary: str, extension: str, bitrate: str) -> None:
        self._binary = binary
        self._extension = extension
        self._bitrate = bitrate
        self._process: asyncio.subprocess.Process | None = None

    def args(self) -> list[str]:
        if (input_format := INPUT_FORMATS.get(self._extension)) is not None:
            source = [
                "-f",
                input_format,
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-fflags",
                "+nobuffer",
            ]
        else:
            source = []  # an unknown format: ffmpeg finds it, a moment later
        return [
            self._binary,
            "-hide_banner",
            "-loglevel",
            "error",
            *source,
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            self._bitrate,
            "-application",
            "voip",
            # The whole speech band (wideband, up to 8 kHz): at a low bitrate Opus narrows it by
            # itself, which sounds "from a cardboard box" (heard at 8 kbit/s, 2026-11-05)
            "-cutoff",
            "8000",
            # Pages of 0.2 s, each written as soon as it is full
            "-page_duration",
            "200000",
            "-flush_packets",
            "1",
            "-f",
            "ogg",
            "pipe:1",
        ]

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self.args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def feed(self, data: bytes) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def close_input(self) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def output(self) -> AsyncIterator[bytes]:
        assert self._process is not None
        assert self._process.stdout is not None
        while chunk := await self._process.stdout.read(OUTPUT_CHUNK):
            yield chunk

    async def succeeded(self) -> bool:
        assert self._process is not None
        return await self._process.wait() == 0

    async def stop(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()
        await self._process.wait()


def create_encoder(binary: str, extension: str, bitrate: str) -> Encoder:
    return FfmpegEncoder(binary, extension, bitrate)


class SpeechError(Exception):
    """No piece to give; `code` and `status` as the watch receives them."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class SpeechStream:
    """The reply's voice as Ogg/Opus pages, growing while it is made.

    The watch asks for the bytes from an offset; answers hold whole pages. How it ends: the last
    page has the end-of-stream flag (`ended`), or the voice is `cut` midway (synthesis or ffmpeg
    failing, the size cap - ffmpeg is stopped, the pages so far stay), or nothing came at all.
    """

    def __init__(
        self,
        encoder: Encoder,
        say: Callable[[str], AsyncIterator[bytes]],
        spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]],
        name: str,
    ) -> None:
        self._encoder = encoder
        self._say = say
        self._spawn = spawn
        self._name = name
        self._data = bytearray()
        self._pages_end = 0  # the end of the last whole page
        self._page_ends: list[int] = []
        self._page_granules: list[int] = []
        self._eos = False  # the last whole page ends the stream
        # Synthesis failed: what ffmpeg has still comes out, as a cut voice
        self._source_failed = False
        self._changed = asyncio.Event()
        self._started = False
        self.ended = False  # the whole voice is here
        self.cut = False  # it stopped midway, or never came
        self.capped = False
        self._tasks: list[asyncio.Task[Any]] = []

    @property
    def done(self) -> bool:
        return self.ended or self.cut

    @property
    def size(self) -> int:
        return self._pages_end

    async def start(self) -> None:
        await self._encoder.start()
        self._started = True
        self._tasks.append(self._spawn(self._read()))

    def speak(self, text: str) -> None:
        """The reply text: the TTS engine's audio goes to ffmpeg as it comes."""
        self._tasks.append(self._spawn(self._feed(text)))

    async def _feed(self, text: str) -> None:
        chunks = aiter(self._say(text))
        try:
            while True:
                try:
                    async with asyncio.timeout(SYNTHESIS_STALL_SECONDS):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                await self._encoder.feed(chunk)
        except (ConnectionResetError, BrokenPipeError):
            return  # ffmpeg is gone: its output tells how it ended
        except Exception:
            # ffmpeg ends as usual and gives what it has; its last page's end flag is left out,
            # so the watch plays it all and then hears "cut", not the end
            _LOGGER.exception("WristTalk %s: speech synthesis failed", self._name)
            self._source_failed = True
        await self._encoder.close_input()

    async def _read(self) -> None:
        async for chunk in self._encoder.output():
            if self.done:
                break
            self._data += chunk
            self._parse_pages()
            self._notify()
        succeeded = await self._encoder.succeeded()
        if not self.done:
            self.ended = succeeded and self._eos and not self._source_failed
            self.cut = not self.ended
        self._notify()

    def _parse_pages(self) -> None:
        data = self._data
        while True:
            start = self._pages_end
            if len(data) - start < _PAGE_HEADER:
                return
            if data[start : start + 4] != b"OggS":
                _LOGGER.warning("WristTalk %s: not an Ogg page from ffmpeg", self._name)
                self._give_up()
                return
            segments = data[start + 26]
            body_at = start + _PAGE_HEADER + segments
            if len(data) < body_at:
                return
            end = body_at + sum(data[start + _PAGE_HEADER : body_at])
            if len(data) < end:
                return
            if end > MAX_AUDIO_BYTES:
                self.capped = True
                self._give_up()
                return
            eos = bool(data[start + 5] & _FLAG_END_OF_STREAM)
            if eos and self._source_failed:
                return  # not the end: the voice was cut
            (granule,) = struct.unpack_from("<q", data, start + 6)
            if granule < 0:  # no packet ends on this page
                granule = self._page_granules[-1] if self._page_granules else 0
            self._page_ends.append(end)
            self._page_granules.append(granule)
            self._eos = eos
            self._pages_end = end

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _granule_at(self, offset: int) -> int:
        """The speech (in granules) before `offset`."""
        index = bisect_right(self._page_ends, offset) - 1
        return self._page_granules[index] if index >= 0 else 0

    def _speech_after(self, offset: int) -> float:
        if not self._page_granules:
            return 0.0
        return (
            self._page_granules[-1] - self._granule_at(offset)
        ) / GRANULES_PER_SECOND

    def _piece_end(self, offset: int) -> int:
        """The end of the last whole page within one answer from `offset`."""
        limit = offset + (MAX_PIECE_BYTES if offset == 0 else MAX_LATER_PIECE_BYTES)
        index = bisect_right(self._page_ends, limit) - 1
        end = self._page_ends[index] if index >= 0 else 0
        if end <= offset:
            # A page larger than an answer (not from ffmpeg's 0.2 s pages): sent whole
            end = self._page_ends[bisect_right(self._page_ends, offset)]
        return end

    async def piece(self, offset: int, deadline: float) -> bytes:
        """The bytes from `offset`, waiting until `deadline` (loop time) for enough of them.

        From the start, the answer waits for START_MARGIN_SECONDS of speech or the end - the watch's
        start margin in one request, and a short reply whole in one request. Later answers give
        whatever new pages there are, as soon as there are any.
        """
        loop = asyncio.get_running_loop()
        while True:
            if offset > self._pages_end and self.done:
                raise SpeechError("invalid_offset", 400)
            have = self._pages_end - offset
            if (
                self.done
                or have >= (MAX_PIECE_BYTES if offset == 0 else MAX_LATER_PIECE_BYTES)
                or (
                    have > 0
                    and (
                        offset > 0 or self._speech_after(offset) >= START_MARGIN_SECONDS
                    )
                )
            ):
                break
            changed = self._changed
            left = deadline - loop.time()
            if left <= 0:
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(left):
                    await changed.wait()
        if self._pages_end > offset:
            return bytes(self._data[offset : self._piece_end(offset)])
        if self.ended:
            raise SpeechError("audio_ended", 410)
        if self.cut:
            if self._pages_end == 0:
                raise SpeechError("no_audio", 404)
            raise SpeechError("audio_cut", 410)
        raise SpeechError("audio_waiting", 202)

    async def whole(self, deadline: float) -> bytes:
        """The whole voice once it has ended (apps on protocols 2-5)."""
        loop = asyncio.get_running_loop()
        while not self.done:
            changed = self._changed
            left = deadline - loop.time()
            if left <= 0:
                raise SpeechError("audio_waiting", 202)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(left):
                    await changed.wait()
        if self.ended:
            return bytes(self._data[: self._pages_end])
        if self.capped:
            raise SpeechError("audio_too_large", 413)
        raise SpeechError("no_audio", 404)

    def _give_up(self) -> None:
        """Cut here: the pages so far stay, ffmpeg is stopped."""
        if not self.done:
            self.cut = True
        self._notify()
        if self._started:
            self._tasks.append(self._spawn(self._encoder.stop()))

    async def close(self) -> None:
        """No more of this voice: ffmpeg stopped, the tasks ended."""
        if not self.done:
            self.cut = True
        self._notify()
        if self._started:
            await self._encoder.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
