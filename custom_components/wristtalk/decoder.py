"""Ogg/Opus from the watch -> PCM 16 kHz, 16 bit, mono for the Assist pipeline.

Home Assistant's own ffmpeg decodes it while the chunks are still arriving (measured on a Home
Assistant Green, ffmpeg 8.1.2: PCM comes out before the input ends, cuts inside Ogg pages do not
matter, a process starts in about 2 ms) - one process per command.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
from typing import Protocol

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

PCM_CHUNK = 3200  # 0.1 s of PCM


class Decoder(Protocol):
    """Feed Ogg/Opus bytes in, read PCM out."""

    async def start(self) -> None: ...

    async def feed(self, data: bytes) -> None: ...

    async def close_input(self) -> None: ...

    def pcm(self) -> AsyncIterator[bytes]: ...

    async def stop(self) -> None: ...


class FfmpegDecoder:
    """One ffmpeg process: stdin Ogg/Opus, stdout s16le 16 kHz mono."""

    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._input_closed = False

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "ogg",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def feed(self, data: bytes) -> None:
        assert self._process is not None and self._process.stdin is not None
        if self._input_closed:
            return
        self._process.stdin.write(data)
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await self._process.stdin.drain()

    async def close_input(self) -> None:
        if self._process is None or self._process.stdin is None or self._input_closed:
            return
        self._input_closed = True
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def pcm(self) -> AsyncIterator[bytes]:
        assert self._process is not None and self._process.stdout is not None
        while chunk := await self._process.stdout.read(PCM_CHUNK):
            yield chunk

    async def stop(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()
        await self._process.wait()


def create_decoder(hass: HomeAssistant) -> Decoder:
    """A decoder using the ffmpeg binary Home Assistant is configured with."""
    return FfmpegDecoder(get_ffmpeg_manager(hass).binary)
