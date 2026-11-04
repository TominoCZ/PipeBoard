"""Gain nodes: their volume is the gain, changed in small steps so it does not click (PLAN §5.1).

A gain node is a lingering null sink whose monitor carries its input times its volume (measured
exact). PipeWire's own volume ramp is ignored for such a node, so changes are stepped here: 0.1 dB
every 2 ms through one persistent pw-cli (a hard 0.5 → 2.0 jump made a 0.24 sample step; 0.1 dB
steps 0.06, near the 0.03 of the tone itself). Nothing here may crash the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import shutil

from pipeboard.scope import _die_with_parent

log = logging.getLogger(__name__)
# Levels are dB with one decimal, from MIN_DB (−144: silence in practice, the floor of 24-bit sound)
# to MAX_DB. A ramp has at most MAX_STEPS steps of at least STEP_DB, and makes its fine steps only
# above QUIET_DB (−40 dB, a hundredth of the signal): below that a step is too small to hear.
MIN_DB, MAX_DB, STEP_DB, STEP_S, MAX_STEPS, QUIET_DB = -144.0, 24.0, 0.1, 0.002, 200, -40.0


def clamp(db) -> float:
    """A level from the page or a saved board: one decimal (half up, like the page), −144 … +24.
    None and "-inf" (from an older board) are −144."""
    if db is None or (isinstance(db, str) and db.strip().lower() in ("-inf", "-infinity")):
        return MIN_DB
    db = float(db)
    if math.isnan(db):
        return 0.0
    return math.floor(max(MIN_DB, min(MAX_DB, db)) * 10 + 0.5) / 10


def level(dev: dict) -> float:
    """What a gain node is set to: its level, or 0 dB (an exact pass-through) while bypassed."""
    return 0.0 if dev.get("bypass") else clamp(dev.get("gain_db", 0.0))


def factor(db: float) -> float:
    return round(10 ** (db / 20), 9)


def _line(start: float, end: float) -> list[float]:
    n = min(MAX_STEPS, max(1, math.ceil(abs(end - start) / STEP_DB - 1e-9)))
    return [start + (end - start) * k / n for k in range(1, n + 1)]


def steps(start: float, end: float) -> list[float]:
    """The levels a ramp passes through after start, ending exactly at end."""
    if start < QUIET_DB and end < QUIET_DB:
        return [end]
    if end < QUIET_DB:
        return _line(start, QUIET_DB) + [end]
    if start < QUIET_DB:
        return [QUIET_DB] + ([] if end == QUIET_DB else _line(QUIET_DB, end))
    return _line(start, end)


class Gains:
    """Sets gain nodes' volumes, ramped, one ramp at a time per node. `live=False` only records."""

    def __init__(self, live: bool):
        self.live = live
        self.cli: asyncio.subprocess.Process | None = None
        self.now: dict[int, float] = {}  # node id → dB it is at
        self.ramps: dict[int, asyncio.Task] = {}
        self.sent: list[tuple[int, float]] = []  # what went out (for checks)
        # One command at a time: two at launch (two gains, two EQs) each started a pw-cli while
        # the other was still connecting; the forgotten one's pipe errored at exit.
        self.lock = asyncio.Lock()

    async def set(self, node: int, db: float, channels: int = 2, ramp: bool = True) -> None:
        """Move a node's gain to db: in steps, or at once (a node that has just appeared)."""
        old = self.ramps.pop(node, None)
        if old:
            old.cancel()
        start = self.now.get(node)
        path = steps(start, db) if ramp and start is not None else [db]
        self.ramps[node] = asyncio.create_task(self._ramp(node, path, channels))

    async def _ramp(self, node: int, path: list[float], channels: int) -> None:
        try:
            for db in path:
                await self._say(node, db, channels)
                self.now[node] = db
                if len(path) > 1:
                    await asyncio.sleep(STEP_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a gain that fails to move must not take the app down
            log.exception("gain: could not set the volume")

    async def _say(self, node: int, db: float, channels: int) -> None:
        self.sent.append((node, db))
        f = factor(db)
        await self.command(
            f"set-param {node} Props {{ channelVolumes = [ {' '.join([str(f)] * channels)} ] }}")

    async def command(self, line: str) -> None:
        """One line to the persistent pw-cli (the EQ's coefficients go this way too)."""
        if not self.live:
            return
        async with self.lock:
            await self._write(line)

    async def _write(self, line: str) -> None:
        if self.cli is None or self.cli.returncode is not None:
            # stdbuf -oL: piped, pw-cli holds its output in blocks, so "connected" never came
            # through and every first command waited the full 3 s.
            lines = ["stdbuf", "-oL"] if shutil.which("stdbuf") else []
            self.cli = await asyncio.create_subprocess_exec(
                *lines, "pw-cli", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, preexec_fn=_die_with_parent)
            await self._connected(self.cli)
        self.cli.stdin.write((line + "\n").encode())
        await self.cli.stdin.drain()

    async def _connected(self, cli: asyncio.subprocess.Process) -> None:
        """Wait until pw-cli is connected: a command it reads before that fails ("unknown
        global"), so the first level sent to a fresh pw-cli was lost (found by the EQ's live
        test). Then keep reading what it says, so its pipe never fills, and log its errors."""
        try:
            async with asyncio.timeout(3):
                while (got := await cli.stdout.readline()) and b"remote 0 is named" not in got:
                    pass
        except TimeoutError:
            log.warning("pw-cli did not say it was connected; sending anyway")
        asyncio.create_task(self._listen(cli))

    @staticmethod
    async def _listen(cli: asyncio.subprocess.Process) -> None:
        async for got in cli.stdout:
            text = got.decode(errors="replace").strip()
            if "Error" in text and "unsupported type" not in text:
                log.warning("pw-cli: %s", text)

    async def stop(self) -> None:
        for task in self.ramps.values():
            task.cancel()
        with contextlib.suppress(ProcessLookupError, OSError, AttributeError):
            self.cli.stdin.close()
            self.cli.kill()
            await self.cli.wait()
            self.cli._transport.close()  # its stdout pipe, before the loop closes (as watch does)
