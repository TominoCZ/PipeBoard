"""Live wires: a low-resolution picture of the sound each wire carries (PLAN §8a).

One capture listens to every output that has a wire: `pw-cat` at 8 kHz, its node **async**, so
the audio cycle never waits for it, and of class Stream/Input/Audio/Internal, so nothing lists it
as an app recording and it never appears on the board. If this process stalls, only the picture
misses data; the user's audio cannot underrun because of it (tested: a capture blocked for 8 s
moved no xrun counter). For each port it keeps the strongest sample of every millisecond (so
loudness is true for every frequency) in a ring of WINDOW values, mapped to a byte in dB.

Frames to the page are binary and carry only what is new since the last frame (see encode());
the page plays them back at real time. 127 means full scale: clipping.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import itertools
import logging
import math
import os
import shutil
import signal
import struct
from array import array
from collections import deque

log = logging.getLogger(__name__)
# graph.py keeps every pipeboard_scope* node off the board. Each capture gets a name of its own
# (pid and a counter): a restart must never link to the previous capture while it is going away.
NAME = f"pipeboard_scope_{os.getpid()}"
_made = itertools.count(1)


def _die_with_parent() -> None:
    """In the capture's process: the kernel ends it when PipeBoard ends, however that happens."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
RATE, BUCKET, WINDOW = 8000, 8, 96  # 8 kHz, 1 ms per value, ~100 ms on a wire
# Channels kept free, so a wire that appears joins the running capture: a restart for every app
# that came or went reset every wire on the board (user). PipeWire's limit is 64 channels.
SPARE, MAX_CHANNELS = 16, 64
FLOOR_DB = -54.0  # quieter than this draws as a flat wire
CLIP = 127
F_SETPIPE_SZ = 1031


def level(peak: float) -> int:
    """A signed sample peak as a signed byte in dB: −54 dBFS → 0, 0 dBFS → 126, full scale → 127."""
    if math.isnan(peak):
        return 0  # garbage in: draw nothing rather than fail
    mag = abs(peak)
    if mag >= 0.999:
        return CLIP if peak > 0 else -CLIP
    if mag <= 10 ** (FLOOR_DB / 20):
        return 0
    v = round((20 * math.log10(mag) - FLOOR_DB) / -FLOOR_DB * 126)
    return v if peak > 0 else -v


def peaks(samples: array, channels: int) -> list[list[float]]:
    """Interleaved float samples → per channel, the strongest (signed) sample of each BUCKET."""
    out = []
    for ch in range(channels):
        own = samples[ch::channels]
        row = []
        for i in range(0, len(own) - BUCKET + 1, BUCKET):
            seg = own[i:i + BUCKET]
            hi, lo = max(seg), min(seg)
            row.append(hi if hi >= -lo else lo)
        out.append(row)
    return out


# Three bands for brightness: one-pole low-passes at 250 Hz and 2 kHz split the sound into low
# (kick, bass), mid (voice, guitar) and high (hi-hats, "s"); each band stands for this pitch.
SPLITS = tuple(1 - math.exp(-2 * math.pi * hz / RATE) for hz in (250.0, 2000.0))
BAND_HZ = (90.0, 650.0, 3000.0)


def bands(samples: array, channels: int, state: list[list[float]]) -> list[list[tuple]]:
    """Interleaved float samples → per channel and BUCKET: the energy in each band (low, mid,
    high). state: each channel's two filters, carried on between calls."""
    a1, a2 = SPLITS
    out = []
    for ch in range(channels):
        lp1, lp2 = state[ch]
        own, row = samples[ch::channels], []
        for i in range(0, len(own) - BUCKET + 1, BUCKET):
            lo = mid = hi = 0.0
            for x in own[i:i + BUCKET]:
                x = 0.0 if x != x else x  # (NaN)
                lp1 += a1 * (x - lp1)
                lp2 += a2 * (x - lp2)
                lo += lp1 * lp1
                mid += (lp2 - lp1) ** 2
                hi += (x - lp2) ** 2
            row.append((lo, mid, hi))
        state[ch] = [lp1, lp2]
        out.append(row)
    return out


def bright(parts) -> int:
    """How bright a stretch of sound is, 1 … 255 (0: not known): the pitch of its dominant sound,
    the bands' pitches weighted by their energy, on a log scale from 40 Hz to 4 kHz. A kick drum
    reads low, a hi-hat high, and a kick with a quiet hi-hat over it stays low (owner: the wiggle
    wide for a kick, dense for a hi-hat; zero crossings or a change-to-signal ratio would let a
    little treble take over)."""
    energy = [sum(p[b] for p in parts) for b in range(3)]
    total = sum(energy)
    if total < 1e-9:
        return 0
    logf = sum(e * math.log(hz) for e, hz in zip(energy, BAND_HZ, strict=True)) / total
    return max(1, min(255, round(1 + 254 * (logf - math.log(40)) / math.log(100))))


def louder(a: float, b: float) -> float:
    return a if abs(a) >= abs(b) else b


def scaled(chunk: list[float]) -> list[int]:
    """A stretch of signal as bytes: the shape kept linear, the loudness on a dB scale.

    One gain for the whole stretch puts its peak where level() puts it, so a sine stays a sine
    (per-sample dB would square it off) while quiet and loud still read apart. Full scale is 127.
    """
    real = [abs(v) for v in chunk if not math.isnan(v)]
    peak = max(real, default=0.0)
    top = abs(level(peak))
    if top < 2:
        return []
    gain = min(top, 126) / peak
    return [0 if math.isnan(v) else CLIP if v >= 0.999 else -CLIP if v <= -0.999
            else max(-126, min(126, round(v * gain))) for v in chunk]


def encode(rings: list[deque], groups: list[list[int]], new: int, total: int,
           energy: list[deque] | None = None) -> tuple[bytes, set]:
    """One frame: the values each source got since the last frame, for the sources with sound.

    Layout: total (u32, the running count of 1 ms values), then per source with sound: its index
    (u8), how many values (u8), the values (i8, oldest first), its brightness over them (u8,
    bright(); 0 when not known). The page plays them back at real time, so the wave slides
    smoothly from the source down the wire; its wiggle follows the brightness.
    """
    new = min(new, WINDOW)
    if new <= 0:
        return b"", set()
    frame, clipping = bytearray(), set()
    for idx, chans in enumerate(groups):
        chunk = [0.0] * new
        for ch in chans:
            tail = list(rings[ch])[-new:]
            for i, v in enumerate(tail, start=new - len(tail)):
                chunk[i] = louder(chunk[i], v)
        values = scaled(chunk)
        if not values:
            continue  # silent: nothing to send; the page treats missing values as silence
        if CLIP in values or -CLIP in values:
            clipping.add(idx)
        tone = bright([p for ch in chans for p in list(energy[ch])[-new:]]) if energy else 0
        frame += bytes([idx, new]) + bytes(v & 0xFF for v in values) + bytes([tone])
    # Even in silence the count goes out (4 bytes): the page's clock never loses the beat.
    return struct.pack("<I", total & 0xFFFFFFFF) + frame, clipping


class Scope:
    """The capture for a set of sources: [(card key, socket id, [output port ids]), ...].

    sources[i] is the source whose values go out as index i (None: a gap left by one that went,
    reused by the next to come); groups[i] its channels. A source that does not fit is left out.
    """

    def __init__(self, sources: list[tuple[str, str, list[int]]]):
        self.capacity = min(MAX_CHANNELS, sum(len(p) for _, _, p in sources) + SPARE)
        self.free = list(range(self.capacity))
        self.sources: list[tuple | None] = []
        self.groups: list[list[int]] = []
        self.todo = self._place([(k, s, tuple(p)) for k, s, p in sources])  # linked at start
        self.ready = False  # the first links are made: from then on, update() may change them
        self.rings = [deque(maxlen=WINDOW) for _ in range(self.capacity)]
        self.energy = [deque(maxlen=WINDOW) for _ in range(self.capacity)]  # band energies per ms
        self.filters = [[0.0, 0.0] for _ in range(self.capacity)]
        self.total = self.sent = 0  # 1 ms values captured per channel, and how many went out
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.failed = False  # the capture could not start, or died: the user sees plain wires
        self.name = f"{NAME}_{next(_made)}"
        self.dead_since = 0.0  # when the board first saw it dead

    def _place(self, sources: list[tuple]) -> list[tuple[int, int]]:
        """Channels and an index for each new source; returns the (port, channel) links to make."""
        links = []
        for src in sources:
            if len(src[2]) > len(self.free):
                continue
            chans, self.free = self.free[:len(src[2])], self.free[len(src[2]):]
            i = self.sources.index(None) if None in self.sources else len(self.sources)
            if i == len(self.sources):
                self.sources.append(None)
                self.groups.append([])
            self.sources[i], self.groups[i] = src, chans
            links += zip(src[2], chans, strict=True)
        return links

    async def update(self, sources: list[tuple[str, str, list[int]]]) -> bool:
        """Follow a new set of sources without a restart, so the wires carry on: the ones gone are
        unlinked and their channels freed, the new ones linked in. False: they do not fit (or the
        capture is still starting); the caller starts a fresh capture."""
        want = [(k, s, tuple(p)) for k, s, p in sources]
        have = {src: i for i, src in enumerate(self.sources) if src}
        new = [src for src in want if src not in have]
        gone = [i for src, i in have.items() if src not in set(want)]
        room = len(self.free) + sum(len(self.groups[i]) for i in gone)
        if not self.ready or not self.alive or sum(len(src[2]) for src in new) > room:
            return False
        for i in gone:
            for port, ch in zip(self.sources[i][2], self.groups[i], strict=True):
                # The port may be gone already, and its link with it: fine.
                await self._pw_link("-d", str(port), f"{self.name}:input_AUX{ch}")
            self.free += self.groups[i]
            self.sources[i], self.groups[i] = None, []
        for port, ch in self._place(new):
            await self._pw_link(str(port), f"{self.name}:input_AUX{ch}")
        return True

    async def _pw_link(self, *args: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "pw-link", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    @property
    def alive(self) -> bool:
        return not self.failed and (self.task is None or not self.task.done())

    async def start(self) -> None:
        """Start capturing. Never raises: a capture that fails leaves plain wires, nothing else."""
        try:
            await self._start()
        except Exception:  # noqa: BLE001 - a picture must never take the app down
            log.exception("live wires: the capture did not start")
            self.failed = True

    async def _start(self) -> None:
        if not self.todo:
            return
        n = self.capacity
        # stdbuf -o0: pw-cat otherwise holds its output in 4 KB blocks, which with few channels
        # meant data every 65 ms (late and lumpy); unbuffered it comes every PipeWire cycle (~5 ms).
        unbuffered = ["stdbuf", "-o0"] if shutil.which("stdbuf") else []
        self.proc = await asyncio.create_subprocess_exec(
            *unbuffered, "pw-cat", "--record", "-", "--raw", "--rate", str(RATE), "--format", "f32",
            "--channels", str(n), "--channel-map", ",".join(f"AUX{i}" for i in range(n)),
            "--latency", "20ms", "-q", "0",
            # Async: the audio never waits for it. Stream/Input/Audio/Internal, not the plain
            # Stream/Input/Audio: WirePlumber still gives it ports, but the Pulse side (Plasma,
            # mixers) does not list it as an app recording. A made-up class gets no ports at all.
            "-P", f"{{ node.name={self.name} node.description=\"PipeBoard live wires\" "
                  "media.class=Stream/Input/Audio/Internal node.virtual=true "
                  "node.autoconnect=false node.passive=true node.async=true }",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_die_with_parent)
        with contextlib.suppress(OSError):  # room for a second of stalls before pw-cat blocks
            fcntl.fcntl(self.proc.stdout._transport.get_extra_info("pipe"), F_SETPIPE_SZ, 1 << 20)
        self.task = asyncio.create_task(self._guarded(n))

    async def _guarded(self, n: int) -> None:
        try:
            await self._run(n)
            log.warning("live wires: the capture ended")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("live wires: the capture failed")
        self.failed = True

    async def _run(self, n: int) -> None:
        await self._link(n)
        frame, buf = n * 4 * BUCKET, b""
        while chunk := await self.proc.stdout.read(65536):
            buf += chunk
            usable = len(buf) - len(buf) % frame
            if usable:
                samples = array("f")
                samples.frombytes(buf[:usable])
                buf = buf[usable:]
                rows = peaks(samples, n)
                for ring, row in zip(self.rings, rows, strict=True):
                    ring.extend(row)
                for ring, row in zip(self.energy, bands(samples, n, self.filters), strict=True):
                    ring.extend(row)
                self.total += len(rows[0])

    async def _link(self, n: int) -> None:
        """Wire each source port into its channel, once the capture's ports exist."""
        for pid, ch in self.todo:
            for _ in range(20):
                if await self._pw_link(str(pid), f"{self.name}:input_AUX{ch}"):
                    break
                await asyncio.sleep(0.05)
        self.ready = True

    def frame(self) -> tuple[bytes, set[int]]:
        new, self.sent = self.total - self.sent, self.total
        return encode(self.rings, self.groups, new, self.total, self.energy)

    async def stop(self) -> None:
        """Stop and forget the capture; its links go with its node. Never raises."""
        if self.task:
            self.task.cancel()
        with contextlib.suppress(ProcessLookupError, OSError):
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        with contextlib.suppress(Exception):
            self.proc._transport.close()  # its unread pipe, or asyncio complains at exit


class FakeScope(Scope):
    """For --fixture and shots: made-up sound on every source, no PipeWire."""

    async def _start(self) -> None:
        self.ready = True
        self.task = asyncio.create_task(self._guarded(self.capacity))

    async def _pw_link(self, *args: str) -> bool:
        return True

    async def _run(self, n: int) -> None:
        # As much sound as real time has passed, like a sound card: sleeping 20 ms and making 20 ms
        # ran ~4 % slow, which made the page's playhead keep catching up (a false stutter).
        start, made = asyncio.get_running_loop().time(), 0
        while True:
            due = int((asyncio.get_running_loop().time() - start) * 1000)
            for ch, ring in enumerate(self.rings):
                for ms in range(made, due):  # a voice-like tone that swells, fades and pauses
                    u = ms * 0.001
                    gate = 1.0 if math.sin(u * (0.8 + ch * 0.23) + ch) > -0.2 else 0.0
                    loud = gate * (0.05 + 0.5 * math.sin(u * (1.1 + ch * 0.37)) ** 2)
                    hz = 110 + ch * 37
                    tone = math.sin(u * 2 * math.pi * hz) + 0.3 * math.sin(u * 2 * math.pi * 330)
                    ring.append(loud * tone * 0.8)
                    v = loud * tone * 0.8  # (its brightness: mostly low, some mid, as the tone is)
                    self.energy[ch].append((v * v, 0.15 * v * v, 0.0))
            self.total += due - made
            made = due
            await asyncio.sleep(0.005)
