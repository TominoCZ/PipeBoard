"""Buffer headroom (PLAN §8c): how close the interface's buffers come to running out, read from
the kernel's own status files (/proc/asound/cardN/pcmD{p,c}/sub0), no root, nothing changed.

Output: frames PipeWire has written that are not yet sent to USB (appl_ptr − hw_ptr). The ~2 ms
already in USB transfers is left out: it is committed and cannot cover a late block (owner: the
first figure counted it and looked 2 ms safer than it was). At 0 the output runs dry: a click.
Input: room before the capture ring overflows (buffer − (hw_ptr − appl_ptr)). At 0 it clicks too.

That holds for an interface with implicit feedback (the UR44C: /proc/asound/cardN/stream0 says
"Implicit Feedback Mode: Yes"): snd-usb-audio then never uses its low-latency playback mode and
copies sound into USB transfers ahead of time, an xrun as soon as hw_ptr passes appl_ptr. Without
implicit feedback, low-latency mode defers a transfer when a block is late and only drops out once
the transfers in flight run dry (sound/usb/endpoint.c, pcm.c): there the output reads low.

ponytail: sampled about every millisecond, so a low can read up to a millisecond of drain high;
an exact low would need the kernel's trace of each USB transfer (eBPF, tracepoints)."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

EVERY_S = 0.001


def fields(text: str) -> dict[str, str]:
    """A status or hw_params file: "key: value" lines."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def frames_left(side: str, status: str, buffer: int) -> int | None:
    """Frames of headroom in one buffer now ("out" or "in"); None while it is not running."""
    return _left(side, fields(status), buffer)


def _left(side: str, f: dict[str, str], buffer: int) -> int | None:
    if f.get("state") != "RUNNING" or not buffer:
        return None
    try:
        filled = int(f["appl_ptr"]) - int(f["hw_ptr"])
    except (KeyError, ValueError):
        return None
    return filled if side == "out" else buffer + filled  # (capture: hw runs ahead of appl)


def buffer_frames(folder: Path) -> int:
    """The ring's size in frames, 0 while the device is closed."""
    with contextlib.suppress(OSError, ValueError):
        return int(fields((folder / "hw_params").read_text()).get("buffer_size", 0))
    return 0


def sample(folders: dict[str, Path], seconds: float, every: float = EVERY_S,
           seen: dict | None = None) -> dict[str, int | None]:
    """Each buffer's fewest frames of headroom over `seconds`, read every `every`. Blocking: run
    it in a thread. None for a buffer that was not running the whole time. The status files stay
    open and are read again from the start (a fresh open each time cost ~5 % of a core).
    `seen` (kept by the caller across calls) counts the interface's restarts in "restarts": its
    start time (trigger_time) changing is a stop and start again, an xrun's recovery or a
    reopen. Both directions restart together (linked): counted once."""
    seen = {} if seen is None else seen
    sizes = {side: buffer_frames(d) for side, d in folders.items()}
    low: dict[str, int | None] = dict.fromkeys(folders)
    fds = {}
    try:
        for side, d in folders.items():
            with contextlib.suppress(OSError):
                fds[side] = os.open(d / "status", os.O_RDONLY)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            moved = False
            for side, fd in fds.items():
                with contextlib.suppress(OSError):
                    f = fields(os.pread(fd, 4096, 0).decode())
                    if (t := f.get("trigger_time")) and f.get("state") == "RUNNING":
                        moved |= seen.get(side, t) != t
                        seen[side] = t
                    left = _left(side, f, sizes[side])
                    if left is not None and (low[side] is None or left < low[side]):
                        low[side] = left
            seen["restarts"] = seen.get("restarts", 0) + moved
            time.sleep(every)
    finally:
        for fd in fds.values():
            os.close(fd)
    return low


def folders(nodes: list[dict]) -> dict[str, str]:
    """The interface's status folders by side, from its nodes' properties (alsa.card, .device)."""
    out = {}
    for p in nodes:
        side = {"playback": "out", "capture": "in"}.get(p.get("api.alsa.pcm.stream"))
        if side and side not in out and p.get("alsa.card") is not None:
            out[side] = (f"/proc/asound/card{p['alsa.card']}/pcm{p.get('alsa.device', 0)}"
                         f"{'p' if side == 'out' else 'c'}/sub{p.get('alsa.subdevice', 0)}")
    return out
