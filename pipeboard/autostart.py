"""Launch at login: a systemd user unit that starts PipeBoard minimized to the tray (PLAN §8).

It runs `pipeboard --background` once the graphical session and PipeWire are up: no window, the
tray icon, and the launch profile applied. Restarted if it ever fails.

KillMode=mixed: at logout systemd's stop reaches PipeBoard alone, which stops its effects itself.
The default sent SIGTERM to the effects too, at the same moment, so they ended before PipeBoard
knew it was quitting and each was logged "stopped unexpectedly" (owner, 2026-11-14).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

UNIT = "pipeboard.service"
ROOT = Path(__file__).resolve().parent.parent


def path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT


def text() -> str:
    return f"""[Unit]
Description=PipeBoard: routes your PipeWire audio (in the tray)
After=graphical-session.target pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
KillMode=mixed
WorkingDirectory={ROOT}
ExecStart={sys.executable} -m pipeboard --background
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


async def _systemctl(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out + err).decode().strip()


async def enabled() -> bool:
    code, out = await _systemctl("is-enabled", UNIT)
    return code == 0 and out == "enabled"


async def refresh() -> None:
    """A unit written before KillMode=mixed gets it (launch at login on). Only that line: the
    ExecStart is the one the owner turned it on with, not this run's Python (checks, fixtures)."""
    unit = path()
    try:
        old = unit.read_text()
        if "KillMode=" not in old:
            unit.write_text(old.replace("[Service]\n", "[Service]\nKillMode=mixed\n", 1))
            await _systemctl("daemon-reload")
    except OSError:
        pass


async def set_enabled(on: bool) -> str | None:
    """Turn launch at login on or off; return a complaint, or None."""
    try:
        if on:
            unit = path()
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(text())
            await _systemctl("daemon-reload")
        code, out = await _systemctl("enable" if on else "disable", UNIT)
        return f"systemctl: {out}" if code else None
    except OSError as e:
        return f"Could not set up launch at login: {e}"
