"""Dynamics nodes: the Limiter and the Compressor (PLAN §5.5, §5.6). Both are one LSP plugin
(LADSPA) in a filter-chain process, started and stopped like an EQ's (eq.Eqs), their settings
set live through the shared pw-cli, and one live panel that shows what goes in and comes out.

Limiter: keeps the sound under a ceiling so the card does not clip. Between nodes PipeWire's
sound is float and never clips; it clips where it becomes integers, at the card (the UR44C
takes s32). Measured (plan notes, M1): a +6 dB tone under a −0.5 dB ceiling peaks at 0.9435.
Compressor: turns loud passages down by its ratio above a threshold, for squashing music on
purpose; put a limiter after it. Measured offline (M7): ratio 4 gives 1 dB out per 4 dB in
above the threshold; 10 ms attack and 150 ms release reach 63 % in 12 and 117 ms.
Noise gate: turns the sound down by its range while it stays under the threshold (hiss between
words). Measured offline (M10): a −30 dB threshold opens at −29.7 and closes at −34.8 dB.
De-esser: turns harsh "s" sounds down, split-band: the sound is split at its frequency
(Linkwitz–Riley, PipeWire's own biquads) and only the top band goes through LSP's compressor;
the rest of the voice is never touched. Measured offline (M14): "s" −7.6 dB with the voice's body
±0.0 dB during it (TAP's and a wideband compressor ducked the whole voice by ~7 dB).
Noise suppression: our own plugin around Speex's (denoise.py), Strength being how far noise is
turned down.
"""

from __future__ import annotations

import math
import operator
import os
import secrets
from array import array
from pathlib import Path

from pipeboard import denoise, eq

FILE = "lsp-plugins-ladspa.so"
DIRS = ("/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa", "/usr/lib64/ladspa",
        "/usr/local/lib/ladspa")
LSP = "http://lsp-plug.in/plugins/ladspa/"
# The panel's capture (eq.Spectrum): in L, in R, out L, out R at 48 kHz; peaks per 10 ms block.
CAPTURE, BLOCK = ("FL", "FR", "RL", "RR"), 480
BLOCK_BYTES = BLOCK * len(CAPTURE) * 4

# Limiter. LSP's own default turns on Gain boost (makeup gain up to 0 dBFS: the ceiling would not
# hold): off. 5 ms lookahead catches a peak's onset; release 20 ms is its slowest.
# Smooth (owner: heavy limiting sounded harsh) turns on its automatic level regulation, a slow
# leveller before the brickwall, and the wider reduction shape (mode 1, Herm Wide; Sharp is mode
# 0, Herm Thin). Measured offline at 192 kHz, 50 Hz at 0 dBFS under a −14 dB ceiling (plan notes,
# M6): Sharp 35% THD, Smooth 0.25%, held 0.8 dB under the ceiling, quiet sound untouched. The
# leveller's target is Threshold × Knee − ~8 dB: Knee +7.5 dB puts it just under the ceiling
# (+9 dB hands the work back to the brickwall: 1.2%); 20 ms attack / 500 ms release pump least.
LIMITER_FIXED = {"Gain boost": 0.0, "Lookahead (ms)": 5.0, "Attack time (ms)": 5.0,
                 "Release time (ms)": 20.0, "Oversampling": 0.0, "Dithering": 0.0,
                 "Automatic level regulation attack time (ms)": 20.0,
                 "Automatic level regulation release time (ms)": 500.0,
                 "Knee level (G)": 2.371374}
# Compressor: downward, RMS detection over 10 ms (LSP's own default: smoother on music than
# peaks), a −6 dB soft knee, no sidechain filters, all wet. LSP's LADSPA defaults are not its
# own (attack 1.5 s, reactivity 187 ms), so every control is set here.
COMPRESSOR_FIXED = {"Compression mode": 0.0, "Sidechain type": 0.0, "Sidechain mode": 1.0,
                    "Sidechain lookahead (ms)": 0.0, "Sidechain reactivity (ms)": 10.0,
                    "Sidechain source": 0.0, "Sidechain preamp (G)": 1.0,
                    "Release threshold (G)": 0.0, "Hold time (ms)": 0.0, "Knee (G)": 0.501187,
                    "Dry gain (G)": 0.0, "Wet gain (G)": 1.0, "Dry/Wet balance (%)": 100.0,
                    "High-pass filter mode": 0.0, "Low-pass filter mode": 0.0,
                    "Stereo split": 0.0}

# Noise gate: peak detection over 10 ms, so its threshold means what the panel's peaks show (RMS
# opened 3 dB above the line on a tone); hysteresis: it closes 4 dB under where it opens, so a
# voice hovering at the threshold does not make it chatter. A soft 6 dB zone under both: LSP's
# attack and release smooth its level reading, not the gain, so a hard threshold (zone 1) jumped
# 40 dB within 1 ms on every open and close (owner: "a sharp on-off"); with the zone the gain
# fades as the level crosses it: at most 2.6–3.4 dB/ms where it can be heard, still closing fully
# (−40 dB) on hiss and not chopping syllables (plan notes, M10 fix; ZamGateX2 compared, no hold).
GATE_FIXED = {"Sidechain input": 0.0, "Sidechain mode": 0.0, "Sidechain lookahead (ms)": 0.0,
              "Sidechain source": 0.0, "Sidechain reactivity (ms)": 10.0,
              "Sidechain preamp (G)": 1.0, "High-pass filter mode": 0.0,
              "Low-pass filter mode": 0.0, "Stereo split": 0.0, "Hysteresis": 1.0,
              "Curve zone size (G)": 0.501187, "Hysteresis threshold (G)": 0.630957,
              "Hysteresis zone size (G)": 0.501187, "Makeup gain (G)": 1.0, "Dry gain (G)": 0.0,
              "Wet gain (G)": 1.0, "Dry/Wet balance (%)": 100.0}

# De-esser: LSP's compressor on the top band only, peak detection over 1 ms (an "s" is short),
# ratio 6, 1 ms attack, 60 ms release, no lookahead (a delay on the top band alone would comb
# against the bottom band when they are summed).
DEESSER_FIXED = {**COMPRESSOR_FIXED, "Sidechain mode": 0.0, "Sidechain reactivity (ms)": 1.0,
                 "Ratio": 6.0, "Attack time (ms)": 1.0, "Release time (ms)": 60.0,
                 "Makeup gain (G)": 1.0}
XOVER = tuple(f"{band}{ch}{i}" for ch in "LR" for band in ("lo", "hi") for i in (1, 2))

# Per kind: its plugin, its node in the graph, what the page may set (lo, hi, default; one
# decimal) and its on/off flags.
KINDS = {
    "limiter": {"name": "Limiter", "label": LSP + "limiter_stereo", "node": "lim",
                "prefix": "pb_lim", "fixed": LIMITER_FIXED,
                "fields": {"ceiling_db": (-24.0, 0.0, -0.5)}, "flags": ("bypass", "smooth")},
    "compressor": {"name": "Compressor", "label": LSP + "compressor_stereo", "node": "comp",
                   "prefix": "pb_comp", "fixed": COMPRESSOR_FIXED,
                   "fields": {"threshold_db": (-60.0, 0.0, -18.0), "ratio": (1.0, 20.0, 4.0),
                              "attack_ms": (0.1, 200.0, 10.0),
                              "release_ms": (10.0, 2000.0, 150.0),
                              "makeup_db": (0.0, 24.0, 0.0)},
                   "flags": ("bypass",)},
    "gate": {"name": "Noise Gate", "label": LSP + "gate_stereo", "node": "gate",
             "prefix": "pb_gate", "fixed": GATE_FIXED,
             "fields": {"threshold_db": (-60.0, 0.0, -40.0), "range_db": (-70.0, 0.0, -40.0),
                        "attack_ms": (0.1, 200.0, 5.0), "hold_ms": (0.0, 1000.0, 100.0),
                        "release_ms": (10.0, 2000.0, 150.0)},
             "flags": ("bypass",)},
    # Our own plugin (denoise.py): mono, one per channel; its own bypass is exact.
    "denoise": {"name": "Noise Suppression", "label": "pb_denoise", "node": "ns",
                "prefix": "pb_ns", "fixed": {},
                # Default −12 dB (owner: at −30 the voice sounded low quality, Speex's watery
                # artefacts; gentler removes less noise but keeps the voice natural).
                "fields": {"strength_db": (-40.0, 0.0, -12.0)}, "flags": ("bypass",)},
    "deesser": {"name": "De-Esser", "label": LSP + "compressor_stereo", "node": "ds",
                "prefix": "pb_ds", "fixed": DEESSER_FIXED,
                "fields": {"threshold_db": (-60.0, 0.0, -30.0),
                           "freq_hz": (2000.0, 12000.0, 5000.0)},
                "flags": ("bypass",)},
}
# Every number a profile compares (profiles._settings; flags are compared as bools there).
SETTINGS = ("ceiling_db", "threshold_db", "ratio", "attack_ms", "release_ms",
            "makeup_db", "range_db", "hold_ms", "freq_hz", "strength_db")


def new(kind: str) -> dict:
    k = KINDS[kind]
    dev = {"id": "d" + secrets.token_hex(5), "type": kind, "name": k["name"], "channels": 2,
           **{f: lo_hi_def[2] for f, lo_hi_def in k["fields"].items()},
           **dict.fromkeys(k["flags"], False)}
    dev["node"] = f"{k['prefix']}_{dev['id']}"
    return dev


def clean(kind: str, field: str, value) -> float:
    """A setting from the page or a saved board: in its range, one decimal."""
    lo, hi, default = KINDS[kind]["fields"][field]
    return round(eq._num(value, lo, hi, default), 1)


def update(dev: dict, op: dict) -> None:
    """The settings an op from the page carries, made safe, onto the device."""
    k = KINDS[dev["type"]]
    for field in k["fields"]:
        if field in op:
            dev[field] = clean(dev["type"], field, op[field])
    for flag in k["flags"]:
        if flag in op:
            dev[flag] = bool(op[flag])


def _gain(db: float) -> float:
    return round(10 ** (db / 20), 6)


def controls(dev: dict) -> dict:
    """What the page sets, as LSP's controls (levels as factors)."""
    get = lambda f: clean(dev["type"], f, dev.get(f))  # noqa: E731
    bypass = 1.0 if dev.get("bypass") else 0.0
    if dev["type"] == "limiter":
        smooth = 1.0 if dev.get("smooth") else 0.0
        return {"Threshold (G)": _gain(get("ceiling_db")), "Bypass": bypass,
                "Automatic level regulation": smooth, "Operating mode": smooth}
    if dev["type"] == "gate":
        return {"Curve threshold (G)": _gain(get("threshold_db")),
                "Reduction (G)": _gain(get("range_db")), "Attack (ms)": get("attack_ms"),
                "Hold time (ms)": get("hold_ms"), "Release (ms)": get("release_ms"),
                "Bypass": bypass}
    if dev["type"] == "deesser":
        return {"Attack threshold (G)": _gain(get("threshold_db")), "Bypass": bypass}
    if dev["type"] == "denoise":
        return {"Strength (dB)": get("strength_db"), "Bypass": bypass}
    return {"Attack threshold (G)": _gain(get("threshold_db")), "Ratio": get("ratio"),
            "Attack time (ms)": get("attack_ms"), "Release time (ms)": get("release_ms"),
            "Makeup gain (G)": _gain(get("makeup_db")), "Bypass": bypass}


def params(dev: dict) -> str:
    """`set-param` Props for the controls: "lim:Threshold (G)" 0.944 … (a de-esser's crossover
    frequency too, on each of its biquads)."""
    node = KINDS[dev["type"]]["node"]
    live = {f"{node}:{k}": v for k, v in controls(dev).items()}
    if dev["type"] == "deesser":
        live |= {f"{b}:Freq": clean("deesser", "freq_hz", dev.get("freq_hz")) for b in XOVER}
    return " ".join(f'"{k}" {v!r}' for k, v in live.items())


def plugin() -> Path:
    """LSP's LADSPA library; FileNotFoundError (an OSError: the EQ runner reports it) if absent."""
    dirs = [*os.environ.get("LADSPA_PATH", "").split(":"), *DIRS]
    found = next((Path(d) / FILE for d in dirs if d and (Path(d) / FILE).is_file()), None)
    if not found:
        raise FileNotFoundError("LSP's plugins are not installed (package lsp-plugins-ladspa)")
    return found


def conf(dev: dict) -> str:
    k = KINDS[dev["type"]]
    values = " ".join(f'"{n}" = {v!r}' for n, v in {**k["fixed"], **controls(dev)}.items())
    lib = denoise.plugin() if dev["type"] == "denoise" else plugin()
    node = (f'{{ type = ladspa name = {k["node"]} plugin = "{lib}" label = "{k["label"]}" '
            f'control = {{ {values} }} }}')
    n = k["node"]
    if dev["type"] == "deesser":
        return _deesser(dev, node)
    if dev["type"] == "denoise":  # mono: filter-chain copies it per channel; Speex is for 48 kHz
        pos = "audio.position = [ FL FR ]"  # (the streams resample to it)
        return eq.chain(dev, [node], [], f'"{n}:Input"', f'"{n}:Output"').replace(
            pos, f"{pos}\n      audio.rate = 48000")
    return eq.chain(dev, [node], [], f'"{n}:Input L" "{n}:Input R"',
                    f'"{n}:Output L" "{n}:Output R"')


def _deesser(dev: dict, comp: str) -> str:
    """Per channel: the input split by two Butterworth low-passes and two high-passes in a row
    (Linkwitz–Riley, 24 dB/octave: the bands sum back flat), the top band through the stereo
    compressor `ds`, both bands mixed again."""
    f = clean("deesser", "freq_hz", dev.get("freq_hz"))
    nodes, links = [comp], []
    for ch in "LR":
        nodes += [f"{{ type = builtin name = in{ch} label = copy }}",
                  f'{{ type = builtin name = mix{ch} label = mixer '
                  f'control = {{ "Gain 1" = 1.0 "Gain 2" = 1.0 }} }}']
        for band, label in (("lo", "bq_lowpass"), ("hi", "bq_highpass")):
            nodes += [f'{{ type = builtin name = {band}{ch}{i} label = {label} '
                      f'control = {{ "Freq" = {f!r} "Q" = 0.7071 }} }}' for i in (1, 2)]
        pairs = [(f"in{ch}:Out", f"lo{ch}1:In"), (f"lo{ch}1:Out", f"lo{ch}2:In"),
                 (f"lo{ch}2:Out", f"mix{ch}:In 1"), (f"in{ch}:Out", f"hi{ch}1:In"),
                 (f"hi{ch}1:Out", f"hi{ch}2:In"), (f"hi{ch}2:Out", f"ds:Input {ch}"),
                 (f"ds:Output {ch}", f"mix{ch}:In 2")]
        links += [f'{{ output = "{a}" input = "{b}" }}' for a, b in pairs]
    return eq.chain(dev, nodes, links, '"inL:In" "inR:In"', '"mixL:Out" "mixR:Out"')


def capture(model: dict, key: str) -> list[tuple[int, str]]:
    """What the panel's capture links: the ports wired into the node (PipeWire sums them, as its
    own input does) to in L/R, its output ports to out L/R."""
    cards = {c["key"]: c for c in model["cards"]}
    card = cards.get(key)
    if not card or not card["outputs"]:
        return []
    ports = []
    for w in model["wires"]:
        src = cards.get(w["from"][0])
        sock = next((s for s in src["outputs"] if s["id"] == w["from"][1]), None) if src else None
        if w["to"][0] == key and sock:
            ports += [(sock["ports"][i], CAPTURE[j]) for i, j in w["pairs"] if j < 2]
    return ports + [(p, CAPTURE[2 + j]) for j, p in enumerate(card["outputs"][0]["ports"][:2])]


# How much later an effect's sound comes out than it goes in, in samples at the capture's 48 kHz:
# the plugin's own (measured live, plan notes M22/M34: the limiter's 5 ms lookahead; Speex's 20 ms
# and the 48 kHz resampling around it), plus none, one or two graph cycles, as the graph happens
# to run it: fed by a player it was one (256 samples at 1024 / 192 kHz), behind the owner's Bass
# EQ none. So the cycles are found from the sound itself (Aligned).
DELAY = {"limiter": 240, "denoise": 1016}


def latency_ms(kind: str) -> float:
    """What an effect of PipeBoard's own adds itself, in ms: the limiter its lookahead, Noise
    Suppression its two 10 ms Speex frames (pb_denoise.c reports 2 frames); the rest none (no
    lookahead set; the EQ's filters work sample by sample)."""
    return {"limiter": LIMITER_FIXED["Lookahead (ms)"], "denoise": 20.0}.get(kind, 0.0)


def delays(kind: str, clock: dict | None) -> list[int]:
    """The delays an effect's output may trail its input by now: its own plus 0, 1, 2 cycles."""
    clock = clock or {}
    cycle = round((clock.get("quantum") or 1024) / (clock.get("rate") or 48000) * 48000)
    return [DELAY.get(kind, 0) + k * cycle for k in range(3)]


def _match(a: array, b: array, d: int) -> float:
    """How alike b is to a delayed by d samples: their correlation, 0 … 1."""
    x, y = a[:len(a) - d], b[d:]
    dot = lambda u, v: sum(map(operator.mul, u, v))  # noqa: E731
    xy, xx, yy = dot(x, y), dot(x, x), dot(y, y)
    return xy / math.sqrt(xx * yy) if xx > 1e-9 and yy > 1e-9 else 0.0


class Aligned:
    """Peaks per block with the in channels delayed by the effect's delay, to the sample, so a
    block's in and out are the same sound (owner: the peaks differed; shifting whole 10 ms blocks
    on the page left up to 1.7 dB, and a fixed cycle was half a block wrong on their board). About
    once a second, while there is sound, the candidate delays are tried on the last half second
    (left channels) and the best match is kept."""

    def __init__(self, key: str):
        self.key, self.delay, self.carry = key, -1, array("f")
        self.heard_in, self.heard_out, self.since = array("f"), array("f"), 0

    def peaks(self, data: bytes, candidates: list[int]) -> list[list[float]]:
        a, n = array("f"), len(CAPTURE)
        a.frombytes(data)
        frames = len(a) // n
        self._learn(a[0::n], a[2::n], candidates)
        if self.delay not in candidates:  # (first, or the graph's cycle changed)
            self._use(candidates[0])
        ins = array("f", [0.0]) * (2 * frames)
        ins[0::2], ins[1::2] = a[0::n], a[1::n]
        ins = self.carry + ins
        a[0::n], a[1::n] = ins[0:2 * frames:2], ins[1:2 * frames:2]
        self.carry = ins[2 * frames:]
        return peaks(a.tobytes())

    def _use(self, delay: int) -> None:  # (from silence: one block may show in quieter)
        self.delay, self.carry = delay, array("f", [0.0]) * (2 * delay)

    def _learn(self, left_in: array, left_out: array, candidates: list[int]) -> None:
        keep = 24000 + max(candidates)
        self.heard_in = (self.heard_in + left_in)[-keep:]
        self.heard_out = (self.heard_out + left_out)[-keep:]
        self.since += len(left_in)
        if self.since < 48000 or len(self.heard_in) < keep:
            return
        self.since = 0
        score = {d: _match(self.heard_in, self.heard_out, d) for d in candidates}
        best = max(score, key=score.get)
        if best != self.delay and score[best] > max(0.5, score.get(self.delay, 0) + 0.05):
            self._use(best)


def peaks(data: bytes) -> list[list[float]]:
    """Per 10 ms block of the capture: the loudest sample going in and coming out (either side)."""
    a = array("f")
    a.frombytes(data)
    out, n = [], len(CAPTURE)
    for at in range(0, len(a) - BLOCK * n + 1, BLOCK * n):
        block = a[at:at + BLOCK * n]
        out.append([round(max(max(map(abs, block[ch::n])) for ch in pair), 4)
                    for pair in ((0, 1), (2, 3))])
    return out
