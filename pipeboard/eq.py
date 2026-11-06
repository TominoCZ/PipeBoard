"""The EQ node: a parametric equalizer built into PipeBoard (PLAN §5.3).

The sound: a PipeWire filter-chain, run as its own `pipewire -c <conf>` process, with two fixed
rows of SLOTS `bq_raw` biquads per channel and a mixer after them. PipeBoard computes every slot's
coefficients itself (the RBJ audio-EQ cookbook, at the graph's rate) and sets them live with
`pw-cli set-param`, so a band can be added, retyped or dragged without a restart, and the curve
the page draws is the same math the sound gets. PipeWire empties a biquad's memory whenever its
coefficients change (bq_raw_update, 1.6), which clicked on every move: so a change goes to the
row that is silent, which settles, and then a sample-exact crossfade (a `ramp`) moves to it.
Unused slots are exact pass-through (b0 = a0 = 1). Measured: a +6 dB bell at 1 kHz gives ×1.995,
−6 dB ×0.501, pass-through is bit-exact (plan notes, M11 spike).

Like plugins, an EQ ends with PipeBoard (PR_SET_PDEATHSIG) and the board starts it again.
Its nodes are /Internal streams: apps and volume mixers never list them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import secrets
import shutil
from array import array
from collections.abc import Awaitable, Callable
from pathlib import Path

from pipeboard import devices
from pipeboard.scope import _die_with_parent

log = logging.getLogger(__name__)
# PipeWire clamps bq_raw's b0…a2 to ±10 (the controls' own range) without a word: a high shelf
# above ~+15 dB needs |b1| > 10, and the clamped filter boosted the lows by ~+28 dB (the user
# heard it). So no coefficient ever leaves ±LIMIT: a band that needs more is scaled
# down and the difference added back as plain gain in MAKEUP spare slots (exact, see slots()).
SLOTS, MAKEUP, LIMIT, MAX_BANDS = 16, 3, 8.0, 8
BAND_SLOTS = SLOTS - MAKEUP
# An EQ runs only the slots its bands need, in steps of STEP (each slot costs the same flat or not:
# measured at 192 kHz / 256, 16 slots 52 µs a cycle, 4 slots 15), and its dynamic stages only once
# a band is dynamic (4 stages: 29 µs more). More is a bigger EQ swapped in without a cut (Eqs.grow);
# ponytail: it never shrinks while running (a smaller one comes at its next start).
STEP = 4
FREQ, GAIN, Q = (20.0, 20000.0), (-18.0, 18.0), (0.1, 18.0)
TYPES = ("bell", "low-shelf", "high-shelf", "low-cut", "high-cut", "notch")
SLOPES = {12: (0.7071,), 24: (0.5412, 1.3066), 48: (0.5098, 0.6013, 0.9000, 2.5629)}  # Butterworth
FLAT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # b0 b1 b2 a0 a1 a2: pass-through
SILENT = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # a row with one of these says nothing
RUN = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "pipeboard"
# The swap: the silent row gets the new coefficients, rings out its reset for SETTLE_S, then the
# ramp fades over FADE_S, per sample (the mixer's gains step once per cycle: measured, still
# 0.05–0.1 jumps), plus LAG_S for the command to reach the audio (a cycle at 1024/48 kHz is 21 ms;
# without it the next change reset a row still faintly heard). ponytail: a 40 ms settle is plenty
# for bells and shelves above ~50 Hz; a steep cut near 20 Hz rings longer and may thump faintly.
# While dragging, the sound follows at ~8 swaps a second (the last position always lands).
ROWS, SETTLE_S, FADE_S, LAG_S = ("s", "t"), 0.04, 0.03, 0.05
GROW_SETTLE_S = 0.2  # a bigger EQ's row before it is faded in: rare, so long enough for low cuts


def new(name: str = "EQ") -> dict:
    """A flat EQ on the board: bands come from the user."""
    dev = {"id": "d" + secrets.token_hex(5), "type": "eq", "name": name, "channels": 2,
           "bands": [], "bypass": False}
    dev["node"] = f"pb_eq_{dev['id']}"
    return dev


def _num(value, lo: float, hi: float, default: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(value) else min(hi, max(lo, value))


def clean(bands) -> list[dict]:
    """Bands from the page, made safe: known types, numbers in range, no more than fit the slots.
    Frequencies are whole Hz, gains 0.1 dB, Q two decimals."""
    out, used = [], 0
    for b in bands if isinstance(bands, list) else []:
        if not isinstance(b, dict) or b.get("type") not in TYPES or len(out) == MAX_BANDS:
            continue
        band = {"type": b["type"], "freq": round(_num(b.get("freq"), *FREQ, 1000.0)),
                "gain": round(_num(b.get("gain"), *GAIN, 0.0), 1),
                "q": round(_num(b.get("q"), *Q, 1.0), 2), "on": b.get("on", True) is not False}
        if band["type"] in ("low-cut", "high-cut"):
            band["slope"] = int(b.get("slope")) if b.get("slope") in SLOPES else 12
        if used + len(_sections(band)) > BAND_SLOTS:
            continue
        used += len(_sections(band))
        out.append({**band, **_dyn_fields(b)})
    return out


def _dyn_fields(b: dict) -> dict:
    """A band's dynamic settings (M15), made safe: on or off, and its numbers in range."""
    out = {"dyn": b.get("dyn") is True}
    for name, (lo, hi, default) in DYN.items():
        out[name] = round(_num(b.get(name), lo, hi, default), 1)
    return out


def _sections(band: dict) -> tuple:
    """The Q of each biquad a band needs: one, or a Butterworth cascade for a steep cut."""
    cut = band["type"] in ("low-cut", "high-cut")
    return SLOPES[band.get("slope", 12)] if cut else (band["q"],)


def biquad(kind: str, freq: float, gain: float, q: float, rate: float) -> tuple:
    """One RBJ cookbook biquad, normalised (a0 = 1)."""
    w = 2 * math.pi * min(freq, 0.49 * rate) / rate
    c, alpha, a = math.cos(w), math.sin(w) / (2 * q), 10 ** (gain / 40)
    root = 2 * math.sqrt(a) * alpha
    coef = {
        "bell": (1 + alpha * a, -2 * c, 1 - alpha * a, 1 + alpha / a, -2 * c, 1 - alpha / a),
        "low-shelf": (a * ((a + 1) - (a - 1) * c + root), 2 * a * ((a - 1) - (a + 1) * c),
                      a * ((a + 1) - (a - 1) * c - root), (a + 1) + (a - 1) * c + root,
                      -2 * ((a - 1) + (a + 1) * c), (a + 1) + (a - 1) * c - root),
        "high-shelf": (a * ((a + 1) + (a - 1) * c + root), -2 * a * ((a - 1) + (a + 1) * c),
                       a * ((a + 1) + (a - 1) * c - root), (a + 1) - (a - 1) * c + root,
                       2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - root),
        "low-cut": ((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha),
        "high-cut": ((1 - c) / 2, 1 - c, (1 - c) / 2, 1 + alpha, -2 * c, 1 - alpha),
        "notch": (1.0, -2 * c, 1.0, 1 + alpha, -2 * c, 1 - alpha),
    }[kind]
    return tuple(v / coef[3] for v in coef[:3]) + (1.0,) + tuple(v / coef[3] for v in coef[4:])


def slots(bands: list[dict], rate: float, bypass: bool = False, size: int = SLOTS) -> list[tuple]:
    """A row's `size` slots: the bands that are on, in order, then the makeup gain, then
    pass-through."""
    return (_used(bands, rate, bypass) + [FLAT] * size)[:size]


def _used(bands: list[dict], rate: float, bypass: bool = False) -> list[tuple]:
    """The slots the bands use. A biquad whose b0…b2 reach past LIMIT is divided down (its shape
    is the same, only quieter) and the factor goes into plain-gain slots (b0 = factor), so the
    whole chain is exactly what the bands ask for and every value stays inside PipeWire's range."""
    out, makeup = [], 1.0
    for b in [] if bypass else [b for b in bands if b["on"]]:
        for q in _sections(b):
            c = biquad(b["type"], b["freq"], b["gain"], q, rate)
            k = max(1.0, max(abs(v) for v in c[:3]) / LIMIT)
            out.append((c[0] / k, c[1] / k, c[2] / k, 1.0, c[4], c[5]))
            makeup *= k
    for _ in range(MAKEUP):
        if makeup <= 1.0 + 1e-12:
            break
        step = min(makeup, LIMIT)
        out.append((step, 0.0, 0.0, 1.0, 0.0, 0.0))
        makeup /= step
    return out


def shape(dev: dict, rate: float) -> tuple[int, int]:
    """(slots in a row, dynamic stages) an EQ needs for its bands, bypassed or not: the slots in
    steps of STEP, the stages all or none."""
    used = len(_used(dev["bands"], rate))
    return (min(SLOTS, max(STEP, -(-used // STEP) * STEP)),
            DYN_SLOTS if dynamic(dev["bands"]) else 0)


def response(coefs: list[tuple], freq: float, rate: float) -> float:
    """The level in dB the slots give a tone at freq (for checks; the page has the same)."""
    z = complex(math.cos(2 * math.pi * freq / rate), -math.sin(2 * math.pi * freq / rate))
    level = 1.0
    for b0, b1, b2, a0, a1, a2 in coefs:
        level *= abs((b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z))
    return 20 * math.log10(max(level, 1e-9))  # a notch's centre is silence


def conf(dev: dict, rate: float, size: tuple[int, int] | None = None, silent: bool = False) -> str:
    """The filter-chain process's config: per channel, the input copied into two rows of raw
    biquads (both starting at the bands, or silent); out = s·(1 − fade) + t·fade, fade 0: the
    first row. `size`: (slots in a row, dynamic stages), shape() unless given."""
    n, stages = size or shape(dev, rate)
    row_slots = [SILENT] * n if silent else slots(dev["bands"], rate, dev.get("bypass"), n)
    nodes = ["{ type = builtin name = in label = copy }"]
    links = []
    for row in ROWS:
        for i, (b0, b1, b2, a0, a1, a2) in enumerate(row_slots):
            nodes.append(f"{{ type = builtin name = {row}{i} label = bq_raw config = {{ "
                         f"coefficients = [ {{ rate = {int(rate)} b0 = {b0!r} b1 = {b1!r} "
                         f"b2 = {b2!r} a0 = {a0!r} a1 = {a1!r} a2 = {a2!r} }} ] }} }}")
            before = f"{row}{i - 1}:Out" if i else "in:Out"
            links.append(f'{{ output = "{before}" input = "{row}{i}:In" }}')
        links.append(f'{{ output = "{row}{n - 1}:Out" input = "{row}x:In 1" }}')
        links.append(f'{{ output = "{row}x:Out" input = "mix:In {ROWS.index(row) + 1}" }}')
        nodes.append(f"{{ type = builtin name = {row}x label = mult }}")
    # The ramp's own controls have no range, so PipeWire clamps whatever is set on them to 0: they
    # are fed by `linear` nodes instead (a link is not clamped). One value, go:Add (the row to
    # fade to), sets Stop to it and Start to 1 − it in the same cycle: never a half-set ramp.
    lin = lambda name, mult, add: (f'{{ type = builtin name = {name} label = linear '  # noqa: E731
                                   f'control = {{ "Mult" = {mult} "Add" = {add} }} }}')
    nodes += [lin("go", 0.0, 0.0), lin("back", -1.0, 1.0), lin("len", 0.0, FADE_S),
              "{ type = builtin name = fade label = ramp }", lin("unfade", -1.0, 1.0),
              '{ type = builtin name = mix label = mixer '
              'control = { "Gain 1" = 1.0 "Gain 2" = 1.0 } }']
    links += [f'{{ output = "{a}" input = "{b}" }}' for a, b in (
        ("go:Notify", "fade:Stop"), ("go:Notify", "back:Control"), ("back:Notify", "fade:Start"),
        ("len:Notify", "fade:Duration (s)"), ("fade:Out", "unfade:In"), ("unfade:Out", "sx:In 2"),
        ("fade:Out", "tx:In 2"))]
    stages, out = dyn_stages(dev, rate, stages)
    return chain(dev, nodes + stages[0], links + stages[1], '"in:In"', f'"{out}"')


# ---- dynamic bands (M15): a band that moves with how loud it is, Pro-Q style -----------------
# After the two rows, DYN_SLOTS stages, each at rest unless a dynamic band uses it: per channel
# out = x − F(x) + P(F(x)), F the band's region (a band-pass for a bell, a low-pass for a low
# shelf, a high-pass for a high shelf: a raw biquad, set live), P LSP's Dynamics Processor with a
# curve that moves it by at most the band's Range: a cut when loud (Range < 0), a boost when quiet
# (Range > 0). At rest P is bypassed and the stage passes the sound exactly. The subtraction is
# the builtin `invert` (PipeWire's mixer clamps a negative gain to 0: it doubled the band). A glitch
# in F when its frequency moves reaches both −F and P(F): it cancels while the band is at rest.
# Measured (plan notes, M15): at rest exact to 1e-8; Range −6 holds exactly −6.0 dB; live at 192
# kHz a quiet tone −40.0 → −40.0, a loud one −10 → −16.0. A stage costs CPU even at rest (LSP
# keeps processing while bypassed): 8 doubled an EQ's (3.7 → 7.3 % of a core), so 4 stages: any 4
# bands may be dynamic, switched live. ponytail: more would mean restarting the EQ with more.
DYN_SLOTS, DYN_RATIO, DYN_TYPES = 4, 3.0, ("bell", "low-shelf", "high-shelf")
DYN = {"thr": (-60.0, 0.0, -24.0), "range": (-18.0, 18.0, -6.0), "attack": (0.1, 200.0, 10.0),
       "release": (10.0, 2000.0, 150.0)}
DP_LABEL = "http://lsp-plug.in/plugins/ladspa/dyna_processor_mono"
DP_FIXED = {"Sidechain type": 0.0, "Sidechain mode": 0.0, "Sidechain lookahead (ms)": 0.0,
            "Sidechain reactivity (ms)": 10.0, "Sidechain preamp (G)": 1.0,
            "High-pass filter mode": 0.0, "Low-pass filter mode": 0.0, "Hold time (ms)": 0.0,
            "Low-level ratio": 1.0, "High-level ratio": 1.0, "Overall makeup gain (G)": 1.0,
            "Dry gain (G)": 0.0, "Wet gain (G)": 1.0, "Dry/Wet balance (%)": 100.0,
            "Knee 0 (G)": 0.501187, "Knee 1 (G)": 0.501187, "Point enable 2": 0.0,
            "Point enable 3": 0.0}
REST = {"Bypass": 1.0}


def _g(db: float) -> float:
    return round(10 ** (db / 20), 6)


def dyn_curve(band: dict) -> dict:
    """The processor's controls for a dynamic band: two points (input level, output level) and
    unity slopes outside them. Cut: unity up to the threshold, 1/DYN_RATIO above it until it has
    moved by Range, then parallel. Boost: the mirror image under the threshold."""
    t, r = band["thr"], band["range"]
    span = abs(r) * DYN_RATIO / (DYN_RATIO - 1)
    lo, hi = ((t, t), (t + span, t + span + r)) if r < 0 else ((t - span, t - span + r), (t, t))
    return {"Bypass": 0.0, "Point enable 0": 1.0, "Threshold 0 (G)": _g(lo[0]),
            "Gain 0 (G)": _g(lo[1]), "Point enable 1": 1.0, "Threshold 1 (G)": _g(hi[0]),
            "Gain 1 (G)": _g(hi[1]), "Attack time default (ms)": band["attack"],
            "Release time default (ms)": band["release"]}


def dyn_filter(band: dict, rate: float) -> tuple:
    """The band's region, normalised (a0 = 1): an RBJ band-pass (0 dB at its centre) for a bell,
    a Butterworth low-pass or high-pass at its frequency for a shelf."""
    w = 2 * math.pi * min(band["freq"], 0.49 * rate) / rate
    c, alpha = math.cos(w), math.sin(w) / (2 * (band["q"] if band["type"] == "bell" else 0.7071))
    coef = {"bell": (alpha, 0.0, -alpha), "low-shelf": ((1 - c) / 2, 1 - c, (1 - c) / 2),
            "high-shelf": ((1 + c) / 2, -(1 + c), (1 + c) / 2)}[band["type"]]
    a0 = 1 + alpha
    return tuple(v / a0 for v in coef) + (1.0, -2 * c / a0, (1 - alpha) / a0)


def dynamic(bands: list[dict]) -> list[dict]:
    """The bands that are dynamic now, in order, at most DYN_SLOTS (each takes a stage)."""
    return [b for b in bands if b.get("dyn") and b["on"] and b["type"] in DYN_TYPES][:DYN_SLOTS]


def dyn_values(dev: dict, rate: float, n: int = DYN_SLOTS) -> list[tuple[tuple, dict]]:
    """Each of n stages' filter coefficients and processor controls: the dynamic bands' first,
    the rest at rest (pass-through filter, processor bypassed)."""
    used = [] if dev.get("bypass") else dynamic(dev["bands"])[:n]
    return [(dyn_filter(b, rate), dyn_curve(b)) for b in used] + [(FLAT, REST)] * (n - len(used))


def dyn_stages(dev: dict, rate: float, n: int = DYN_SLOTS) -> tuple[tuple[list, list], str]:
    """The stages' nodes and links after `mix`, and the node the sound leaves from. None when
    LSP's plugins are missing (the EQ works; its bands just cannot be dynamic)."""
    from pipeboard import dynamics  # noqa: PLC0415 - it builds on this module
    try:
        lsp = dynamics.plugin() if n else None
    except FileNotFoundError:
        lsp = None
    if not lsp:
        return ([], []), "mix:Out"
    nodes, links, prev = [], [], "mix:Out"
    for j, ((b0, b1, b2, a0, a1, a2), ctl) in enumerate(dyn_values(dev, rate, n)):
        values = " ".join(f'"{k}" = {v!r}' for k, v in {**DP_FIXED, **ctl}.items())
        nodes += [f"{{ type = builtin name = din{j} label = copy }}",
                  f"{{ type = builtin name = dbp{j} label = bq_raw config = {{ coefficients = [ "
                  f"{{ rate = {int(rate)} b0 = {b0!r} b1 = {b1!r} b2 = {b2!r} a0 = {a0!r} "
                  f"a1 = {a1!r} a2 = {a2!r} }} ] }} }}",
                  f"{{ type = builtin name = dneg{j} label = invert }}",
                  f'{{ type = ladspa name = ddp{j} plugin = "{lsp}" label = "{DP_LABEL}" '
                  f"control = {{ {values} }} }}",
                  f'{{ type = builtin name = dmix{j} label = mixer '
                  f'control = {{ "Gain 1" = 1.0 "Gain 2" = 1.0 "Gain 3" = 1.0 }} }}']
        links += [f'{{ output = "{a}" input = "{b}" }}' for a, b in (
            (prev, f"din{j}:In"), (f"din{j}:Out", f"dbp{j}:In"), (f"din{j}:Out", f"dmix{j}:In 1"),
            (f"dbp{j}:Out", f"dneg{j}:In"), (f"dneg{j}:Out", f"dmix{j}:In 2"),
            (f"dbp{j}:Out", f"ddp{j}:Input"), (f"ddp{j}:Output", f"dmix{j}:In 3"))]
        prev = f"dmix{j}:Out"
    return (nodes, links), prev


def _stage_params(j: int, coefs: tuple, ctl: dict) -> str:
    """`set-param` pairs for dynamic stage j: its filter's b0…a2 and its processor's controls."""
    names = ("b0", "b1", "b2", "a0", "a1", "a2")
    return " ".join([f'"dbp{j}:{n}" {v!r}' for n, v in zip(names, coefs, strict=True)]
                    + [f'"ddp{j}:{k}" {v!r}' for k, v in ctl.items()])


def tags_of(dev: dict) -> str:
    """The properties that make an effect's nodes PipeBoard's own, one card, never autoconnected."""
    return (f"node.autoconnect = false node.dont-reconnect = true pipeboard.owned = true "
            f"pipeboard.id = {dev['id']} pipeboard.kind = effect "
            f"pipeboard.board = {devices.BOARD or 'none'}")


def chain(dev: dict, nodes: list[str], links: list[str], inputs: str, outputs: str) -> str:
    """A filter-chain process's config around a graph (the EQ's, a limiter's or compressor's):
    its two streams are /Internal, PipeBoard's own, never autoconnected."""
    tags = tags_of(dev)
    return f"""context.properties = {{ log.level = 0 }}
context.spa-libs = {{ audio.convert.* = audioconvert/libspa-audioconvert
                     support.* = support/libspa-support }}
context.modules = [
  {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "{dev['name']}"
      filter.graph = {{
        nodes = [
          {(chr(10) + "          ").join(nodes)}
        ]
        links = [ {" ".join(links)} ]
        inputs = [ {inputs} ]
        outputs = [ {outputs} ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{ node.name = "{dev['node']}_in" media.class = "Stream/Input/Audio/Internal"
                        {tags} }}
      playback.props = {{ node.name = "{dev['node']}" media.class = "Stream/Output/Audio/Internal"
                         {tags} }}
    }}
  }}
]
"""


def params(coefs: list[tuple], last: list[tuple] | None, row: str = "s") -> str:
    """`set-param` Props for the slots of a row that changed: "s3:b0" 1.02 …"""
    names = ("b0", "b1", "b2", "a0", "a1", "a2")
    return " ".join(f'"{row}{i}:{n}" {v!r}' for i, c in enumerate(coefs)
                    if not last or last[i] != c for n, v in zip(names, c, strict=True))


class Eqs:
    """Starts and stops EQ (and limiter, compressor) processes; sends coefficient changes
    through one persistent pw-cli (`say`, the gain's). `live=False` (the fixture) only records
    what it would send."""

    def __init__(self, live: bool, say: Callable[[str], Awaitable[None]],
                 ended: Callable[[str, int], None]):
        self.live, self.say, self.ended = live, say, ended
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.last: dict[str, tuple] = {}  # key → (rate, [each row's slots]) last sent
        self.on: dict[str, int] = {}  # key → the row heard
        self.want: dict[str, tuple] = {}  # key → (node, rate, slots) still to swap in
        self.swaps: dict[str, asyncio.Task] = {}
        self.sent: list[tuple[str, str]] = []
        self.dyn_last: dict[str, list] = {}  # key → each dynamic stage's (filter, controls) sent
        self.unrun: dict[str, str] = {}  # key → its input node's name, until that node first runs
        self.size: dict[str, tuple[int, int]] = {}  # key → (slots in a row, stages) it runs
        self.old: dict[str, tuple] = {}  # key → the EQ a bigger one replaces (grow), its state

    def _size(self, key: str) -> tuple[int, int]:
        return self.size.get(key, (SLOTS, DYN_SLOTS))

    async def start(self, key: str, dev: dict, rate: float, silent: bool = False) -> str | None:
        """An EQ's process, or a limiter's or compressor's (the same kind, dynamics.py). An EQ
        `silent` says nothing until a row gets its bands (grow)."""
        if key in self.procs:
            return None
        if dev["type"] == "eq":
            self.size[key] = n, stages = shape(dev, rate)
            coefs = [SILENT] * n if silent else slots(dev["bands"], rate, dev.get("bypass"), n)
            self.last[key], self.on[key] = (rate, [coefs, coefs]), 0
            self.dyn_last[key] = dyn_values(dev, rate, stages)  # what the conf starts it with
            self.unrun[key] = f"{dev['node']}_in"
        if not self.live:
            return None
        from pipeboard import dynamics  # noqa: PLC0415 - it builds on this module
        try:
            RUN.mkdir(parents=True, exist_ok=True)
            path = RUN / f"{dev['type']}-{dev['id']}.conf"
            text = (conf(dev, rate, self.size[key], silent) if dev["type"] == "eq"
                    else dynamics.conf(dev))
            path.write_text(text)
            proc = await asyncio.create_subprocess_exec(
                "pipewire", "-c", str(path), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, preexec_fn=_die_with_parent,
                start_new_session=True)  # Ctrl+C in a terminal reaches PipeBoard only: the
                # sound is routed around the EQ before it stops (it ends with PipeBoard anyway)
        except OSError as e:
            return f"could not start it: {e}"
        self.procs[key] = proc
        asyncio.create_task(self._watch(key, proc))
        return None

    async def _watch(self, key: str, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if self.procs.get(key) is proc:  # it ended by itself, not by stop()
            del self.procs[key]
            self.ended(key, code)

    async def set(self, key: str, dev: dict, node: int, rate: float) -> None:
        """Move the EQ to dev's bands, by a swap of rows; changes while one runs: the last wins.
        Its dynamic stages change at once (a glitch there cancels while a band is at rest)."""
        await self._dyn(key, dev, node, rate)
        coefs = slots(dev["bands"], rate, dev.get("bypass"), self._size(key)[0])
        self.want[key] = (node, rate, coefs)
        if key not in self.swaps or self.swaps[key].done():
            self.swaps[key] = asyncio.create_task(self._swaps(key))

    async def _swaps(self, key: str) -> None:
        try:
            while key in self.want:
                await self._swap(key, *self.want.pop(key))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an EQ that fails to move must not take the app down
            log.exception("eq: could not set the bands")

    async def _swap(self, key: str, node: int, rate: float, coefs: list[tuple]) -> None:
        """The silent row gets the slots that differ, settles, and is faded in."""
        was_rate, rows = self.last.get(key, (None, [None, None]))
        if was_rate != rate:
            rows = [None, None]  # the graph's rate changed: neither row is right any more
        on = self.on.get(key, 0)
        if rows[on] == coefs:
            return
        idle = 1 - on
        line = params(coefs, rows[idle], ROWS[idle])
        rows[idle] = coefs
        self.last[key] = (rate, rows)
        if line:
            self.sent.append((key, line))
            await self.say(f"set-param {node} Props {{ params = [ {line} ] }}")
            await asyncio.sleep(SETTLE_S)  # unchanged, the row has been running warm all along
        await self.say(f'set-param {node} Props {{ params = [ "go:Add" {float(idle)} ] }}')
        await asyncio.sleep(FADE_S + LAG_S)
        self.on[key] = idle

    def ran(self, running: set[str]) -> None:
        """Node names now running. Filter coefficients set before an EQ's node first runs are
        lost: PipeWire's raw biquads start from the config's then (measured live, plan notes M25;
        a plugin's controls keep theirs). So once it runs, forget what was sent: the next set()
        sends everything again (both rows, every dynamic stage)."""
        for key, name in list(self.unrun.items()):
            if name in running:
                del self.unrun[key]
                self.last[key], self.dyn_last[key] = (None, [None, None]), None

    def heard(self, key: str) -> list[tuple] | None:
        """The slots of the row playing now (for checks)."""
        return self.last[key][1][self.on[key]] if key in self.last else None

    async def _dyn(self, key: str, dev: dict, node: int, rate: float) -> None:
        """The dynamic stages that differ from what was last sent: their filter and controls."""
        n = self._size(key)[1]
        now, before = dyn_values(dev, rate, n), self.dyn_last.get(key) or [None] * n
        line = " ".join(_stage_params(j, *stage) for j, stage in enumerate(now)
                        if stage != before[j])
        self.dyn_last[key] = now
        if line:
            self.sent.append((key, line))
            await self.say(f"set-param {node} Props {{ params = [ {line} ] }}")

    def fits(self, key: str, dev: dict, rate: float) -> bool:
        """Whether the running EQ has the slots and stages dev's bands need."""
        (n, stages), (has, has_stages) = shape(dev, rate), self._size(key)
        return n <= has and stages <= has_stages

    async def grow(self, key: str, dev: dict, rate: float, silent: bool) -> bool:
        """A bigger EQ for dev's bands, started beside the running one, which plays on: the caller
        wires it like the old one, handover() fades from one to the other, finish() stops the one
        not kept. `silent`: it starts saying nothing (the old one is heard). False: it did not
        start, and the old one is kept."""
        if (swap := self.swaps.get(key)) and not swap.done():
            with contextlib.suppress(Exception):
                await swap  # the old one's own swap lands first: handover() fades from its row
        self.old[key] = (self.procs.pop(key, None), self.on.get(key, 0), self._size(key),
                         self.last.get(key), self.dyn_last.get(key), self.unrun.get(key))
        if error := await self.start(key, dev, rate, silent):
            log.warning("eq: a bigger EQ did not start: %s", error)
            await self.finish(key, False)
            return False
        return True

    async def handover(self, key: str, old_node: int, node: int, dev: dict, rate: float) -> None:
        """Both running, both wired, the new one silent: its second row gets dev's bands and
        settles, the old one's silent row goes to zero, and both fade at once, the old one out and
        the new one in. Two processes doing the same sound: no cut (a tone through it moved under
        0.03 dB, plan notes)."""
        _, on, (n_old, _), *_ = self.old[key]
        n = self._size(key)[0]
        coefs = slots(dev["bands"], rate, dev.get("bypass"), n)
        line = params(coefs, None, ROWS[1])
        self.sent.append((key, line))
        await self.say(f"set-param {node} Props {{ params = [ {line} ] }}")
        await asyncio.sleep(GROW_SETTLE_S)
        quiet = params([SILENT] * n_old, None, ROWS[1 - on])
        await self.say(f"set-param {old_node} Props {{ params = [ {quiet} ] }}")
        await self.say(f'set-param {old_node} Props {{ params = [ "go:Add" {float(1 - on)} ] }}')
        await self.say(f'set-param {node} Props {{ params = [ "go:Add" 1.0 ] }}')
        await asyncio.sleep(FADE_S + LAG_S)
        self.last[key], self.on[key] = (rate, [[SILENT] * n, coefs]), 1
        self.unrun.pop(key, None)  # it runs: what was sent stays

    async def finish(self, key: str, keep: bool) -> None:
        """After grow(): stop the old EQ (keep the new one), or the new one (back to the old)."""
        old, *state = self.old.pop(key, (None,))
        if keep:
            await _end(old)
            return
        new = self.procs.pop(key, None)
        if old:
            self.procs[key] = old
        self.on[key], self.size[key], self.last[key], self.dyn_last[key], unrun = state
        self.unrun.pop(key, None)
        if unrun:
            self.unrun[key] = unrun
        await _end(new)

    async def stop(self, key: str) -> None:
        proc = self.procs.pop(key, None)
        self.last.pop(key, None)
        self.dyn_last.pop(key, None)
        self.unrun.pop(key, None)
        self.want.pop(key, None)
        self.size.pop(key, None)
        if swap := self.swaps.pop(key, None):
            swap.cancel()
        await _end(self.old.pop(key, (None,))[0])
        await _end(proc)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(k) for k in list(self.procs)))


async def _end(proc: asyncio.subprocess.Process | None) -> None:
    """A process stopped: asked, then made to."""
    if not proc:
        return
    for end in (proc.terminate, proc.kill):
        with contextlib.suppress(ProcessLookupError):
            end()
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), 3)
            return
        except TimeoutError:
            continue


SPECTRUM_RATE = 48000  # enough for 20 Hz–20 kHz; the page's FFT does the rest


class Spectrum:
    """While an EQ's panel is open: its input and its output, each summed to one channel, at
    48 kHz, raw floats, for the page's FFTs. A limiter's or compressor's panel: what goes in and
    comes out, on four channels (dynamics.capture). Under the live wires' rules (PLAN §8a): an
    async node (the audio never waits for it), /Internal (no app lists it), only while a window
    shows the panel, and nothing here may raise (a dead capture is an empty spectrum, nothing
    else).
    `ports`: (port id, the capture's channel it goes into)."""

    def __init__(self, key: str, ports: list[tuple[int, str]], channels: tuple = ("MONO",)):
        self.key, self.ports, self.channels = key, ports, channels
        # pipeboard_scope*: the board never shows it (graph._plumbing)
        self.name = f"pipeboard_scope_eq_{secrets.token_hex(3)}"
        self.buf = bytearray()
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            unbuffered = ["stdbuf", "-o0"] if shutil.which("stdbuf") else []
            self.proc = await asyncio.create_subprocess_exec(
                *unbuffered, "pw-cat", "--record", "-", "--raw", "--rate", str(SPECTRUM_RATE),
                "--format", "f32", "--channels", str(len(self.channels)),
                "--channel-map", ",".join(self.channels), "--latency", "20ms",
                "-P", f"{{ node.name={self.name} node.description=\"PipeBoard EQ spectrum\" "
                      "media.class=Stream/Input/Audio/Internal node.virtual=true "
                      "node.autoconnect=false node.passive=true node.async=true }",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_die_with_parent)
            self.task = asyncio.create_task(self._run())
        except Exception:  # noqa: BLE001 - no spectrum is no reason to stop anything
            log.exception("eq: the spectrum capture did not start")

    async def _run(self) -> None:
        try:
            for pid, ch in self.ports:  # two into one input: PipeWire sums them
                for _ in range(20):
                    link = await asyncio.create_subprocess_exec(
                        "pw-link", str(pid), f"{self.name}:input_{ch}",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    if await link.wait() == 0:
                        break
                    await asyncio.sleep(0.05)
            while chunk := await self.proc.stdout.read(65536):
                self.buf += chunk
                del self.buf[:-SPECTRUM_RATE * 4 * len(self.channels)]  # a second at most
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("eq: the spectrum capture failed")

    def take(self, unit: int = 4) -> bytes:
        """The audio since the last call, in whole units (bytes: a sample, or a dynamics block)."""
        n = len(self.buf) - len(self.buf) % unit
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        with contextlib.suppress(Exception):
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
            self.proc._transport.close()


class FakeSpectrum(Spectrum):
    """For --fixture: made-up sound (a plucked low string and some hiss), no PipeWire."""

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        loop, made = asyncio.get_running_loop(), 0
        start = loop.time()
        while True:
            due = int((loop.time() - start) * SPECTRUM_RATE)
            out = array("f")
            for n in range(made, due):
                t = n / SPECTRUM_RATE
                pluck = math.exp(-((t % 1.6) * 2.2))
                tone = sum(math.sin(2 * math.pi * 110 * k * t) / k for k in (1, 2, 3, 5))
                hiss = (((n * 1103515245 + 12345) >> 8) % 2001 - 1000) / 1000
                v = 0.2 * pluck * tone + 0.004 * hiss
                # A limiter's: in loud (×4), out held under −1 dB (a stand-in for its own sound).
                held = max(-0.89, min(0.89, 4 * v))
                out.extend({1: [v], 2: [v, v]}.get(len(self.channels), [4 * v, 4 * v, held, held]))
            self.buf += out.tobytes()
            del self.buf[:-SPECTRUM_RATE * 4 * len(self.channels)]
            made = due
            await asyncio.sleep(0.04)
