"""Make the Linux-only musegadget SDK behave on macOS.

Call :func:`configure` before importing anything else from ``musegadget``:

- State lives in ``~/Library/Application Support/MuseBridge`` instead of
  ``/var/lib/musegadget`` (the SDK reads these paths from the environment).
  Each extra person gets a profile under ``profiles/<name>``: their own gadget
  identity, Muse pairing and watch secret.
- ``musegadget.ble_server`` (BlueZ over D-Bus) is replaced by the CoreBluetooth
  peripheral in :mod:`musebridge.mac_ble`. ``musegadget.pair`` imports it
  lazily, so registering the module here is enough.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

STATE_DIR = Path.home() / "Library" / "Application Support" / "MuseBridge"


PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def state_dir(profile: str | None = None) -> Path:
    if profile is None:
        return STATE_DIR
    if not PROFILE_NAME.fullmatch(profile):
        raise ValueError("a profile name is lowercase letters, digits, - and _")
    return STATE_DIR / "profiles" / profile


def configure(profile: str | None = None) -> Path:
    from musegadget import config

    state = Path(os.environ.setdefault(config.STATE_DIR_ENV, str(state_dir(profile))))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ.setdefault(config.SOCKET_ENV, str(state / "musegadget.sock"))
    os.environ.setdefault(config.COMMANDS_DIR_ENV, str(state / "commands.d"))

    if sys.platform == "darwin":
        from musebridge import mac_ble

        sys.modules["musegadget.ble_server"] = mac_ble
    return state
