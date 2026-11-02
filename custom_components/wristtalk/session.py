"""One voice command of a watch: chunks in, the command, the reply and its audio out.

The watch posts Ogg/Opus chunks while the user speaks; they go through ffmpeg into the watch's
Assist satellite pipeline. Home Assistant's VAD ends the command; the chunk request that sees it
waits for speech-to-text and returns the command ("heard"). The reply text and the audio follow
on separate requests.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
import contextlib
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.core import HomeAssistant

from .decoder import Decoder

if TYPE_CHECKING:
    from .assist_satellite import WristTalkSatellite

_LOGGER = logging.getLogger(__name__)

MAX_CHUNK_BYTES = 16 * 1024
MAX_COMMAND_SECONDS = 20.0
MAX_AUDIO_BYTES = 256 * 1024
COMMANDS_PER_MINUTE = 30
HEARD_WAIT_SECONDS = 10.0
# The pipeline's VAD sees a chunk a moment after it is written: a short grace lets the very chunk
# on which the command ended come back with the command, instead of the next one a second later
END_GRACE_SECONDS = 0.2
LONG_POLL_SECONDS = 15.0
STT_RETRY_ERROR = "stt-stream-failed"
NOTHING_ERRORS = ("stt-no-text-recognized",)
# STT failing (after the retry) is "nothing heard" twice in a row on a watch - a short noise before
# speech ends the command in the VAD; from the third time an error the watch tells by its status.
# 424 "failed dependency": no proxy on the way (Home Assistant Cloud) sends it.
STT_FAILURES_FOR_ERROR = 3
STT_FAILED = "stt_failed"
STT_FAILED_STATUS = 424


class SessionError(Exception):
    """A command request is refused; `code` is the error the watch receives."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class CommandSession:
    """A command from the first chunk to the delivered reply."""

    hass: HomeAssistant
    satellite: WristTalkSatellite
    session_id: str
    decoder: Decoder
    text_only: bool
    on_closed: Callable[[CommandSession], None] | None = None
    # STT failed (True, returns the failures in a row) or recognised a command (False)
    on_stt: Callable[[bool], int] | None = None
    started: float = field(default_factory=time.monotonic)
    heard: str | None = None
    speech: bool = False  # the pipeline's VAD heard speech start (the watch's "nothing said" timer)
    nothing: bool = False
    reply: str | None = None
    error: str | None = None
    error_status: int = 503
    audio_data: bytes | None = None
    finished: bool = False
    _expected_offset: int = 0
    _pending: dict[int, bytes] = field(default_factory=dict)
    _input_closed: bool = False
    _pcm: bytearray = field(default_factory=bytearray)
    _queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    _heard_event: asyncio.Event = field(default_factory=asyncio.Event)
    _reply_event: asyncio.Event = field(default_factory=asyncio.Event)
    _tts_event: asyncio.Event = field(default_factory=asyncio.Event)
    _ended_event: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    _retry_pending: bool = False
    _retried: bool = False
    _cap_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        await self.decoder.start()
        self._spawn(self._read_pcm())
        self._spawn(self._run(self._live_stream()))
        # End of command without silence (radio, wind): close the input after the cap
        self._cap_handle = self.hass.loop.call_later(
            MAX_COMMAND_SECONDS, lambda: self._spawn(self.close_input())
        )

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        self._tasks.append(self.hass.async_create_background_task(coro, f"wristtalk {self.session_id}"))

    async def _read_pcm(self) -> None:
        async for chunk in self.decoder.pcm():
            self._pcm.extend(chunk)
            await self._queue.put(chunk)
        await self._queue.put(None)

    async def _live_stream(self) -> AsyncIterator[bytes]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk

    async def _kept_stream(self) -> AsyncIterator[bytes]:
        """The same command again, from the PCM kept for one STT retry."""
        for start in range(0, len(self._pcm), 3200):
            yield bytes(self._pcm[start : start + 3200])

    async def _run(self, stream: AsyncIterator[bytes]) -> None:
        try:
            await self.satellite.async_run_command(self, stream)
        except Exception:  # noqa: BLE001 - reported to the watch as an error
            _LOGGER.exception("WristTalk command %s failed", self.session_id)
            self._fail("pipeline_failed")
        if self._retry_pending:
            self._retry_pending = False
            self._retried = True
            await self._run(self._kept_stream())
            return
        self._finish()

    # --- audio in ---------------------------------------------------------------------------

    async def feed(self, offset: int, data: bytes, *, final: bool) -> None:
        """A chunk at its byte offset; chunks may come out of order, duplicates are ignored."""
        if len(data) > MAX_CHUNK_BYTES:
            raise SessionError("chunk_too_large", 413)
        if not self._input_closed and offset >= self._expected_offset:
            self._pending[offset] = data
            while self._expected_offset in self._pending:
                chunk = self._pending.pop(self._expected_offset)
                await self.decoder.feed(chunk)
                self._expected_offset += len(chunk)
        if final:
            await self.close_input()

    async def close_input(self) -> None:
        if self._input_closed:
            return
        self._input_closed = True
        self._ended_event.set()
        if self._cap_handle is not None:
            self._cap_handle.cancel()
        await self.decoder.close_input()

    async def chunk_reply(self) -> dict[str, Any]:
        """The answer to a chunk: listening, or - once the command has ended - the command."""
        if not self._heard_event.is_set() and not self._input_closed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(END_GRACE_SECONDS):
                    await self._ended_event.wait()
        if not self._heard_event.is_set() and not self._input_closed:
            return {"state": "listening", "speech": self.speech}
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(HEARD_WAIT_SECONDS):
                await self._heard_event.wait()
        if self.error is not None:
            raise SessionError(self.error, self.error_status)
        if self.heard:
            return {"state": "heard", "heard": self.heard}
        if self.nothing:
            return {"state": "nothing"}
        return {"state": "listening", "speech": self.speech}

    # --- pipeline events ----------------------------------------------------------------------

    def on_event(self, event: PipelineEvent) -> None:
        data = event.data or {}
        if event.type is PipelineEventType.STT_VAD_START:
            self.speech = True
        elif event.type is PipelineEventType.STT_VAD_END:
            # The pipeline has the command; stop taking audio
            self._spawn(self.close_input())
        elif event.type is PipelineEventType.STT_END:
            self.heard = str(data.get("stt_output", {}).get("text") or "")
            self.nothing = not self.heard
            if self.heard and self.on_stt is not None:
                self.on_stt(False)
            self._heard_event.set()
            self._ended_event.set()
        elif event.type is PipelineEventType.INTENT_END:
            speech = data.get("intent_output", {}).get("response", {}).get("speech", {})
            self.reply = str(speech.get("plain", {}).get("speech") or "")
            self._reply_event.set()
            if self.text_only:
                self._tts_event.set()
            else:
                # Synthesis starts the moment the text is known; audio() only collects it
                self._spawn(self._synthesize(self.reply))
        elif event.type is PipelineEventType.ERROR:
            code = str(data.get("code") or "error")
            if code == STT_RETRY_ERROR and not self._retried and self.heard is None:
                # Seen in practice; the kept PCM gets one more try, invisible to the user
                _LOGGER.debug("WristTalk %s: %s, retrying STT once", self.session_id, code)
                self._retry_pending = True
            elif code == STT_RETRY_ERROR:
                # A failure again: measured with a short noise before speech (the VAD ends the
                # command on it) and with silence only - "say it again" helps, until it keeps failing
                failures = self.on_stt(True) if self.on_stt is not None else 1
                if failures >= STT_FAILURES_FOR_ERROR:
                    self.error_status = STT_FAILED_STATUS
                    self._fail(STT_FAILED)
                else:
                    self.nothing = True
                    self._finish()
            elif code in NOTHING_ERRORS:
                self.nothing = True
                self._finish()
            else:
                self._fail(code)

    async def _synthesize(self, text: str) -> None:
        try:
            self.audio_data = await self.satellite.async_synthesize(text)
        except Exception:  # noqa: BLE001 - the reply text is on the watch already
            _LOGGER.exception("WristTalk %s: speech synthesis failed", self.session_id)
        self._tts_event.set()

    def _fail(self, code: str) -> None:
        self.error = code
        self._finish()

    def _finish(self) -> None:
        self.finished = True
        for event in (self._heard_event, self._reply_event, self._ended_event):
            event.set()
        if self.reply is None or self.text_only:
            self._tts_event.set()

    # --- reply out ----------------------------------------------------------------------------

    async def reply_result(self) -> dict[str, Any]:
        """Long poll for the agent's reply text."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(LONG_POLL_SECONDS):
                await self._reply_event.wait()
        if self.reply is not None:
            return {"reply": self.reply, "audio": not self.text_only}
        if self.error is not None:
            raise SessionError(self.error, self.error_status)
        if self.nothing:
            return {"state": "nothing"}
        return {"state": "waiting"}

    async def audio(self) -> bytes:
        """The ready reply audio (Ogg/Opus), at most MAX_AUDIO_BYTES."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(LONG_POLL_SECONDS):
                await self._tts_event.wait()
        data = self.audio_data
        if data is None:
            if self.error is not None:
                raise SessionError(self.error, self.error_status)
            raise SessionError("no_audio", 404)
        if len(data) > MAX_AUDIO_BYTES:
            raise SessionError("audio_too_large", 413)
        self.satellite.tts_response_finished()
        return data

    async def close(self) -> None:
        # Requests still waiting (a long poll, a chunk waiting for STT) answer now, not at their timeout
        if not self.finished:
            self._fail("closed")
        if self._cap_handle is not None:
            self._cap_handle.cancel()
        await self.close_input()
        await self.decoder.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        # Wait for the pipeline to unwind: what it still reports goes to the satellite's current
        # session, and the next command must not get it
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.on_closed is not None:
            self.on_closed(self)


class SessionManager:
    """The current command of each watch, and its rate limit."""

    def __init__(self, hass: HomeAssistant, decoder_factory: Callable[[HomeAssistant], Decoder]) -> None:
        self._hass = hass
        self._decoder_factory = decoder_factory
        self._sessions: dict[str, CommandSession] = {}
        self._starts: dict[str, deque[float]] = {}
        self._stt_failures: dict[str, int] = {}

    def get(self, watch_id: str, session_id: str) -> CommandSession:
        session = self._sessions.get(watch_id)
        if session is None or session.session_id != session_id:
            raise SessionError("unknown_session", 404)
        return session

    async def get_or_start(
        self, watch_id: str, session_id: str, satellite: WristTalkSatellite, *, text_only: bool
    ) -> CommandSession:
        current = self._sessions.get(watch_id)
        if current is not None and current.session_id == session_id:
            return current
        if current is not None:
            # A watch runs one command at a time: a new session means it gave the last one up
            # (its own "nothing said" timer, BACK, a lost connection), finished or not
            await current.close()
        now = time.monotonic()
        starts = self._starts.setdefault(watch_id, deque())
        while starts and now - starts[0] > 60:
            starts.popleft()
        if len(starts) >= COMMANDS_PER_MINUTE:
            raise SessionError("too_many_commands", 429)
        starts.append(now)
        session = CommandSession(
            hass=self._hass,
            satellite=satellite,
            session_id=session_id,
            decoder=self._decoder_factory(self._hass),
            text_only=text_only,
            on_closed=lambda s: self._sessions.pop(watch_id, None) if self._sessions.get(watch_id) is s else None,
            on_stt=lambda failed: self._count_stt(watch_id, failed),
        )
        self._sessions[watch_id] = session
        await session.start()
        return session

    def _count_stt(self, watch_id: str, failed: bool) -> int:
        """STT failures in a row on this watch; a recognised command resets the count."""
        count = self._stt_failures.get(watch_id, 0) + 1 if failed else 0
        self._stt_failures[watch_id] = count
        return count

    async def async_close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
