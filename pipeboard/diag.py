"""Diagnostic cards (PLAN §8d): Waveform and Stereo. Each card is a quiet null sink on the board
(devices.py): what is wired into it is copied there by PipeWire, nothing in the sound changes.
One capture reads every card that is on from its sink's monitor, two channels each, only while a
window shows the board; the page draws it. Under the live wires' rules (PLAN §8a): an async node
(the audio never waits for it), /Internal (no app lists it), and nothing here may raise."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import shutil
import time
from array import array

from pipeboard.scope import _die_with_parent

log = logging.getLogger(__name__)
RATE = 48000  # enough for a waveform's shape and a stereo picture; PipeWire resamples to it
KEEP_S = 0.25  # at most this much is held between frames (a page that stalls gets the newest)
LATENCY = "20ms"  # the block the capture asks PipeWire for


def monitors(dump: list[dict], nodes: dict[str, str]) -> dict[str, list[int]]:
    """Each card's monitor ports, left then right, by its node's name ({node.name: card key})."""
    ids = {o["id"]: nodes[n] for o in dump if o["type"].endswith("Node")
           and (n := ((o.get("info") or {}).get("props") or {}).get("node.name")) in nodes}
    found: dict[str, list] = {}
    for o in dump:
        p = (o.get("info") or {}).get("props") or {}
        if o["type"].endswith("Port") and p.get("node.id") in ids \
                and (o.get("info") or {}).get("direction") == "output":
            found.setdefault(ids[p["node.id"]], []).append((p.get("port.id", 0), o["id"]))
    return {k: [pid for _, pid in sorted(v)] for k, v in found.items()}


MARK = b"\xff\xff\xff\xff"  # starts a binary frame of diagnostics (a live wires' starts: a count)


def clips(data: bytes, channels: int, card: int) -> bool:
    """Whether a card's two channels reach full scale anywhere in this frame (a Counter's
    clipping). The array's own min and max (C speed: owner, the visualizers' load)."""
    a = array("f", data)
    sides = [a[ch::channels] for ch in (2 * card, 2 * card + 1)]
    return any(s and (max(s) >= 0.999 or min(s) <= -0.999) for s in sides)


class Taps:
    """One capture for every card that is on: `cards` is [(key, [left port, right port])], its
    channels in that order. take() hands the page what came in since the last frame."""

    def __init__(self, cards: list[tuple[str, list[int]]]):
        self.cards, self.name = cards, f"pipeboard_scope_diag_{secrets.token_hex(3)}"
        self.buf = bytearray()
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    @property
    def channels(self) -> int:
        return 2 * len(self.cards)

    async def start(self) -> None:
        try:
            unbuffered = ["stdbuf", "-o0"] if shutil.which("stdbuf") else []
            n = self.channels
            self.proc = await asyncio.create_subprocess_exec(
                *unbuffered, "pw-cat", "--record", "-", "--raw", "--rate", str(RATE),
                "--format", "f32", "--channels", str(n),
                "--channel-map", ",".join(f"AUX{i}" for i in range(n)), "--latency", LATENCY,
                "-P", f"{{ node.name={self.name} node.description=\"PipeBoard diagnostics\" "
                      "media.class=Stream/Input/Audio/Internal node.virtual=true "
                      "node.autoconnect=false node.passive=true node.async=true }",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_die_with_parent)
            self.task = asyncio.create_task(self._run())
        except Exception:  # noqa: BLE001 - no picture is no reason to stop anything
            log.exception("diagnostics: the capture did not start")

    async def _link(self, port: int, channel: int) -> None:
        for _ in range(20):  # (its ports appear a moment after it starts)
            link = await asyncio.create_subprocess_exec(
                "pw-link", str(port), f"{self.name}:input_AUX{channel}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await link.wait() == 0:
                return
            await asyncio.sleep(0.05)

    async def _run(self) -> None:
        try:
            for i, (_key, ports) in enumerate(self.cards):
                for side, port in enumerate(ports[:2]):
                    await self._link(port, 2 * i + side)
            frame = 4 * self.channels
            while chunk := await self.proc.stdout.read(65536):
                self.buf += chunk
                del self.buf[:-int(RATE * KEEP_S) * frame]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("diagnostics: the capture failed")

    def take(self) -> bytes:
        """Whole frames that came in since the last call (raw floats, the cards' channels)."""
        frame = 4 * self.channels
        n = len(self.buf) // frame * frame
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    async def stop(self) -> None:
        """Never raises; its node and links go with the process."""
        if self.task:
            self.task.cancel()
        with contextlib.suppress(ProcessLookupError, OSError):
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        with contextlib.suppress(Exception):
            self.proc._transport.close()  # its unread pipe


class FakeTaps(Taps):
    """For --fixture and shots: made-up stereo music on every card, no PipeWire."""

    async def start(self) -> None:
        self.t0 = self.sent = time.monotonic()

    def take(self) -> bytes:
        now = time.monotonic()
        start, n = int((self.sent - self.t0) * RATE), int((now - self.sent) * RATE)
        self.sent += n / RATE
        out = array("f")
        for i in range(start, start + min(n, int(RATE * KEEP_S))):
            t = i / RATE
            mid = 0.5 * math.sin(2 * math.pi * 220 * t) + 0.2 * math.sin(2 * math.pi * 661 * t)
            side = mid * 0.4 * math.sin(2 * math.pi * 3 * t)  # (leaning slowly: it fans out)
            side += 0.06 * math.sin(2 * math.pi * 330 * t)
            out.extend([mid + side, mid - side] * len(self.cards))
        return out.tobytes()

    async def stop(self) -> None:
        return None
