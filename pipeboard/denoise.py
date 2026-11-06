"""The Noise Suppression node's sound (PLAN §5.8): PipeBoard's own small LADSPA plugin around
Speex's preprocessor (native/pb_denoise.c), with a real bypass and a Strength control; run like
the dynamics nodes (dynamics.py: KINDS["denoise"]), one plugin per channel.

It replaced PipeWire's WebRTC module (owner: every built-in effect gets a power button, and a
bypass is a pass-through; WebRTC has none: it ignores changes while running, and with its noise
suppression off it still changed the sound). Measured offline at 48 kHz on the same signals
(plan notes, M17): −40 dB hiss → −64.8 dB alone, −58.8 between syllables, the voice kept at
−0.1 dB, at Strength −30 (WebRTC: −59.2, −54.2, −2.4); bypassed, the input exactly, as it
arrives (no delay; on, 20 ms late).

The plugin is compiled once, on demand, with the system's C compiler, against libspeexdsp (it
comes with PipeWire's Pulse side); neither's headers are needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parent / "native" / "pb_denoise.c"


def built() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "pipeboard" / "pb_denoise.so"


def plugin() -> Path:
    """The plugin, built if missing or older than its source. FileNotFoundError (an OSError: the
    effect's runner reports it) when it cannot be built."""
    out = built()
    if out.is_file() and out.stat().st_mtime >= SRC.stat().st_mtime:
        return out
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        raise FileNotFoundError("Noise Suppression is built once with a C compiler: install gcc")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}")
    made = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-o", str(tmp), str(SRC),
                           "-l:libspeexdsp.so.1", "-lm"], capture_output=True, text=True,
                          timeout=60, check=False)
    if made.returncode:
        tmp.unlink(missing_ok=True)
        why = (made.stderr.strip().splitlines() or ["?"])[-1]
        raise FileNotFoundError(f"could not build Noise Suppression ({why}); it needs libspeexdsp1")
    tmp.replace(out)
    return out
