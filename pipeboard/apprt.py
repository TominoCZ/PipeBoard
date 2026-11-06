"""Apps' audio in real time (PLAN §8c). Every app with a stream in the graph runs PipeWire's
audio thread in its own process (`pw-data-loop`, `data-loop.N`); it should make that thread
real time itself (module-rt, RTKit), and a sandboxed app may not (owner: Spotify's Snap, its own
libpipewire 1.0.5, left it at normal priority; late by a millisecond now and then, it stalled
every sound in the graph: 6 resyncs in 2 minutes at block 192, none once raised).

PipeBoard raises that one thread to SCHED_FIFO, never the app: it does the per-cycle copy and
resampling only (Spotify's: ~6 µs a 1 ms cycle), so a game keeps its CPU, and the kernel's RT
throttling still caps a runaway. Below PipeWire's own (95, pulse 83), so the server comes first.
Allowed without root by the app's own RLIMIT_RTPRIO (the owner's session: 95). Async nodes
(PipeBoard's captures) are left alone: the graph does not wait for them. Nothing here raises."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)
PRIO = 80
PROC = Path("/proc")
THREAD = re.compile(r"^(pw-)?data-loop")  # (libpipewire's audio thread, by its name)
OWN = {"pipewire", "pipewire-pulse", "wireplumber"}  # PipeWire's own processes set their own
RAISED: set[int] = set()  # the threads PipeBoard raised, put back when the setting goes off


def apps(dump: list[dict]) -> dict[int, str]:
    """The processes owning an audio stream the graph waits for, {host pid: app name}."""
    clients = {o["id"]: (o.get("info") or {}).get("props") or {}
               for o in dump if o["type"].endswith("Client")}
    found = {}
    for o in dump:
        p = (o.get("info") or {}).get("props") or {}
        kind = str(p.get("media.class", ""))
        if not o["type"].endswith("Node") or not kind.startswith("Stream/") \
                or "Audio" not in kind or p.get("node.async"):
            continue
        c = clients.get(p.get("client.id"), {})
        pid = c.get("pipewire.sec.pid") or c.get("application.process.id")  # (sec: the host's)
        if pid and c.get("application.process.binary") not in OWN:
            found[int(pid)] = c.get("application.name") or p.get("application.name") \
                or c.get("application.process.binary") or f"pid {pid}"
    return found


def threads(pid: int) -> list[int]:
    """The app's PipeWire audio threads, by name."""
    out = []
    with contextlib.suppress(OSError):
        for task in (PROC / str(pid) / "task").iterdir():
            with contextlib.suppress(OSError):
                if THREAD.match((task / "comm").read_text().strip()):
                    out.append(int(task.name))
    return out


def sync(dump: list[dict], on: bool,
         get: Callable[[int], int] = os.sched_getscheduler,
         put: Callable[[int, int, int], None] = lambda tid, pol, prio: os.sched_setscheduler(
             tid, pol, os.sched_param(prio))) -> list[dict]:
    """Each app's audio thread state, raising those at normal priority when `on` (kept in RAISED,
    so they can be put back). [{name, state}]: own (real time by itself),
    raised (by PipeBoard), normal (not raised: the setting is off), refused, or none (no thread
    found)."""
    out = []
    for pid, name in sorted(apps(dump).items(), key=lambda x: x[1].lower()):
        tids = threads(pid)
        state = "normal" if tids else "none"
        for tid in tids:
            try:
                rt = get(tid) in (os.SCHED_FIFO, os.SCHED_RR)
                if rt:
                    state = "raised" if tid in RAISED else ("own" if state == "normal" else state)
                elif on:
                    put(tid, os.SCHED_FIFO, PRIO)
                    RAISED.add(tid)
                    state = "raised"
                    log.info("apps' audio: %s's %d raised to real time", name, tid)
            except (OSError, ValueError):  # (gone, or its limit does not allow it)
                state = "refused" if state == "normal" else state
        out.append({"name": name, "state": state})
    return out


def lower(put: Callable[[int, int, int], None] = lambda tid, pol, prio:
          os.sched_setscheduler(tid, pol, os.sched_param(prio))) -> None:
    """Put the threads PipeBoard raised back to normal priority (the setting turned off)."""
    for tid in list(RAISED):
        with contextlib.suppress(OSError, ValueError):
            put(tid, os.SCHED_OTHER, 0)
        RAISED.discard(tid)
