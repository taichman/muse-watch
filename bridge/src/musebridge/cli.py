"""musebridge: pair this Mac with Muse, then serve the watch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import wave
from pathlib import Path

from musebridge import macos

DEFAULT_PORT = 8787


def cmd_pair(args: argparse.Namespace) -> int:
    from musegadget import cli as sdk_cli
    from musegadget import config

    if args.sdk_token:
        path = config.state_dir() / config.SDK_TOKEN_FILE
        path.touch(mode=0o600)
        path.write_text(args.sdk_token.strip() + "\n", encoding="utf-8")
    try:
        config.sdk_token()
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print("If macOS asks, allow Bluetooth for your terminal app.")
    argv = ["pair", "--timeout", str(args.timeout)] + (["--force"] if args.force else [])
    return sdk_cli.main(argv)


def cmd_sdk(args: argparse.Namespace) -> int:
    from musegadget import cli as sdk_cli

    return sdk_cli.main([args.command])


def _addresses() -> list[str]:
    names = [f"{socket.gethostname().removesuffix('.local')}.local"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packets sent; picks the outbound interface
            names.append(probe.getsockname()[0])
    except OSError:
        pass
    return names


def cmd_secret(args: argparse.Namespace) -> int:
    from musebridge.server import load_secret

    print(f"Secret: {load_secret()}")
    for host in _addresses():
        print(f"URL:    http://{host}:{args.port}")
    return 0


LOG_DIR = Path.home() / "Library/Logs/MuseBridge"


def _log_to_file(profile: str | None) -> None:
    """Also keep an INFO-level log on disk, for troubleshooting after the fact."""
    from logging.handlers import RotatingFileHandler

    log_file = LOG_DIR / (f"serve-{profile}.log" if profile else "serve.log")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    print(f"Logging to {log_file}")


def cmd_serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from musebridge.server import build_app, load_secret
    from musebridge.voice import MuseVoice

    # WebSocket debug logs include the Authorization header; never enable them.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    _log_to_file(args.profile)
    voice = MuseVoice(vm_id=args.vm_id, thread_id=args.thread_id, idle_timeout=args.idle_timeout)
    app = build_app(voice, load_secret())
    for host in _addresses():
        print(f"Serving the watch at http://{host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from musebridge import launchd

    if args.action == "install":
        launchd.install(args.profile, args.port, LOG_DIR)
        print(f"Installed {launchd.plist_path(args.profile)}; the bridge now starts at login.")
    elif args.action == "uninstall":
        launchd.uninstall(args.profile)
        print("Removed; the bridge no longer starts at login.")
    print(f"Status: {launchd.status(args.profile)}")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    """Send a WAV to the running bridge as one turn and save Muse's reply."""
    import aiohttp

    from musebridge.server import load_secret

    with wave.open(args.wav, "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
            print("Input must be PCM16 mono 24 kHz, e.g. "
                  "`afconvert -f WAVE -d LEI16@24000 -c 1 in.m4a out.wav`.", file=sys.stderr)
            return 2
        pcm = source.readframes(source.getnframes())

    async def body():
        frame = 1920  # 40 ms, paced like a live microphone
        for i in range(0, len(pcm), frame):
            yield pcm[i:i + frame]
            await asyncio.sleep(0.04)

    async def run() -> int:
        headers = {"Authorization": f"Bearer {load_secret()}", "Content-Type": "application/octet-stream"}
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{args.url}/v1/talk", data=body(), headers=headers) as resp:
                if resp.status == 204:
                    print("Muse didn't answer.")
                    return 1
                if resp.status != 200:
                    print(f"Bridge answered HTTP {resp.status}: {await resp.text()}", file=sys.stderr)
                    return 1
                with wave.open(args.out, "wb") as out:
                    out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    async for chunk in resp.content.iter_any():
                        out.writeframes(chunk)
        print(f"Reply saved to {args.out}")
        return 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musebridge")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--profile", help="another person's pairing and secret (default: yours)")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="pair this Mac with the Muse app over Bluetooth")
    pair.add_argument("--sdk-token", help="SDK token from gadgets.muse.ai (saved for later runs)")
    pair.add_argument("--force", action="store_true", help="pair again even if already paired")
    pair.add_argument("--timeout", type=int, default=600, help="seconds to keep pairing open")
    pair.set_defaults(func=cmd_pair)

    for name, text in (("info", "show device identity and pairing"),
                       ("unpair", "forget the saved pairing"),
                       ("doctor", "check setup")):
        sub.add_parser(name, help=text).set_defaults(func=cmd_sdk)

    serve = sub.add_parser("serve", help="serve the watch app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}; required with --profile")
    serve.add_argument("--vm-id", help="Muse VM to use (default: the account's default)")
    serve.add_argument("--thread-id", help="Muse conversation to talk in (default: the main one)")
    serve.add_argument("--idle-timeout", type=float, default=300,
                       help="hang up the voice call after this many quiet seconds")
    serve.set_defaults(func=cmd_serve)

    service = sub.add_parser("service", help="run serve in the background at login (launchd)")
    service.add_argument("action", choices=["install", "uninstall", "status"])
    service.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}; required with --profile")
    service.set_defaults(func=cmd_service)

    secret = sub.add_parser("secret", help="print the secret and URL to enter in the watch app")
    secret.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}; required with --profile")
    secret.set_defaults(func=cmd_secret)

    say = sub.add_parser("say", help="test the bridge: send a WAV as one turn, save the reply")
    say.add_argument("wav", help="PCM16 mono 24 kHz WAV")
    say.add_argument("--out", default="reply.wav")
    say.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}")
    say.set_defaults(func=cmd_say)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if getattr(args, "port", 0) is None:
        if args.profile:
            parser.error("--profile needs its own --port, e.g. --port 8788")
        args.port = DEFAULT_PORT
    try:
        macos.configure(args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
