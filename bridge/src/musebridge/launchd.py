"""Run ``musebridge serve`` as a launchd agent: at login, restarted if it dies.

The agent wraps the bridge in ``caffeinate -is`` so the Mac doesn't idle-sleep
while it serves (on a laptop, only while on power, and never with the lid shut).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

AGENTS = Path.home() / "Library" / "LaunchAgents"


def label(profile: str | None) -> str:
    return f"com.musebridge.{profile or 'default'}"


def plist_path(profile: str | None) -> Path:
    return AGENTS / f"{label(profile)}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(profile: str | None, port: int, log_dir: Path) -> None:
    exe = Path(sys.argv[0]).resolve()
    args = [str(exe)] + (["--profile", profile] if profile else []) + ["serve", "--port", str(port)]
    name = f"launchd-{profile}" if profile else "launchd"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label(profile),
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", *args],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        # Startup output and crashes; the bridge's own log is serve*.log.
        "StandardOutPath": str(log_dir / f"{name}.log"),
        "StandardErrorPath": str(log_dir / f"{name}.log"),
    }
    AGENTS.mkdir(parents=True, exist_ok=True)
    path = plist_path(profile)
    uninstall(profile, quiet=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], check=True)


def uninstall(profile: str | None, *, quiet: bool = False) -> None:
    path = plist_path(profile)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{label(profile)}"],
                   capture_output=quiet, check=False)
    path.unlink(missing_ok=True)


def status(profile: str | None) -> str:
    result = subprocess.run(["launchctl", "print", f"{_domain()}/{label(profile)}"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return "not installed"
    fields = dict(line.strip().split(" = ", 1) for line in result.stdout.splitlines()
                  if " = " in line and line.startswith("\t") and not line.startswith("\t\t"))
    return f"{fields.get('state', '?')} (pid {fields.get('pid', '-')}, last exit {fields.get('last exit code', '-')})"
