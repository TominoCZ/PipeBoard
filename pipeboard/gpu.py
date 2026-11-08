"""The GPU's power level, any vendor, without root, and whether it is what makes the sound drop out.

Measured on the owner's RTX 3080 Ti (2026-11-08, 1 ms blocks at 192 kHz): while a video played and
nothing else moved on screen, the GPU sat in a power-saving level and PipeWire's real-time threads
were woken up to 3.6 ms late (~135 late cycles per 5 s). With its memory clock locked at its stock
maximum: none. Locked at its lowest: as many as before, so it is the power-saving level itself,
not the switching between levels. Any animation (PipeBoard's own window, a panel popping up) lifts
the GPU out of it, which is why the window can't be used to test it: the server watches in the
background, whatever the window does.

- NVIDIA: one long-running `nvidia-smi` query (a reading a second; a spawn per second costs more).
- AMD (amdgpu): sysfs `pp_dpm_mclk`, the memory clock's levels, the current one marked `*`.
- Intel (i915): sysfs `gt_cur_freq_mhz` against `gt_RP0_freq_mhz` (it shares system memory: its
  graphics clock is what it saves power on).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)
DRM = Path("/sys/class/drm")
VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
LOW = 0.9  # under this share of its top clock, a GPU is in a power-saving level
NVIDIA_QUERY = "index,name,clocks.mem,clocks.max.mem"
# GPU at full speed (the fix): a script that puts every GPU at its stock top clocks, run as root
# now and by a service at boot and after sleep (a lock does not survive either). Tests: stand-ins.
NVIDIA_SMI, SYSTEMCTL = "nvidia-smi", "systemctl"
SCRIPT = Path("/usr/local/sbin/pipeboard-gpu-full-speed")
UNIT = Path("/etc/systemd/system/pipeboard-gpu.service")
SLEEP = "suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target"


def full_script() -> str:
    """Every GPU at its stock top clocks, never above (no overclock): NVIDIA its memory clock
    locked at its own maximum (measured: the memory clock is what mattered), AMD the driver's
    "high" level, Intel its lowest clock raised to its top one."""
    return f"""#!/bin/sh
# Written by PipeBoard (Audio engine > GPU at full speed): every GPU at its stock top clocks, so its
# power saving never makes the sound drop out. Run at boot and after sleep ({UNIT.name}).
if command -v {NVIDIA_SMI} >/dev/null 2>&1; then
  for i in $({NVIDIA_SMI} --query-gpu=index --format=csv,noheader 2>/dev/null); do
    m=$({NVIDIA_SMI} -i "$i" --query-gpu=clocks.max.mem --format=csv,noheader,nounits)
    {NVIDIA_SMI} -i "$i" --lock-memory-clocks="$m,$m" >/dev/null
  done
fi
for f in {DRM}/card[0-9]*/device/power_dpm_force_performance_level; do
  [ -e "$f" ] && echo high > "$f"
done
for c in {DRM}/card[0-9]*; do
  [ -f "$c/gt_RP0_freq_mhz" ] && cat "$c/gt_RP0_freq_mhz" > "$c/gt_min_freq_mhz"
done
exit 0
"""


def unit() -> str:
    return f"""[Unit]
Description=PipeBoard: every GPU at its stock top clocks (its power saving made the sound drop out)
After=multi-user.target {SLEEP}

[Service]
Type=oneshot
ExecStart={SCRIPT}

[Install]
WantedBy=multi-user.target {SLEEP}
"""


def helper_lines() -> str:
    """The root helper's `gpu on|off` (engine.helper_script): on, the script and its service
    written and enabled, then run; off, both removed and every GPU back to its driver's choice."""
    return f"""  gpu)
    case "$rest" in
    on)
      mkdir -p {SCRIPT.parent}
      cat > {SCRIPT} <<'PBGPU'
{full_script()}PBGPU
      chmod 755 {SCRIPT}
      cat > {UNIT} <<'PBGPU'
{unit()}PBGPU
      {SYSTEMCTL} daemon-reload </dev/null >/dev/null 2>&1
      {SYSTEMCTL} enable {UNIT.name} </dev/null >/dev/null 2>&1
      {SCRIPT} </dev/null && echo ok || echo failed;;
    off)
      {SYSTEMCTL} disable {UNIT.name} </dev/null >/dev/null 2>&1
      rm -f {UNIT} {SCRIPT}
      {SYSTEMCTL} daemon-reload </dev/null >/dev/null 2>&1
      if command -v {NVIDIA_SMI} >/dev/null 2>&1; then
        {NVIDIA_SMI} --reset-memory-clocks </dev/null >/dev/null 2>&1
      fi
      for f in {DRM}/card[0-9]*/device/power_dpm_force_performance_level; do
        [ -e "$f" ] && echo auto > "$f"
      done
      for c in {DRM}/card[0-9]*; do
        [ -f "$c/gt_RPn_freq_mhz" ] && cat "$c/gt_RPn_freq_mhz" > "$c/gt_min_freq_mhz"
      done
      echo ok;;
    *) echo bad;;
    esac;;
"""


def full() -> bool:
    """Whether PipeBoard keeps the GPUs at full speed (its service is there)."""
    return UNIT.is_file()


def nvidia_line(line: str) -> dict | None:
    """One line of `nvidia-smi --query-gpu=index,name,clocks.mem,clocks.max.mem`, csv, no units."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 4 or not parts[0].isdigit():
        return None
    try:
        mem, top = int(parts[2]), int(parts[3])
    except ValueError:
        return None
    return {"vendor": "nvidia", "id": f"nvidia{parts[0]}", "name": parts[1], "clock": mem,
            "top": top, "what": "memory"}


def amd_levels(text: str) -> tuple[int, int] | None:
    """`pp_dpm_mclk` ("0: 96Mhz\\n1: 456Mhz *\\n…"): the current level's clock and the top one's."""
    levels = re.findall(r"^\s*\d+:\s*(\d+)\s*[Mm]hz\s*(\*?)", text, re.M)
    if not levels:
        return None
    now = next((int(c) for c, mark in levels if mark), None)
    return (now, max(int(c) for c, _ in levels)) if now is not None else None


def _sysfs() -> list[dict]:
    """AMD and Intel GPUs from sysfs (NVIDIA's clocks are not there: nvidia-smi)."""
    found = []
    for card in sorted(DRM.glob("card[0-9]*")):
        if "-" in card.name:  # (a connector: card0-DP-1)
            continue
        dev = card / "device"
        with contextlib.suppress(OSError):
            vendor = VENDORS.get((dev / "vendor").read_text().strip())
            if vendor == "amd" and (levels := amd_levels((dev / "pp_dpm_mclk").read_text())):
                found.append({"vendor": "amd", "id": card.name, "name": "AMD GPU",
                              "clock": levels[0], "top": levels[1], "what": "memory"})
            elif vendor == "intel" and (card / "gt_cur_freq_mhz").is_file():
                found.append({"vendor": "intel", "id": card.name, "name": "Intel GPU",
                              "clock": int((card / "gt_cur_freq_mhz").read_text()),
                              "top": int((card / "gt_RP0_freq_mhz").read_text()),
                              "what": "graphics"})
    return found


def low(gpu: dict) -> bool:
    return gpu["top"] > 0 and gpu["clock"] < LOW * gpu["top"]


class Watch:
    """The GPUs' clocks, read about once a second; `now()` the latest. Never raises."""

    def __init__(self):
        self.nvidia: dict[str, dict] = {}
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if shutil.which("nvidia-smi") and not self.task:
            self.task = asyncio.create_task(self._nvidia())

    async def _nvidia(self) -> None:
        """One nvidia-smi printing a line per GPU every second; started again if it ends."""
        while True:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    "nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits",
                    "-lms", "1000", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                async for raw in self.proc.stdout:
                    if gpu := nvidia_line(raw.decode(errors="replace")):
                        self.nvidia[gpu["id"]] = gpu
                await self.proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - no GPU reading is no diagnosis, not a crash
                log.exception("gpu: could not read nvidia-smi")
            self.nvidia.clear()
            await asyncio.sleep(30)  # (gone or failing: not a busy loop)

    def now(self) -> list[dict]:
        return [*self.nvidia.values(), *_sysfs()]

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        with contextlib.suppress(ProcessLookupError, AttributeError):
            self.proc.kill()
            await self.proc.wait()
            self.proc._transport.close()


# The diagnosis: over the last minutes, one sample a second while the sound runs: the clocking
# card's new dropouts, and whether a GPU was in a power-saving level. The GPU is the likely cause
# when dropouts come (nearly) only while it saves power, with enough seconds of both to compare
# (the owner's: ~28 a second while saving, none while up). Not enough of either: nothing said.
KEEP_S, ENOUGH_S, ENOUGH_DROPS, RATIO = 600, 30, 20, 5


class Evidence:
    """The samples (dropouts this second, GPU saving power) and what they point to."""

    def __init__(self, keep: int = KEEP_S):
        self.samples: deque[tuple[int, bool]] = deque(maxlen=keep)

    def add(self, drops: int, saving: bool) -> None:
        self.samples.append((max(0, drops), saving))

    def clear(self) -> None:
        self.samples.clear()

    def verdict(self) -> dict | None:
        """{"saving": dropouts a second while saving, "up": while not, and the seconds of each}
        when the GPU's power saving is the likely cause; else None."""
        saving = [d for d, s in self.samples if s]
        up = [d for d, s in self.samples if not s]
        if len(saving) < ENOUGH_S or len(up) < ENOUGH_S or sum(saving) < ENOUGH_DROPS:
            return None
        rate_saving, rate_up = sum(saving) / len(saving), sum(up) / len(up)
        if rate_up * RATIO > rate_saving:
            return None
        return {"saving": round(rate_saving, 1), "up": round(rate_up, 2),
                "saving_s": len(saving), "up_s": len(up)}
