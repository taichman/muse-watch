import asyncio
import struct

import pytest
from aiohttp.test_utils import TestClient, TestServer

from musebridge import macos

macos.configure()

from musebridge.server import build_app  # noqa: E402
from musebridge.voice import FRAME_BYTES, SILENCE, Microphone, MuseVoice, TurnLimits  # noqa: E402

TONE = struct.pack("<960h", *([3000, -3000] * 480))


class FakeCall:
    """Stands in for the SDK call: replies to each turn with a little audio."""

    def __init__(self, voice: MuseVoice, reply_frames: int = 3) -> None:
        self.voice = voice
        self.reply_frames = reply_frames
        self.barge_ins = 0
        self.playback_idle = True
        self.busy = False
        self.mic_state = "fake"

    async def barge_in(self) -> None:
        self.barge_ins += 1


def fake_voice(reply_frames: int = 3) -> tuple[MuseVoice, FakeCall, list[bytes]]:
    voice = MuseVoice(limits=TurnLimits(reply_quiet_s=0.3, first_audio_s=1.0))
    call = FakeCall(voice, reply_frames)
    heard: list[bytes] = []

    async def ensure_call():
        voice._task = asyncio.get_running_loop().create_future()  # "active" while pending
        return call

    async def muse():
        # Drain the mic like VoiceCall would, then answer once the turn ends.
        frames = voice.mic.frames()
        answered = None
        while True:
            frame = await frames.__anext__()
            turn = voice.speaker.turn
            if frame != SILENCE:
                heard.append(frame)
            elif heard and not voice.mic.talking and turn is not None and turn is not answered:
                answered = turn
                for _ in range(call.reply_frames):
                    voice.speaker.write(TONE)
                    await asyncio.sleep(0.04)
                voice.speaker.write(SILENCE)

    voice._ensure_call = ensure_call
    voice._muse = muse
    return voice, call, heard


async def test_microphone_rechunks_and_flushes_tail():
    mic = Microphone()
    mic.talking = True
    mic.push(b"\1" * (FRAME_BYTES + 100))
    mic.end_turn()
    frames = mic.frames()
    assert await frames.__anext__() == b"\1" * FRAME_BYTES
    assert await frames.__anext__() == b"\1" * 100
    assert await frames.__anext__() == SILENCE


async def test_turn_streams_mic_up_and_reply_down():
    voice, call, heard = fake_voice(reply_frames=3)
    muse = asyncio.create_task(voice._muse())

    async def audio_in():
        for _ in range(5):
            yield TONE
            await asyncio.sleep(0.01)

    reply: list[bytes] = []

    async def write_reply(pcm):
        reply.append(pcm)

    await asyncio.wait_for(voice.turn(audio_in(), write_reply), 5)
    muse.cancel()
    assert call.barge_ins == 1
    assert heard.count(TONE) == 5
    assert reply.count(TONE) == 3
    assert voice.speaker.turn is None and not voice.mic.talking


async def test_new_turn_replaces_a_stuck_one():
    voice, call, heard = fake_voice(reply_frames=3)
    muse = asyncio.create_task(voice._muse())

    async def stuck_audio():  # a watch that went away without the hang-up arriving
        yield TONE
        await asyncio.Event().wait()

    async def audio_in():
        for _ in range(3):
            yield TONE

    reply = []

    async def write_reply(pcm):
        reply.append(pcm)

    stuck = asyncio.create_task(voice.turn(stuck_audio(), write_reply))
    await asyncio.sleep(0.1)
    await asyncio.wait_for(voice.turn(audio_in(), write_reply), 5)
    muse.cancel()
    assert stuck.cancelled()
    assert reply.count(TONE) == 3


async def test_turn_gives_up_when_muse_is_silent():
    voice, call, heard = fake_voice(reply_frames=0)
    muse = asyncio.create_task(voice._muse())

    async def audio_in():
        yield TONE

    reply = []

    async def write_reply(pcm):
        reply.append(pcm)

    await asyncio.wait_for(voice.turn(audio_in(), write_reply), 5)
    muse.cancel()
    assert all(pcm == SILENCE for pcm in reply)


@pytest.fixture
async def client():
    voice, call, heard = fake_voice(reply_frames=2)
    muse = asyncio.create_task(voice._muse())
    async with TestClient(TestServer(build_app(voice, "s3cret"))) as client:
        yield client
    muse.cancel()


async def test_requires_secret(client):
    assert (await client.get("/health")).status == 200
    assert (await client.get("/v1/status")).status == 401
    assert (await client.get("/v1/status", headers={"Authorization": "Bearer nope"})).status == 401


async def test_talk_round_trip(client):
    auth = {"Authorization": "Bearer s3cret"}

    async def body():
        for _ in range(3):
            yield TONE
            await asyncio.sleep(0.01)

    resp = await client.post("/v1/talk", data=body(), headers=auth)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("audio/L16")
    audio = await resp.read()
    assert audio.count(TONE) == 2
