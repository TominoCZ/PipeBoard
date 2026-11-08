"""Render the board in headless Chrome, scenario by scenario, and audit the layout.

    ./run.sh shots                 every shot -> .dev/shots/<name>.png and .dev/shots/sheet.png
    ./run.sh shots board menu      only these
    ./run.sh shots eq              a group: eq and every eq-… shot, and .dev/shots/eq-sheet.png
    ./run.sh shots --list          what there is
    ./run.sh shots icon            the app and tray icons at every size -> .dev/shots/icon.png

No server and no PipeWire: each page is index.html with the scenario's model injected as
window.PB_BOOT, plus a line of JS ("after") that puts the UI in a state (a menu open, a wire
being dragged...). The page audits itself (cards overlapping, text clipped, things off screen,
console errors) and the faults are printed; the exit code is the number of faulty shots.
Look at the PNGs after every visual change.
"""

import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "fixtures")]
import scenarios  # noqa: E402

import asyncio  # noqa: E402
import tempfile  # noqa: E402

from pipeboard import dynamics, eq, graph, pipewire, plugins, profiles, server, workspace  # noqa: E402

WEB = ROOT / "pipeboard" / "web"
OUT = ROOT / ".dev" / "shots"
BIG, SMALL = (1600, 1000), (1100, 700)
UR = "device:alsa_card.usb-Example_Studio_Interface-00"
PEDAL = "device:alsa_card.usb-Example_Guitar_Pedal-00"


# Setups write a config folder before the real server reads it, and may change the graph it sees.
def renamed(folder: Path, dump: list) -> list:
    workspace.save(folder / "workspace.json", {"cards": {
        "mic_mix": {"nick": "Stream Mic", "names": ["Podcast", "Discord Mic"]},
        UR: {"nick": "Desk Interface"}}})
    return dump


def _studio(folder: Path, saved_from: list, board: dict | None = None) -> None:
    store = workspace.Store(folder)
    store.write("Default", profiles.snapshot(graph.build(scenarios.load("machine")), {"cards": {}}))
    store.write("Studio", profiles.snapshot(graph.build(saved_from), {"cards": {}}))
    store.save_settings({"active": "Studio", "launch": "Studio"})
    workspace.save(store.board, board or store.read("Studio"))


def trying(folder: Path, dump: list) -> list:
    """A block of 256 and a margin of 256 on trial, 47 s left of its minute."""
    import time
    workspace.Store(folder).save_settings({"engine_trial": {
        "values": {"block": 256, "margin": 256, "prio": 88, "nice": -11, "tsched": True},
        "was": {"block": 256, "margin": 256, "prio": 88, "nice": -11, "tsched": False}, "before": {},
        "until": time.time() + 47}})
    return dump


def missing(folder: Path, dump: list) -> list:
    """Studio was saved with the Pedal plugged in; today it is not."""
    _studio(folder, scenarios.load("pedal"))
    return dump


def changed(folder: Path, dump: list) -> list:
    """Studio is active; since saving, Music Player was unplugged from Desktop Mix and Mic Mix renamed."""
    _studio(folder, dump)
    store = workspace.Store(folder)
    board = store.read("Studio")
    board["cards"]["mic_mix"]["nick"] = "Stream Mic"
    workspace.save(store.board, board)
    gone = {w["id"]: w for w in graph.build(dump)["wires"]}["music_player:out>desktop_mix:in"]["links"]
    return [o for o in dump if o["id"] not in gone]


def owned(folder: Path, dump: list) -> list:
    """A board with one of PipeBoard's own virtual mics on it, wired from Studio Interface jack 2."""
    from pipeboard import devices
    fixture = pipewire.Fixture(dump)
    dev = {"id": "dshot01", "type": "mic", "name": "Stream Mic", "node": "stream_mic", "channels": 2}
    asyncio.run(fixture.create(dev))
    workspace.save(folder / "workspace.json", {"cards": {"pb:dshot01": {"names": ["Podcast Mic"]}},
                                               "devices": {"pb:dshot01": dev}})
    assert devices.valid_name(dev["name"])
    return fixture.dump


def gained(folder: Path, dump: list) -> list:
    """A gain at +3.5 dB between Studio Interface jack 2 and Mic Mix."""
    fixture = pipewire.Fixture(dump)
    dev = {"id": "dgain01", "type": "gain", "name": "Gain", "node": "gain", "channels": 2,
           "gain_db": 3.5}
    asyncio.run(fixture.create(dev))
    workspace.save(folder / "workspace.json", {"cards": {}, "devices": {"pb:dgain01": dev}})
    model = graph.build(fixture.dump)
    card = next(c for c in model["cards"] if c["key"] == "pb:dgain01")
    ur = next(c for c in model["cards"] if c["key"].startswith("device:"))
    voice = next(c for c in model["cards"] if c["key"] == "mic_mix")
    out2 = next(s for s in ur["outputs"] if s["id"] == "out2")["ports"][0]
    direct = next(w for w in model["wires"] if w["id"] == f"{ur['key']}:out2>mic_mix:in")
    for link in direct["links"]:  # the gain goes in between, not beside
        asyncio.run(fixture.unlink(link))
    for i, port in enumerate(card["inputs"][0]["ports"]):
        asyncio.run(fixture.link(out2, port))
        asyncio.run(fixture.link(card["outputs"][0]["ports"][i], voice["inputs"][0]["ports"][i]))
    return fixture.dump


def plugged(folder: Path, dump: list) -> list:
    """Hum Remover between Studio Interface jack 3 and Instrument Mix, and an LSP compressor, bypassed,
    beside it."""
    fixture = pipewire.Fixture(dump)
    hum, comp = (dict(plugins.new(e), id=i) for e, i in (
        (plugins.FAKE_INSTALLED[3], "dhum01"), (plugins.FAKE_INSTALLED[0], "dcomp01")))
    for dev, params in ((hum, plugins.FAKE_PARAMS["Hum Remover"]), (comp, plugins.FAKE_PARAMS[""])):
        dev.update(params=[{**p, "value": p["def"]} for p in params], editor=True,
                   bypassable=True, node=f"pb-{dev['id']}")
        asyncio.run(fixture.create(dev))
    comp["bypass"] = True
    devices = {f"pb:{d['id']}": d for d in (hum, comp)}
    workspace.save(folder / "workspace.json", {"cards": {}, "devices": devices})
    model = graph.build(fixture.dump)
    card = next(c for c in model["cards"] if c["key"] == "pb:dhum01")
    guitar = next(c for c in model["cards"] if c["key"] == "instrument_mix")
    direct = next(w for w in model["wires"] if w["id"] == f"{UR}:out3>instrument_mix:in")
    jack = next(c for c in model["cards"] if c["key"] == UR)
    out3 = next(s for s in jack["outputs"] if s["id"] == "out3")["ports"][0]
    for link in direct["links"]:
        asyncio.run(fixture.unlink(link))
    for port in card["inputs"][0]["ports"]:  # the guitar into both of its inputs
        asyncio.run(fixture.link(out3, port))
    for i, port in enumerate(card["outputs"][0]["ports"]):
        asyncio.run(fixture.link(port, guitar["inputs"][0]["ports"][i]))
    return fixture.dump


def equalized(folder: Path, dump: list) -> list:
    """Voice EQ between Music Player and Desktop Mix (a low cut, a dip, a presence lift, air), and a
    new, flat EQ beside it."""
    fixture = pipewire.Fixture(dump)
    voice = {**eq.new("Voice EQ"), "id": "deq01", "node": "pb_eq_deq01", "bands": eq.clean([
        {"type": "low-cut", "freq": 80, "slope": 24}, {"type": "bell", "freq": 250, "gain": -3.5, "q": 1.4},
        {"type": "bell", "freq": 3200, "gain": 4, "q": 0.9}, {"type": "high-shelf", "freq": 9000, "gain": 2.5, "q": 0.7}])}
    flat = {**eq.new("EQ"), "id": "deq02", "node": "pb_eq_deq02"}
    lim = {**dynamics.new("limiter"), "id": "dlim01", "node": "pb_lim_dlim01", "ceiling_db": -1.0}
    comp = {**dynamics.new("compressor"), "id": "dcomp01", "node": "pb_comp_dcomp01",
            "threshold_db": -12.0, "makeup_db": 4.0}
    gate = {**dynamics.new("gate"), "id": "dgate01", "node": "pb_gate_dgate01"}
    ns = {**dynamics.new("denoise"), "id": "dns01", "node": "pb_ns_dns01"}
    ds = {**dynamics.new("deesser"), "id": "dds01", "node": "pb_ds_dds01"}
    for dev in (voice, flat, lim, comp, gate, ns, ds):
        asyncio.run(fixture.create(dev))
    workspace.save(folder / "workspace.json", {"cards": {}, "devices": {
        "pb:deq01": voice, "pb:deq02": flat, "pb:dlim01": lim, "pb:dcomp01": comp,
        "pb:dgate01": gate, "pb:dns01": ns, "pb:dds01": ds}})
    model = graph.build(fixture.dump)
    card = next(c for c in model["cards"] if c["key"] == "pb:deq01")
    music_player = next(c for c in model["cards"] if c["key"] == "music_player")
    mix = next(c for c in model["cards"] if c["key"] == "desktop_mix")
    for link in next(w for w in model["wires"] if w["id"] == "music_player:out>desktop_mix:in")["links"]:
        asyncio.run(fixture.unlink(link))
    for i in (0, 1):
        asyncio.run(fixture.link(music_player["outputs"][0]["ports"][i], card["inputs"][0]["ports"][i]))
        asyncio.run(fixture.link(card["outputs"][0]["ports"][i], mix["inputs"][0]["ports"][i]))
    return fixture.dump


def diagnosed(folder: Path, dump: list) -> list:
    """A Waveform and a Stereo card, each wired from Music Player beside its usual path (the
    shots feed them made-up sound: PB.debug.fakeDiag)."""
    fixture = pipewire.Fixture(dump)
    wave = {"id": "dwave01", "type": "waveform", "name": "Waveform", "node": "pb_wave", "channels": 2}
    ster = {"id": "dster01", "type": "stereo", "name": "Stereo", "node": "pb_stereo", "channels": 2}
    import time
    now = round(time.time())
    count = {"id": "dcount01", "type": "counter", "name": "Counter", "node": "pb_counter", "channels": 2,
             "counts": {"drops": 0, "clips": 3, "since": now - 1800, "last_drop": None, "last_clip": now - 300}}
    met = {"id": "dmeter01", "type": "meter", "name": "Meter", "node": "pb_meter", "channels": 2}
    spec = {"id": "dspec01", "type": "spectrum", "name": "Spectrum", "node": "pb_spectrum", "channels": 2}
    for dev in (wave, ster, count, met, spec):
        asyncio.run(fixture.create(dev))
    workspace.save(folder / "workspace.json", {"cards": {}, "devices": {"pb:dwave01": wave, "pb:dster01": ster,
                                                                     "pb:dcount01": count, "pb:dmeter01": met,
                                                                     "pb:dspec01": spec}})
    model = graph.build(fixture.dump)
    cards = {c["key"]: c for c in model["cards"]}
    for key in ("pb:dwave01", "pb:dster01", "pb:dcount01", "pb:dmeter01", "pb:dspec01"):
        for i in (0, 1):
            asyncio.run(fixture.link(cards["music_player"]["outputs"][0]["ports"][i], cards[key]["inputs"][0]["ports"][i]))
    return fixture.dump


def _frame() -> str:
    """A frame from the capture's own encoder: a bass on source 0, a hi-hat on source 1."""
    import base64
    import math
    from array import array
    from collections import deque

    from pipeboard import scope

    rings, energy, tones = [], [], []
    for hz in (70, 3500):
        s = array("f", [0.5 * math.sin(2 * math.pi * hz * k / scope.RATE) for k in range(320)])
        rings.append(deque(scope.peaks(s, 1)[0], maxlen=scope.WINDOW))
        energy.append(deque(scope.bands(s, 1, [[0.0, 0.0]])[0], maxlen=scope.WINDOW))
        tones.append(scope.bright(list(energy[-1])[-40:]))
    frame, _ = scope.encode(rings, [[0], [1]], 40, 1000, energy)
    return f"PB.debug.frameCheck('{base64.b64encode(frame).decode()}', {tones})"


def chained(folder: Path, dump: list) -> list:
    """The equalized board with its effects in one long chain, Music Player → Voice EQ → Limiter →
    Compressor → Noise Gate → De-Esser → Noise Suppression → Desktop Mix: Tidy folds it into rows."""
    dump = equalized(folder, dump)
    fixture = pipewire.Fixture(dump)
    cards = {c["key"]: c for c in graph.build(fixture.dump)["cards"]}
    wire = next(w for w in graph.build(fixture.dump)["wires"] if w["id"] == "pb:deq01:out>desktop_mix:in")
    for link in wire["links"]:
        asyncio.run(fixture.unlink(link))
    chain = ["pb:deq01", "pb:dlim01", "pb:dcomp01", "pb:dgate01", "pb:dds01", "pb:dns01", "desktop_mix"]
    for a, b in zip(chain, chain[1:]):
        for i in (0, 1):
            asyncio.run(fixture.link(cards[a]["outputs"][0]["ports"][i], cards[b]["inputs"][0]["ports"][i]))
    ws = workspace.load(folder / "workspace.json")  # Portals on: the shots on this board fold it
    workspace.save(folder / "workspace.json", {**ws, "tidy_portals": True})
    return fixture.dump


def left_only(folder: Path, dump: list) -> list:
    """Music Player's left channel alone into the Studio Interface's mono IN 3 (a stereo output split by hand)."""
    fixture = pipewire.Fixture(dump)
    model = graph.build(fixture.dump)
    music_player = next(c for c in model["cards"] if c["key"] == "music_player")
    jack = next(c for c in model["cards"] if c["key"] == UR)
    asyncio.run(fixture.link(music_player["outputs"][0]["ports"][0],
                             next(s for s in jack["inputs"] if s["id"] == "in3")["ports"][0]))
    return fixture.dump


PLAN = ("{name: 'Streaming', make: ['Music Player → Music Mix', 'Studio Interface OUT 2 → Mic Instrument Mix'],"
        " remove: ['Music Player → Desktop Mix'], missing: ['Pedal']}")

# The picker's list: the fixture's four, and a long tail like the user's 315.
LISTED = json.dumps(plugins.FAKE_INSTALLED + [
    {"format": "lv2", "name": f"LSP {n}", "maker": "LSP LV2", "ins": 2, "outs": 2, "label": f"urn:lsp:{i}",
     "params": 20} for i, n in enumerate(["Compressor Mono", "Dynamic Processor Stereo", "Expander Stereo",
                                          "Gate Stereo", "Graphic Equalizer x16 Stereo", "Limiter Stereo",
                                          "Multiband Compressor x8 Stereo", "Sidechain Compressor Stereo"])])

# name: (scenario, size, setup, js run after the board is drawn)
SHOTS = {
    "board": ("machine", BIG, None, ""),
    "small": ("machine", SMALL, None, ""),
    "pedal": ("pedal", BIG, None, ""),
    "problems": ("problems", BIG, None, "PB.debug.openProblems()"),
    "problems-away": ("problems", BIG, None, "PB.debug.problemsAway()"),
    "connecting": ("machine", BIG, None, "PB.debug.pending('music_player', 'out', 700, 640)"),
    "connecting-mono": ("machine", BIG, None, f"PB.debug.dragTo('music_player', 'out', '{UR}', 'in3')"),
    "connecting-held": ("machine", BIG, None, "PB.debug.dragFree('music_player', 'out', 760, 640)"),
    "connecting-stereo": ("machine", BIG, None, "PB.debug.dragTo('music_player', 'out', 'mic_mix', 'in')"),
    "selected": ("machine", BIG, None, "PB.debug.select('mic_mix:out>desktop_mix:in') || "
                 "PB.debug.select(PB.debug.wires()[1].id)"),
    "menu": ("machine", BIG, None, "PB.debug.menu(560, 300, ['Add node', 'Devices', 'Virtual'])"),
    "tidy": ("machine", BIG, None, "PB.debug.tidy()"),
    "tidy-fold": ("machine", BIG, chained, "PB.debug.tidy()"),
    "tidy-echo": ("machine", BIG, chained, "PB.debug.tidyEcho()"),
    "tidy-zoom": ("machine", BIG, chained, "PB.debug.tidyZoom()"),
    "tidy-no-portals": ("machine", BIG, chained, "PB.debug.noPortals() || PB.debug.tidy()"),
    "tidy-any-zoom": ("machine", BIG, chained, "PB.debug.tidyAnyZoom()"),
    "portal-follow": ("machine", BIG, chained, "PB.debug.portalFollow('pb:dds01')"),
    "portals-off": ("machine", BIG, chained, "PB.debug.portalsOff()"),
    "tidy-wrap": ("machine", BIG, diagnosed, "PB.debug.tidyWrap(['pb:dwave01', 'pb:dster01', 'pb:dcount01', 'pb:dmeter01', 'pb:dspec01'])"),
    "portal-hover": ("machine", BIG, chained, "PB.debug.tidy() || PB.debug.portalHover(0)"),
    "latency": ("machine", BIG, chained, "PB.debug.tidy() || PB.debug.showLatency()"),
    "latency-near": ("machine", BIG, chained, "PB.debug.tidy() || PB.debug.dynFlag('pb:dcomp01', 'bypass') || "
                     "PB.debug.showLatency() || PB.debug.zoom(0.9)"),
    "latency-plugin": ("machine", BIG, plugged, "PB.debug.showLatency()"),
    "latency-path": ("machine", BIG, chained, "PB.debug.tidy() || PB.debug.showLatency() || PB.debug.zoom(0.9) || "
                     "PB.debug.openPath('desktop_mix')"),
    "latency-path-hw": ("machine", BIG, None, "PB.debug.showLatency() || PB.debug.openPath('mic_mix')"),
    "latency-path-hover": ("machine", BIG, chained, "PB.debug.tidy() || PB.debug.showLatency() || PB.debug.zoom(0.9) || "
                           "PB.debug.openPath('desktop_mix') || PB.debug.pathHover(0)"),
    "latency-path-stays": ("machine", BIG, None, "PB.debug.showLatency() || PB.debug.openPath('mic_mix') || "
                           "PB.debug.pathStays()"),
    "latency-path-drops": ("machine", BIG, None, "PB.debug.showLatency() || PB.debug.openPath('mic_mix') || "
                           f"PB.debug.pathDrops({{'{UR}': 3, mic_mix: 0}})"),
    "latency-path-away": ("machine", BIG, None, "PB.debug.showLatency() || PB.debug.openPath('mic_mix') || "
                          "PB.debug.clickBoard(760, 820)"),
    "engine": ("machine", BIG, None, "PB.debug.engineOpen({block: 256, margin: 256})"),
    "engine-advanced": ("machine", BIG, None, "PB.debug.engineOpen({block: 256, margin: 256, prio: 90, tsched: true}, true)"),
    "engine-kernel": ("machine", BIG, None, "PB.debug.engineOpen({}, true) || PB.debug.fakeHeadroom('ok', {kernel: {grub: true, opts: ["
                      "{name: 'preempt=full', now: false, ours: true, elsewhere: []}, "
                      "{name: 'rcu_nocbs=all', now: true, ours: false, elsewhere: ['/etc/default/grub']}]}})"),
    "engine-same": ("machine", BIG, None, "PB.debug.engineOpen()"),
    "engine-headroom": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom()"),
    "engine-headroom-close": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('close')"),
    # The output running dry (below 0: frames short), the owner's at 1 ms blocks with the window hidden.
    "engine-headroom-dry": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('dry')"),
    "engine-headroom-wait": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('wait')"),
    "engine-apps": ("machine", BIG, None, "PB.debug.engineOpen({}, true) || PB.debug.fakeHeadroom('ok', {app_rt: {on: true, apps: [{name: 'Firefox', state: 'own'}, {name: 'PipeWire ALSA [fluidsynth]', state: 'raised'}, {name: 'Spotify', state: 'raised'}]}})"),
    "engine-usb": ("machine", BIG, None, "PB.debug.engineOpen() || PB.debug.fakeHeadroom('ok', {blocks: [192, 384, 768, 1536, 3072], usb_step: 192, block: 256, margin: 512, margin_blocks: 2})"),
    "engine-headroom-128": ("machine", BIG, None, "PB.debug.fakeHeadroom('ok', {block: 128, margin: 128, margin_blocks: 1}) || PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('saw')"),
    "engine-headroom-idle": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('idle')"),
    "engine-headroom-input": ("machine", BIG, None, "PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('input')"),
    # Two interfaces (the pedal beside the Studio Interface): the Audio engine lists them; the
    # pedal's own panel, docked, with its rates and 96 kHz picked against the auto it runs at.
    "engine-two": ("pedal", BIG, None, "PB.debug.engineOpen()"),
    "engine-iface": ("pedal", BIG, None, "PB.debug.fakeHeadroom('ok', {device: 'alsa_card.usb-Example_Guitar_Pedal-00', title: 'Pedal', "
                     "dev_rate: 48000, rates: [44100, 48000, 96000, 192000], fixed_rate: null}) || PB.debug.ifaceOpen('alsa_card.usb-Example_Guitar_Pedal-00', {rate: 96000}) "
                     "|| PB.debug.fakeHeadroom('input')"),
    "engine-iface-small": ("pedal", SMALL, None, "PB.debug.fakeHeadroom('ok', {rates: [44100, 48000, 96000, 192000], fixed_rate: 48000}) "
                           "|| PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('ok')"),
    # Four interfaces, this machine's real names (long ones: does the list fit?).
    "engine-many": ("machine", BIG, None, "PB.debug.engineOpen() || PB.debug.fakeHeadroom('ok', {interfaces: ["
                    "{key: 'ur', title: 'Steinberg UR44C', rate: 192000, clocks: true, tsched: false, margin_ms: 1.0, fixed_rate: 192000}, "
                    "{key: 'hda', title: 'Family 17h/19h HD Audio Controller', rate: 48000, clocks: false, tsched: true, margin_ms: 10.7}, "
                    "{key: 'cam', title: 'HD Pro Webcam C920', rate: null, clocks: false}, "
                    "{key: 'nv', title: 'GA102 High Definition Audio Controller', rate: null, clocks: false}], device: 'ur'}) "
                    "|| PB.debug.engineOpen({block: 512})"),
    # The widest rate row: the UR44C's six rates and Auto (its value was cut off at the edge).
    "engine-rates-many": ("machine", BIG, None, "PB.debug.fakeHeadroom('ok', {rates: [44100, 48000, 88200, 96000, 176400, 192000], "
                          "fixed_rate: 176400}) || PB.debug.ifaceOpen() || PB.debug.fakeHeadroom('ok')"),
    "engine-away": ("machine", BIG, None, "PB.debug.engineAway()"),
    "engine-one-popover": ("machine", BIG, None, "PB.debug.onePopover()"),
    "engine-iface-away": ("machine", BIG, None, "PB.debug.ifaceAway()"),
    "engine-margin": ("machine", BIG, None, "PB.debug.ifaceOpen(null, {blocks: 2})"),
    # PipeWire running 88 and -11 while its file asks 95 and -19 (the owner's, 2026-11-08).
    "engine-priority-pending": ("machine", BIG, None, "PB.debug.fakeHeadroom('ok', {prio: 95, nice: -19, prio_now: 88, nice_now: -11}) || PB.debug.engineOpen({prio: 95, nice: -19}, true)"),
    # Advanced closed, its summary at its longest: idle states off, a kernel option waiting, apps.
    "engine-adv-summary": ("machine", BIG, None, "PB.debug.fakeHeadroom('ok', {cstates: {states: [{name: 'C1', us: 1, off: false}, {name: 'C2', us: 18, off: true}, {name: 'C3', us: 350, off: true}], rules: []}, "
                           "kernel: {grub: true, opts: [{name: 'preempt=full', now: false, ours: true, elsewhere: []}, {name: 'rcu_nocbs=all', now: false, ours: false, elsewhere: []}]}, prio_now: 88, prio: 95}) || PB.debug.engineOpen({prio: 95})"),
    "engine-priority-pending-shut": ("machine", BIG, None, "PB.debug.fakeHeadroom('ok', {prio: 95, nice: -19, prio_now: 88, nice_now: -11}) || PB.debug.engineOpen({prio: 95, nice: -19})"),
    "engine-priority": ("machine", BIG, None, "PB.debug.engineOpen({prio: 90}, true)"),
    "engine-small": ("machine", SMALL, None, "PB.debug.engineOpen({block: 512}, true)"),
    "engine-trial": ("machine", BIG, trying, ""),
    "engine-trial-stays": ("machine", BIG, trying, "PB.debug.trialStays()"),
    "engine-starting": ("machine", BIG, trying, "PB.debug.starting('sound')"),
    "engine-starting-back": ("machine", BIG, trying, "PB.debug.starting(null, 13)"),
    "engine-starting-revert": ("machine", BIG, None, "PB.debug.starting('wires')"),
    "portal-toggle": ("machine", BIG, None, "PB.debug.portalToggle()"),
    "appear-free": ("machine", BIG, None, "PB.debug.appearOver('music_player', 'mic_mix')"),
    "appear-back": ("machine", BIG, None, "PB.debug.comeBackOver('music_player', 'mic_mix')"),
    "drop-over": ("machine", BIG, equalized, "PB.debug.dropOver('pb:deq02', 'desktop_mix')"),
    "drop-stale": ("machine", BIG, None, "PB.debug.staleAfterDrop('music_player')"),
    "menu-edge": ("machine", BIG, None, "PB.debug.menu(1380, 760, ['Add node', 'Effects'])"),
    "card-menu": ("machine", BIG, renamed, "PB.debug.cardMenu('mic_mix')"),
    "renaming": ("machine", BIG, renamed, "PB.debug.rename('instrument_mix')"),
    "renaming-own": ("machine", BIG, owned, "PB.debug.rename('pb:dshot01')"),
    "create-device": ("machine", BIG, None, "PB.debug.createDevice('mic')"),
    "own-menu": ("machine", BIG, owned, "PB.debug.cardMenu('pb:dshot01')"),
    "delete-device": ("machine", SMALL, owned, "PB.debug.deleteDevice('pb:dshot01')"),
    "gain": ("machine", BIG, gained, ""),
    "gain-live": ("machine", BIG, gained, "PB.debug.fakeLive()"),
    "gain-held": ("machine", BIG, gained, "PB.debug.fieldHold('pb:dgain01')"),
    "plugin": ("machine", BIG, plugged, ""),
    "plugin-editor": ("machine", BIG, plugged, "PB.debug.editorOpen('pb:dhum01')"),
    "plugin-menu": ("machine", BIG, plugged, "PB.debug.cardMenu('pb:dcomp01')"),
    "plugin-picker": ("machine", BIG, None, f"PB.debug.pickPlugin({LISTED})"),
    "plugin-search": ("machine", SMALL, None, f"PB.debug.pickPlugin({LISTED}, 'lsp comp')"),
    "tip-plugin": ("machine", BIG, plugged, "PB.debug.showTip('.card[data-key=\"pb:dhum01\"] header', 1100, 400)"),
    "eq": ("machine", BIG, equalized, ""),
    "eq-panel": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2)"),
    "eq-cut": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 0)"),
    "eq-hover": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 0) || EQ.debug.hover(2)"),
    "eq-drag": ("machine", BIG, equalized, "PB.debug.eqDrag('pb:deq01', 2, 60, -40)"),
    "eq-menu": ("machine", BIG, equalized, "PB.debug.eqMenu('pb:deq01', 1)"),
    "eq-gestures": ("machine", BIG, equalized, "PB.debug.eqGestures('pb:deq01', 2, 4, 1)"),
    "eq-card-menu": ("machine", BIG, equalized, "PB.debug.cardMenu('pb:deq01')"),
    "eq-empty": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq02', -1, false)"),
    "eq-bypass": ("machine", BIG, equalized, "PB.debug.eqBypass('pb:deq01') || PB.debug.eqOpen('pb:deq01', -1)"),
    "eq-small": ("machine", SMALL, equalized, "PB.debug.eqOpen('pb:deq01', 3)"),
    "eq-dynamic": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2) || EQ.debug.dyn(1, -6) || EQ.debug.dyn(2, 5) || EQ.debug.select(2)"),
    "eq-sharp-q": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2) || EQ.debug.sharp(2, 18) || EQ.debug.dyn(2, -12)"),
    "eq-dynamic-live": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2) || EQ.debug.dyn(2, -6) || EQ.debug.dyn(1, 6, -20) || EQ.debug.feed(3200, 0.5, -4)"),
    "eq-wheel-echo": ("machine", BIG, equalized, "PB.debug.eqEcho('pb:deq01', 2)"),
    "eq-range-drag": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2) || EQ.debug.dyn(2, 5) || EQ.debug.rangeDrag(2, 60)"),
    "eq-dynamic-small": ("machine", SMALL, equalized, "PB.debug.eqOpen('pb:deq01', 2) || EQ.debug.dyn(2, 5) || EQ.debug.select(2)"),
    "limiter": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dlim01')"),
    "limiter-drag": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dlim01', -6)"),
    "limiter-menu": ("machine", BIG, equalized, "PB.debug.cardMenu('pb:dlim01')"),
    "limiter-smooth": ("machine", BIG, equalized, "PB.debug.dynFlag('pb:dlim01', 'smooth') || PB.debug.dynOpen('pb:dlim01', -15.5)"),
    "compressor": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dcomp01')"),
    "compressor-menu": ("machine", BIG, equalized, "PB.debug.cardMenu('pb:dcomp01')"),
    "gate": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dgate01')"),
    "deesser": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dds01')"),
    "denoise": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dns01')"),
    "denoise-menu": ("machine", BIG, equalized, "PB.debug.cardMenu('pb:dns01')"),
    "gate-delete": ("machine", BIG, equalized, "PB.debug.askDeleteDevice('pb:dgate01')"),
    "compressor-show": ("machine", BIG, equalized, "PB.debug.dynOpen('pb:dcomp01') || PB.debug.panelTitle()"),
    "eq-moved": ("machine", BIG, equalized, "PB.debug.eqOpen('pb:deq01', 2) || PB.debug.panelMove(-120, -420)"),
    "compressor-add": ("machine", BIG, equalized, "PB.debug.menu(700, 300, ['Add node', 'Effects'])"),
    "wire-moving-left": ("machine", BIG, left_only, f"PB.debug.moveWire('{UR}', 'in3', '{UR}', 'in4')"),
    "wire-selected": ("machine", BIG, None, "PB.debug.select('music_player:out>desktop_mix:in')"),
    "wire-moving": ("machine", BIG, None, "PB.debug.moveWire('mic_mix', 'in', 'music_mix', 'in')"),
    "tip-then-menu": ("machine", BIG, None, "PB.debug.tipThenMenu('desktop_mix')"),
    "take-over": ("machine", BIG, None, "PB.debug.askTakeOver('virtual-devices.service', "
                  "['Desktop Mix', 'Music Mix', 'Mic Mix', 'Instrument Mix', 'Mic Instrument Mix', "
                  "'Stream Mix', 'Input1 Mix'])"),
    "drop-menu": ("machine", BIG, None, "PB.debug.dropWire('music_player', 'out', 560, 760)"),
    "wire-menu": ("machine", BIG, None, "PB.debug.wireMenu('music_player:out>desktop_mix:in', 330, 520)"),
    "tip-card": ("machine", BIG, renamed, "PB.debug.showTip('.card[data-key=\"mic_mix\"] header', 1100, 380)"),
    "tip-hardware": ("machine", BIG, None, "PB.debug.showTip('.card[data-key^=\"device:\"] header', 700, 180)"),
    "tip-socket": ("machine", BIG, None, "PB.debug.showTip('.card[data-key=\"instrument_mix\"] .sock[data-side=\"in\"]', 1100, 300)"),
    "tip-wire": ("machine", BIG, None, "PB.debug.showTip('.wire-g[data-wire*=\"out3>instrument_mix\"] .wire-hit', 1000, 600)"),
    "dragging": ("machine", BIG, None, "PB.debug.drag('desktop_mix', 23, -37)"),
    "live": ("machine", BIG, None, "PB.debug.fakeLive()"),
    "live-frame": ("machine", BIG, None, _frame()),
    "live-instant": ("machine", BIG, None, "PB.debug.liveInstant() || PB.debug.fakeLive()"),  # (off by default: this turns it on)
    "bar-wires": ("machine", BIG, None, "PB.debug.wiresMenu()"),
    "diag": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag()"),
    "diag-wave-panel": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag() || PB.debug.openDiag('pb:dwave01')"),
    "diag-stereo-panel": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag() || PB.debug.openDiag('pb:dster01')"),
    "diag-mono": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag(3) || PB.debug.diagMono('pb:dwave01')"),
    "diag-sync": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag(3) || PB.debug.diagSync('pb:dwave01')"),
    "diag-smooth": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag(3) || PB.debug.diagSmooth('pb:dwave01') || PB.debug.openDiag('pb:dwave01')"),
    "diag-meter-panel": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag() || PB.debug.openDiag('pb:dmeter01')"),
    "diag-spectrum-panel": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag() || PB.debug.openDiag('pb:dspec01')"),
    "diag-off": ("machine", BIG, diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag() || setTimeout(() => "
                 "['pb:dster01', 'pb:dwave01', 'pb:dmeter01', 'pb:dspec01'].forEach(PB.debug.diagOff) || PB.audit(), 500)"),
    "diag-menu": ("machine", BIG, diagnosed, "PB.debug.menu(560, 300, ['Add node', 'Diagnostic'])"),
    "bar-tidy": ("machine", BIG, None, "PB.debug.tidyMenu()"),
    "live-pedal": ("pedal", BIG, None, "PB.debug.fakeLive()"),
    "zoomed-out": ("machine", SMALL, None, "PB.debug.zoom(0.55)"),
    "empty": ("empty", SMALL, None, ""),
    "missing": ("machine", BIG, missing, "PB.debug.openProblems()"),
    "missing-menu": ("machine", BIG, missing, f"PB.debug.missingMenu('{PEDAL}')"),
    "changes": ("machine", BIG, changed, ""),
    "profile-menu": ("machine", BIG, changed, "PB.debug.profileMenu(['Load at launch'])"),
    "load-preview": ("machine", BIG, None, f"PB.debug.showPlan({PLAN})"),
    "save-as": ("machine", BIG, None, "PB.debug.saveAs()"),
    "delete-confirm": ("machine", SMALL, changed, "PB.debug.askDelete('Studio')"),
}


def _boot(name: str) -> dict:
    """The page's first state, exactly as the real server would send it."""
    scenario, _, setup, after = SHOTS[name]
    dump = [] if scenario == "empty" else scenarios.load(scenario)
    with tempfile.TemporaryDirectory() as tmp:
        dump = setup(Path(tmp), dump) if setup else dump
        board = server.Board(pipewire.Fixture(dump), Path(tmp))
        asyncio.run(server.startup(board))
        state = json.loads(board.state())
    return {**state, "mode": "preview", "after": after}


def _page(name: str) -> Path:
    html = (WEB / "index.html").read_text()
    boot = json.dumps(_boot(name)).replace("</", "<\\/")
    head = f'<base href="{WEB.as_uri()}/">\n<script>window.PB_BOOT = {boot};</script>'
    page = OUT / "pages" / f"{name}.html"
    page.write_text(html.replace("<head>", "<head>\n" + head, 1))
    return page


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if path := shutil.which(name):
            return path
    sys.exit("shots: needs google-chrome or chromium")


def _run(chrome: str, size: tuple, page: Path, *flags: str) -> str:
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           f"--user-data-dir={OUT / 'profile'}-{page.stem}", f"--window-size={size[0]},{size[1]}",
           "--virtual-time-budget=1500", "--force-device-scale-factor=1", *flags, page.as_uri()]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout


def shoot(chrome: str, name: str) -> tuple[str, list[str]]:
    size = SHOTS[name][1]
    page = _page(name)
    _run(chrome, size, page, f"--screenshot={OUT / (name + '.png')}")
    dom = _run(chrome, size, page, "--dump-dom")
    found = re.search(r'<pre id="audit"[^>]*>(.*?)</pre>', dom, re.S)
    faults = json.loads(unescape(found.group(1))) if found else ["the page wrote no audit"]
    return name, faults


def sheet(chrome: str, names: list[str], title: str = "sheet") -> None:
    """All shots on one page, screenshotted: one image to look at."""
    tiles = "".join(f'<figure><img src="{n}.png"><figcaption>{n}</figcaption></figure>'
                    for n in names)
    page = OUT / f"{title}.html"
    page.write_text("<style>body{margin:0;background:#111;color:#aaa;font:14px sans-serif;"
                    "display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:10px}"
                    "img{width:100%;display:block}figure{margin:0}</style>" + tiles)
    rows = -(-len(names) // 3)
    _run(chrome, (1800, rows * 380 + 20), page, f"--screenshot={OUT / (title + '.png')}")


def icon(chrome: str) -> None:
    """The app and tray icons at the sizes a desktop uses, on a dark and a light panel."""
    sizes = (16, 22, 24, 32, 48, 64, 128, 256)
    rows = "".join(
        f'<div style="background:{bg};display:flex;align-items:flex-end;gap:18px;padding:16px">'
        + "".join(f'<img src="{WEB.as_uri()}/{name}.svg" width="{n}" height="{n}">' for n in sizes)
        + "</div>" for name in ("icon", "tray") for bg in ("#1b1b1b", "#eff0f1"))
    page = OUT / "icon.html"
    page.write_text(f"<body style='margin:0'>{rows}</body>")
    _run(chrome, (760, 1180), page, f"--screenshot={OUT / 'icon.png'}")
    print(f"icon         {OUT / 'icon.png'}")


def main() -> int:
    args = sys.argv[1:]
    if args == ["icon"]:
        OUT.mkdir(parents=True, exist_ok=True)
        icon(_chrome())
        return 0
    if "--list" in args:
        print("\n".join(SHOTS))
        return 0
    # A group: `shots eq` is every shot named eq or eq-…, with its own sheet (eq-sheet.png).
    groups = [a for a in args if any(n.startswith(a + "-") for n in SHOTS)]
    names = [n for n in SHOTS if n in args or any(n == g or n.startswith(g + "-") for g in groups)]
    names = names or list(SHOTS)
    unknown = [a for a in args if a not in SHOTS and a not in groups]
    if unknown:
        sys.exit(f"shots: unknown {unknown}; try --list")
    (OUT / "pages").mkdir(parents=True, exist_ok=True)
    chrome = _chrome()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda n: shoot(chrome, n), names))
    for name, faults in results:
        print(f"{'FAULT' if faults else 'ok   '} {name:<12} {OUT / (name + '.png')}")
        for f in faults:
            print(f"        - {f}")
    if len(names) > 1:
        title = groups[0] + "-sheet" if len(groups) == 1 and len(args) == 1 else "sheet"
        sheet(chrome, names, title)
        print(f"sheet        {OUT / (title + '.png')}")
    for p in OUT.glob("profile-*"):
        shutil.rmtree(p, ignore_errors=True)
    return sum(1 for _, f in results if f)


if __name__ == "__main__":
    sys.exit(main())
