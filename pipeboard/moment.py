"""What the machine was doing in a second the interface dropped out (owner: is it the kernel, the
GPU, the CPU?). Sampled every second at no cost to the sound: /proc/stat (how busy the CPU was, and
its busiest core) and /proc/interrupts (the GPU's and the USB controllers' interrupts a second,
against the last minute's usual), plus the kernel's and PipeWire's own log lines from that moment,
through one `journalctl -f`. Nothing polls the GPU driver: an NVML query (nvidia-smi) can itself
stall the audio. Nothing here may raise into the board."""

from __future__ import annotations

import asyncio
import contextlib
import re
import statistics
import time
from collections import deque
from pathlib import Path

from pipeboard.scope import _die_with_parent

IRQS = {"nvidia": "gpu", "amdgpu": "gpu", "i915": "gpu", "xe": "gpu", "xhci_hcd": "usb"}
JOURNAL = ("journalctl", "-f", "-n", "0", "-o", "short-unix",
           "_TRANSPORT=kernel", "+", "_COMM=pipewire", "+", "_COMM=wireplumber")
NOISE = ("[UFW ",)  # kernel lines that say nothing about audio
WINDOW_S = 2.0  # log lines this close before a dropout's second are shown with it
# PipeWire's resync of a device that follows the graph's clock: silence written or sound skipped
# to get back on target, a click (alsa-pcm.c, alsa_write_sync). N suppressed: N more before it.
RESYNC = re.compile(r"spa\.alsa: hw:(\d+),\d+[pc]: follower .*resync \((\d+) suppressed\)")


def cpu_times(stat: str) -> dict[str, tuple[int, int]]:
    """/proc/stat: each CPU's (busy, total) jiffies; "cpu" is all of them."""
    out = {}
    for line in stat.splitlines():
        f = line.split()
        if f and f[0].startswith("cpu"):
            n = [int(x) for x in f[1:]]
            total = sum(n[:8])  # (guest time is already in user)
            out[f[0]] = (total - n[3] - n[4], total)  # idle, iowait
    return out


def irq_counts(text: str) -> dict[str, int]:
    """/proc/interrupts: interrupts so far by kind (gpu, usb), every CPU and line summed."""
    out: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if not f or not f[0].rstrip(":").isdigit():
            continue
        if kind := IRQS.get(f[-1]):
            out[kind] = out.get(kind, 0) + sum(int(x) for x in f[1:] if x.isdigit())
    return out


def busy(was: tuple[int, int], now: tuple[int, int]) -> int:
    return round(100 * (now[0] - was[0]) / max(1, now[1] - was[1]))


class Moment:
    def __init__(self) -> None:
        self.last: tuple | None = None  # (time, cpu times, irq counts)
        self.seconds: deque[dict] = deque(maxlen=60)  # the last minute, one sample a second
        self.lines: deque[tuple[float, str]] = deque(maxlen=200)
        self.resyncs: dict[int, int] = {}  # PipeWire's resyncs so far, by ALSA card: audible

    def tick(self, stat: str, interrupts: str, now: float) -> None:
        """One second's sample, from the texts of /proc/stat and /proc/interrupts."""
        cpus, irqs = cpu_times(stat), irq_counts(interrupts)
        if self.last:
            t, was_cpus, was_irqs = self.last
            dt = max(now - t, 1e-3)
            self.seconds.append({
                "cpu": busy(was_cpus["cpu"], cpus["cpu"]),
                "core": max((busy(was_cpus[k], v) for k, v in cpus.items()
                             if k != "cpu" and k in was_cpus), default=0),
                **{k: round((n - was_irqs.get(k, n)) / dt) for k, n in irqs.items()}})
        self.last = (now, cpus, irqs)

    def read(self) -> None:
        with contextlib.suppress(OSError, KeyError, ValueError):
            self.tick(Path("/proc/stat").read_text(), Path("/proc/interrupts").read_text(),
                      time.monotonic())

    def heard(self, line: str) -> None:
        """A `journalctl -o short-unix` line: "<unix time> <host> <who>[pid]: <text>"."""
        f = line.split(None, 3)
        if len(f) < 4 or f[3].startswith(NOISE):
            return
        with contextlib.suppress(ValueError):
            self.lines.append((float(f[0]), f"{f[2].split('[')[0].rstrip(':')}: {f[3].strip()}"))
        if m := RESYNC.search(f[3]):
            card = int(m[1])
            self.resyncs[card] = self.resyncs.get(card, 0) + 1 + int(m[2])

    def describe(self, now: float | None = None) -> str:
        """This second, in one line: CPU, interrupts against the usual, and the log around it."""
        parts = []
        if self.seconds:
            s = self.seconds[-1]
            parts.append(f"cpu {s['cpu']}% (busiest core {s['core']}%)")
            for k in ("gpu", "usb"):
                if k in s:
                    usual = round(statistics.median(x.get(k, 0) for x in self.seconds))
                    parts.append(f"{k} {s[k]} irq/s (usual {usual})")
        now = time.time() if now is None else now
        said = list(dict.fromkeys(t for at, t in self.lines if now - WINDOW_S <= at <= now + 0.5))
        parts += said[:3] + ([f"+{len(said) - 3} more log lines"] if len(said) > 3 else [])
        return "; ".join(parts) or "nothing sampled yet"

    async def follow(self) -> None:
        """The kernel's and PipeWire's log as it is written, until cancelled. Never raises."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *JOURNAL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_die_with_parent)
            while line := await proc.stdout.readline():
                self.heard(line.decode(errors="replace"))
        except OSError:
            return
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
