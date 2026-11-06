"""HTTP API the watch app talks to.

Plain HTTP rather than WebSockets: watchOS only allows WebSockets for apps in
narrow cases, but ordinary URLSession requests always work, over Wi-Fi, LTE or
the paired iPhone.

    GET  /health       no auth; liveness
    GET  /v1/status    {"paired": bool, "call_active": bool}
    POST /v1/talk      body: PCM16LE mono 24 kHz, streamed while the button is held
                       response: Muse's reply in the same format, streamed in real time
    POST /v1/hangup    ends the open voice call

Every /v1 request needs ``Authorization: Bearer <secret>``; ``musebridge
secret`` prints it.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from aiohttp import web
from musegadget import config

from musebridge import credentials
from musebridge.voice import MuseVoice

log = logging.getLogger(__name__)

SECRET_FILE = "bridge_secret"
AUDIO_TYPE = "audio/L16; rate=24000; channels=1"
MAX_TURN_BYTES = 24000 * 2 * 120  # two minutes of audio


def load_secret() -> str:
    path = config.state_dir() / SECRET_FILE
    try:
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = secrets.token_urlsafe(32)
    path.touch(mode=0o600)
    path.write_text(secret + "\n", encoding="utf-8")
    return secret


def build_app(voice: MuseVoice, secret: str) -> web.Application:
    expected = f"Bearer {secret}".encode()

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path.startswith("/v1/"):
            presented = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(presented, expected):
                raise web.HTTPUnauthorized()
        return await handler(request)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(request: web.Request) -> web.Response:
        return web.json_response({
            "paired": bool(config.load_json(config.PAIRING_FILE)),
            "call_active": voice.active,
        })

    async def talk(request: web.Request) -> web.StreamResponse:
        async def audio_in():
            received = 0
            async for chunk in request.content.iter_any():
                received += len(chunk)
                if received > MAX_TURN_BYTES:
                    break
                yield chunk

        response = web.StreamResponse(headers={"Content-Type": AUDIO_TYPE, "Cache-Control": "no-store"})

        async def write_reply(pcm: bytes) -> None:
            if not response.prepared:
                await response.prepare(request)
            await response.write(pcm)

        try:
            await voice.turn(audio_in(), write_reply)
        except credentials.NotPaired as exc:
            raise web.HTTPConflict(text=str(exc))
        except (ConnectionError, TimeoutError, ValueError) as exc:
            if _watch_gone(request):
                log.info("the watch hung up")
                return response if response.prepared else web.Response(status=499)
            # Keep diagnostics to the failure class; SDK errors can echo credentials.
            log.warning("turn failed: %s", type(exc).__name__)
            if response.prepared:
                return response
            raise web.HTTPBadGateway(text=f"Muse unavailable ({type(exc).__name__})")
        if not response.prepared:
            return web.Response(status=204)  # Muse didn't answer
        try:
            await response.write_eof()
        except ConnectionResetError:
            log.info("the watch hung up")
        return response

    def _watch_gone(request: web.Request) -> bool:
        transport = request.transport
        return transport is None or transport.is_closing()

    async def hangup(request: web.Request) -> web.Response:
        await voice.hang_up()
        return web.json_response({"ok": True})

    async def on_cleanup(app: web.Application) -> None:
        await voice.hang_up()

    app = web.Application(middlewares=[auth], client_max_size=MAX_TURN_BYTES)
    app.add_routes([
        web.get("/health", health),
        web.get("/v1/status", status),
        web.post("/v1/talk", talk),
        web.post("/v1/hangup", hangup),
    ])
    app.on_cleanup.append(on_cleanup)
    return app
