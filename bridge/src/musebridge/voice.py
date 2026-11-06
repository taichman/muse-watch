"""A long-lived Muse voice call that push-to-talk turns plug into.

The call stays open between turns, so Muse treats a run of turns as one
conversation and a new turn skips call setup. While no one is talking, the
microphone feeds silence, which is what the SDK's provider endpointing expects.
The call ends when it goes idle (``idle_timeout``) and the next turn opens a
fresh one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from musegadget import __version__ as sdk_version
from musegadget import identity
from musegadget.link_client import DeviceDescription, LinkSession
from musegadget.service import DEFAULT_NOISE_HOST
from musegadget.voice_call import FRAME_BYTES, FRAME_SECONDS, VoiceCall, pcm_rms

from musebridge import credentials

log = logging.getLogger(__name__)

SILENCE = b"\0" * FRAME_BYTES
AUDIBLE_RMS = 50
MIC_QUEUE_FRAMES = 75  # 3 s: absorbs network jitter from the watch
JITTER_WAIT_S = 0.25  # while talking, wait this long for late audio before sending silence


@dataclass
class TurnLimits:
    # Reply is over after this much quiet with no turn in progress. Long, because Muse
    # often says a short lead-in ("let me check"), settles, looks something up, and
    # then answers as a separate, delayed response.
    reply_quiet_s: float = 10.0
    first_audio_s: float = 60.0  # give up if Muse says nothing for this long (tools can be slow)
    max_reply_s: float = 180.0


class Microphone:
    """Watch audio in, 40 ms PCM16 frames out; silence whenever no one is talking."""

    def __init__(self) -> None:
        self.talking = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(MIC_QUEUE_FRAMES)
        self._partial = bytearray()

    def push(self, pcm: bytes) -> None:
        self._partial += pcm
        while len(self._partial) >= FRAME_BYTES:
            self._put(bytes(self._partial[:FRAME_BYTES]))
            del self._partial[:FRAME_BYTES]

    def end_turn(self) -> None:
        tail = len(self._partial) - len(self._partial) % 2
        if tail:
            self._put(bytes(self._partial[:tail]))
        self._partial.clear()
        self.talking = False

    def _put(self, frame: bytes) -> None:
        if self._queue.full():
            self._queue.get_nowait()  # keep the newest audio
        self._queue.put_nowait(frame)

    async def frames(self):
        while True:
            try:
                yield await asyncio.wait_for(
                    self._queue.get(), JITTER_WAIT_S if self.talking else FRAME_SECONDS,
                )
            except asyncio.TimeoutError:
                yield SILENCE


class Speaker:
    """Muse's reply audio, routed to whichever turn is listening."""

    def __init__(self) -> None:
        self.turn: asyncio.Queue[bytes] | None = None
        self.last_audible = 0.0

    def write(self, pcm: bytes) -> None:
        if pcm_rms(pcm) >= AUDIBLE_RMS:
            self.last_audible = time.monotonic()
        if self.turn is not None:
            self.turn.put_nowait(pcm)

    def flush(self) -> None:
        if self.turn is not None:
            while not self.turn.empty():
                self.turn.get_nowait()


class _Call(VoiceCall):
    """VoiceCall that exposes whether Muse is mid-response and still playing,
    and logs the events that decide whether Muse hears the watch at all."""

    _held = False

    @property
    def held(self) -> bool:
        return self._held

    @held.setter
    def held(self, value: bool) -> None:
        # While held, the SDK drops microphone audio: Muse won't hear the watch.
        if value != self._held:
            log.info("Muse %s the microphone", "held" if value else "released")
        self._held = value

    def _track_turn(self, event: dict) -> bool:
        started = event["type"] == "hatch.voice.turn_started"
        log.info("Muse %s a response", "started" if started else "settled")
        return super()._track_turn(event)

    @property
    def mic_state(self) -> str:
        return f"held={self.held}, unplayed audible frames={self._audible_frames}"

    @property
    def busy(self) -> bool:
        """Muse has started a response it hasn't settled yet."""
        return bool(self._active_turns)

    @property
    def playback_idle(self) -> bool:
        return not self._playback and time.monotonic() >= self._playing_until


class MuseVoice:
    def __init__(self, *, vm_id: str | None = None, thread_id: str | None = None,
                 idle_timeout: float = 300, limits: TurnLimits | None = None) -> None:
        self.vm_id, self.thread_id = vm_id, thread_id
        self.idle_timeout = idle_timeout
        self.limits = limits or TurnLimits()
        self.mic = Microphone()
        self.speaker = Speaker()
        self._call: _Call | None = None
        self._task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._current_turn: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _ensure_call(self) -> _Call:
        async with self._start_lock:
            if self.active and self._call is not None and self._call.ready.is_set():
                return self._call
            await self.hang_up()
            pairing, vm = await credentials.find_vm(self.vm_id)
            ident = identity.load_or_create()
            self.mic = Microphone()
            self.speaker = Speaker()
            session = LinkSession(
                noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
                vm_id=vm["vm_id"], vm_auth_token=vm["vm_auth_token"], app_id="hatch-web",
                register_device=False,
                device=DeviceDescription(ident.node_id, "Apple Watch", sdk_version, {}),
                run_command=lambda *unused: {"ok": False, "error": "unsupported command"},
            )
            call = _Call(
                session, device_id=ident.node_id, thread_id=self.thread_id,
                idle_timeout=self.idle_timeout, overall_timeout=4 * 3600,
            )
            self._call = call
            self._task = asyncio.create_task(self._run_call(session, call))
            ready = asyncio.create_task(call.ready.wait())
            done, _ = await asyncio.wait({ready, self._task}, timeout=45,
                                         return_when=asyncio.FIRST_COMPLETED)
            if ready not in done:
                ready.cancel()
                if self._task in done:
                    self._task.result()  # raises the call's failure
                await self.hang_up()
                raise TimeoutError("Muse voice call didn't become ready")
            log.info("voice call ready (thread %s)", call.thread_id)
            return call

    async def _run_call(self, session: LinkSession, call: _Call) -> None:
        stop = asyncio.Event()
        connection = asyncio.create_task(session.run(stop))
        try:
            await session.wait_transport_ready(35)
            reason = await call.run(self.mic.frames(), self.speaker, realtime_input=True)
            log.info("voice call ended: %s", reason)
        finally:
            stop.set()
            try:
                await asyncio.wait_for(connection, 3)
            except (Exception, asyncio.CancelledError):
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)

    async def hang_up(self) -> None:
        task, self._task, self._call = self._task, None, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def turn(self, audio_in, write_reply) -> None:
        """One push-to-talk turn.

        ``audio_in`` yields the watch's PCM16LE 24 kHz mono audio while the
        button is held and ends on release. ``write_reply`` is awaited with
        Muse's reply audio, in real time, until the reply is over.

        A new turn cancels the one in flight: the watch has moved on, even if
        its hang-up hasn't reached us yet (through the iPhone that can take a
        minute).
        """
        me = asyncio.current_task()
        previous, self._current_turn = self._current_turn, me
        if previous is not None and not previous.done():
            log.info("a new turn replaces the one in flight")
            previous.cancel()
        try:
            await self._take_turn(audio_in, write_reply)
        finally:
            if self._current_turn is me:
                self._current_turn = None

    async def _take_turn(self, audio_in, write_reply) -> None:
        async with self._turn_lock:
            call = await self._ensure_call()
            await call.barge_in()  # cut off whatever Muse was still saying
            reply: asyncio.Queue[bytes] = asyncio.Queue()
            self.speaker.flush()
            self.speaker.turn = reply
            self.mic.talking = True
            try:
                received, loudest = 0, 0.0
                async for chunk in audio_in:
                    self.mic.push(chunk)
                    received += len(chunk)
                    loudest = max(loudest, pcm_rms(chunk[: len(chunk) // 2 * 2]))
                self.mic.end_turn()
                log.info("heard %.1f s from the watch (loudest RMS %.0f; %s)",
                         received / 48_000, loudest, call.mic_state)
                await self._stream_reply(call, reply, write_reply)
            finally:
                self.mic.talking = False
                self.speaker.turn = None

    async def _stream_reply(self, call: _Call, reply: asyncio.Queue, write_reply) -> None:
        released = time.monotonic()
        heard = False
        while True:
            try:
                pcm = await asyncio.wait_for(reply.get(), 0.1)
            except asyncio.TimeoutError:
                pcm = None
            if pcm is not None:
                await write_reply(pcm)
                heard = heard or self.speaker.last_audible > released
                continue
            if not self.active:
                return  # the call ended under us
            now = time.monotonic()
            if heard and call.playback_idle and not call.busy:
                if now - self.speaker.last_audible >= self.limits.reply_quiet_s:
                    return
            if not heard and now - released > self.limits.first_audio_s:
                log.info("no reply from Muse within %.0f s (%s); starting a fresh call next turn",
                         self.limits.first_audio_s, call.mic_state)
                await self.hang_up()
                return
            if now - released > self.limits.max_reply_s:
                return
