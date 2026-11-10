"""One voice command of a watch: chunks in, the command, the reply and its audio out.

The watch posts Ogg/Opus chunks while the user speaks; they go through ffmpeg into the watch's
Assist satellite pipeline. Home Assistant's VAD ends the command; the chunk request that sees it
waits for speech-to-text and returns the command ("heard"). The reply text and the audio follow
on separate requests; the audio in pieces while it is being made (protocol 6), or whole.
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
from .speech import SpeechError, SpeechStream

if TYPE_CHECKING:
    from .assist_satellite import WristTalkSatellite

_LOGGER = logging.getLogger(__name__)

MAX_CHUNK_BYTES = 16 * 1024
# The whole recording of a command, with the chunks still waiting for a missing one: 20 s of the
# watch's microphone is about 68 kB (3.4 kB/s, measured), so this only stops a broken or hostile
# client from filling the memory within the command's 20 s
MAX_RECORDING_BYTES = 256 * 1024
# Chunks waiting for a missing one: the watch sends two at a time (security review 2026-11-07)
MAX_PENDING_CHUNKS = 6
MAX_COMMAND_SECONDS = 20.0
COMMANDS_PER_MINUTE = 30
# The last commands of a watch that have ended: a chunk of one of them arriving late (through the
# phone a chunk can take 3.5 s) must not end the current command and start the old one again
ENDED_REMEMBERED = 8
# A request held longer than about 5 s does not come back through the phone (held 10, 15 or 25 s
# it failed after about 30 s, measured on a fēnix 8 through an iPhone, 2026-11-04): every wait
# answers within 4 s, and the watch asks again
HEARD_WAIT_SECONDS = 4.0
# The pipeline's VAD sees a chunk a moment after it is written: a short grace lets the very chunk
# on which the command ended come back with the command, instead of the next one a second later
END_GRACE_SECONDS = 0.2
LONG_POLL_SECONDS = 4.0
STT_FAILED_ERROR = "stt-stream-failed"
NOTHING_ERRORS = ("stt-no-text-recognized",)
# STT failing is "nothing heard" twice in a row on a watch - a short noise before speech ends the
# command in the VAD; from the third time an error the watch tells by its status. Not retried: a
# retry gets the same recording and the VAD the same short sound - it rescued none of 20 commands
# (measured 2026-11-04/07).
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
    finished: bool = False
    # The reply's voice: ffmpeg started when the agent starts (`_prepared`), speaking once the
    # text is known (`voice`); `_voice_ready` once it is known whether there is a voice
    voice: SpeechStream | None = None
    _prepared: SpeechStream | None = None
    _prepare_task: asyncio.Task[SpeechStream | None] | None = None
    _expected_offset: int = 0
    _pending: dict[int, bytes] = field(default_factory=dict)
    _input_closed: bool = False
    # Where the recording ends, once "final" came: the input closes when everything before it is in
    _final_end: int | None = None
    # Chunk requests waiting for the command's text: with protocol 5 only one waits, the others
    # answer "recognizing" at once
    _heard_waiters: int = 0
    _queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    _heard_event: asyncio.Event = field(default_factory=asyncio.Event)
    _reply_event: asyncio.Event = field(default_factory=asyncio.Event)
    _voice_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _ended_event: asyncio.Event = field(default_factory=asyncio.Event)
    _started: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    _cap_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        try:
            await self.decoder.start()
            self._spawn(self._read_pcm())
            self._spawn(self._run(self._live_stream()))
            # End of command without silence (radio, wind): close the input after the cap
            self._cap_handle = self.hass.loop.call_later(
                MAX_COMMAND_SECONDS, lambda: self._spawn(self.close_input())
            )
        finally:
            self._started.set()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = self.hass.async_create_background_task(
            coro, f"wristtalk {self.session_id}"
        )
        self._tasks.append(task)
        return task

    async def _read_pcm(self) -> None:
        async for chunk in self.decoder.pcm():
            await self._queue.put(chunk)
        await self._queue.put(None)

    async def _live_stream(self) -> AsyncIterator[bytes]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk

    async def _run(self, stream: AsyncIterator[bytes]) -> None:
        try:
            await self.satellite.async_run_command(self, stream)
        except Exception:
            _LOGGER.exception("WristTalk command %s failed", self.session_id)
            self._fail("pipeline_failed")
        self._finish()

    # --- audio in ---------------------------------------------------------------------------

    async def feed(self, offset: int, data: bytes, *, final: bool) -> None:
        """A chunk at its byte offset; chunks may come out of order, duplicates are ignored."""
        if len(data) > MAX_CHUNK_BYTES:
            raise SessionError("chunk_too_large", 413)
        # Two chunks travel at once: a later one can arrive first and start the command, and the
        # first one then finds ffmpeg still starting (twice in 50 commands an AssertionError and
        # 500, 2026-11-06/07)
        await self._started.wait()
        if final:
            self._final_end = max(self._final_end or 0, offset + len(data))
        if not self._input_closed and offset >= self._expected_offset:
            waiting = sum(
                len(chunk) for o, chunk in self._pending.items() if o != offset
            )
            if offset + len(data) > MAX_RECORDING_BYTES or (
                self._expected_offset + waiting + len(data) > MAX_RECORDING_BYTES
            ):
                await self.close_input()
                raise SessionError("recording_too_large", 413)
            if offset not in self._pending and len(self._pending) >= MAX_PENDING_CHUNKS:
                await self.close_input()
                raise SessionError("too_many_chunks", 413)
            self._pending[offset] = data
            while self._expected_offset in self._pending:
                chunk = self._pending.pop(self._expected_offset)
                await self.decoder.feed(chunk)
                self._expected_offset += len(chunk)
        # "final" may come before an earlier chunk, or before a lost one sent again (protocol 4):
        # the input closes only once the whole recording up to its end is in; the command's cap
        # closes it if a chunk never comes
        if self._final_end is not None and self._expected_offset >= self._final_end:
            await self.close_input()

    async def close_input(self) -> None:
        if self._input_closed:
            return
        self._input_closed = True
        self._ended_event.set()
        if self._cap_handle is not None:
            self._cap_handle.cancel()
        await self.decoder.close_input()

    async def chunk_reply(self, *, early_signal: bool = False) -> dict[str, Any]:
        """The answer to a chunk: listening, or - once the command has ended - the command.

        `early_signal` (protocol 5): while one request already waits for the text, another one
        answers "recognizing" at once - the watch stops listening a moment sooner, ended by the
        VAD; the one waiting still brings the text. Older apps do not know the state: all wait.
        """
        loop = asyncio.get_running_loop()
        # After "final" a missing chunk is on its way: wait for it within the one 4 s wait, not
        # just the grace - an answer at once would have the watch repeat "final" for nothing
        grace = HEARD_WAIT_SECONDS if self._final_end is not None else END_GRACE_SECONDS
        deadline = (
            loop.time()
            + grace
            + (0 if self._final_end is not None else HEARD_WAIT_SECONDS)
        )
        if not self._heard_event.is_set() and not self._input_closed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(grace):
                    await self._ended_event.wait()
        if not self._heard_event.is_set() and not self._input_closed:
            return {"state": "listening", "speech": self.speech}
        if early_signal and self._heard_waiters and not self._heard_event.is_set():
            return {"state": "recognizing"}
        self._heard_waiters += 1
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0.0, deadline - loop.time())):
                    await self._heard_event.wait()
        finally:
            self._heard_waiters -= 1
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
        elif event.type is PipelineEventType.INTENT_START:
            if not self.text_only and self._prepare_task is None:
                # ffmpeg starts while the agent thinks: its start (about 0.45 s on a Home Assistant
                # Green) is not added to the voice's wait
                self._prepare_task = self._spawn(self._prepare_voice())
        elif event.type is PipelineEventType.INTENT_END:
            speech = data.get("intent_output", {}).get("response", {}).get("speech", {})
            self.reply = str(speech.get("plain", {}).get("speech") or "")
            self._reply_event.set()
            if self.text_only:
                self._voice_ready.set()
            else:
                # Synthesis starts the moment the text is known; the audio requests only collect it
                self._spawn(self._speak(self.reply))
        elif event.type is PipelineEventType.ERROR:
            code = str(data.get("code") or "error")
            if code == STT_FAILED_ERROR:
                # Measured with a short noise before speech (the VAD ends the command on it) and
                # with silence only - "say it again" helps, until it keeps failing
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

    async def _prepare_voice(self) -> SpeechStream | None:
        try:
            self._prepared = await self.satellite.async_prepare_speech(self.session_id)
        except Exception:
            _LOGGER.exception(
                "WristTalk %s: the voice could not start", self.session_id
            )
            self._prepared = None
        if self._prepared is not None and self.finished and self.reply is None:
            # The command ended without a reply while ffmpeg was starting
            await self._prepared.close()
        return self._prepared

    async def _speak(self, text: str) -> None:
        if self._prepare_task is None:
            self._prepare_task = self._spawn(self._prepare_voice())
        speech = await self._prepare_task
        if speech is not None:
            if text:
                speech.speak(text)
                self.voice = speech
            else:
                await speech.close()
        self._voice_ready.set()

    def _fail(self, code: str) -> None:
        self.error = code
        self._finish()

    def _finish(self) -> None:
        self.finished = True
        for event in (self._heard_event, self._reply_event, self._ended_event):
            event.set()
        if self.reply is None or self.text_only:
            self._voice_ready.set()
            if self._prepared is not None:
                # No reply to speak: ffmpeg, started while the agent thought, is not needed
                self._spawn(self._prepared.close())

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

    async def _voice(self, deadline: float) -> SpeechStream:
        """The reply's voice once it is known there is one, within the wait."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(
                max(0.0, deadline - asyncio.get_running_loop().time())
            ):
                await self._voice_ready.wait()
        if not self._voice_ready.is_set():
            raise SessionError("audio_waiting", 202)
        if self.voice is None:
            if self.error is not None:
                raise SessionError(self.error, self.error_status)
            raise SessionError("no_audio", 404)
        return self.voice

    async def audio(self) -> bytes:
        """The whole reply audio (Ogg/Opus) once it is made; 202 while it is (apps on 2-5)."""
        deadline = asyncio.get_running_loop().time() + LONG_POLL_SECONDS
        voice = await self._voice(deadline)
        try:
            data = await voice.whole(deadline)
        except SpeechError as err:
            raise SessionError(err.code, err.status) from err
        self.satellite.tts_response_finished()
        return data

    async def audio_piece(self, offset: int) -> bytes:
        """The reply audio from `offset` while it is being made (protocol 6): whole Ogg pages.

        The first answer waits for a few seconds of speech or the end, later ones for anything
        new - up to 4 s, then 202. The end: the last page's end-of-stream flag; then 410.
        """
        deadline = asyncio.get_running_loop().time() + LONG_POLL_SECONDS
        voice = await self._voice(deadline)
        try:
            data = await voice.piece(offset, deadline)
        except SpeechError as err:
            raise SessionError(err.code, err.status) from err
        if voice.ended and offset + len(data) >= voice.size:
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
        if self._prepared is not None:
            await self._prepared.close()
        if self.on_closed is not None:
            self.on_closed(self)


class SessionManager:
    """The current command of each watch, and its rate limit."""

    def __init__(
        self, hass: HomeAssistant, decoder_factory: Callable[[HomeAssistant], Decoder]
    ) -> None:
        self._hass = hass
        self._decoder_factory = decoder_factory
        self._sessions: dict[str, CommandSession] = {}
        self._starts: dict[str, deque[float]] = {}
        self._stt_failures: dict[str, int] = {}
        self._ended: dict[str, deque[str]] = {}

    def get(self, watch_id: str, session_id: str) -> CommandSession:
        session = self._sessions.get(watch_id)
        if session is None or session.session_id != session_id:
            raise SessionError("unknown_session", 404)
        return session

    async def get_or_start(
        self,
        watch_id: str,
        session_id: str,
        satellite: WristTalkSatellite,
        *,
        text_only: bool,
    ) -> CommandSession:
        current = self._sessions.get(watch_id)
        if current is not None and current.session_id == session_id:
            return current
        ended = self._ended.setdefault(watch_id, deque(maxlen=ENDED_REMEMBERED))
        if session_id in ended:
            # A late or repeated chunk of a command that has ended: never a second pipeline run
            raise SessionError("session_ended", 410)
        if current is not None:
            ended.append(current.session_id)
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
            on_closed=lambda s: (
                self._sessions.pop(watch_id, None)
                if self._sessions.get(watch_id) is s
                else None
            ),
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
