"""Graphs to test and preview with: the user's machine as captured, and variations of it.

    load("machine")   the trimmed pw-dump in pw-dump.json
    load("pedal")    plus a Guitar Pedal used as an effect: Studio Interface 3-4 → Pedal → Instrument Mix
    load("problems")  plus a feedback loop, a one-sided stereo input and a failed link
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODE, PORT, LINK = (f"PipeWire:Interface:{t}" for t in ("Node", "Port", "Link"))
UR_IN = "alsa_input.usb-Example_Studio_Interface-00.pro-input-0"


def _port(dump, node_name, port_name):
    nodes = {o["id"]: o["info"]["props"].get("node.name") for o in dump if o["type"] == NODE}
    return next(o["id"] for o in dump if o["type"] == PORT
                and nodes.get(o["info"]["props"]["node.id"]) == node_name
                and o["info"]["props"]["port.name"] == port_name)


def _link(lid, out, inp, state="active"):
    return {"id": lid, "type": LINK, "info": {"output-port-id": out, "input-port-id": inp,
                                              "state": state, "props": {}}}


def _node(nid, name, media, desc, serial):
    """A Pedal half: its own USB clock, at 48 kHz while the graph runs at 192."""
    props = {"node.name": name, "media.class": media, "node.description": desc,
             "device.id": 900, "device.api": "alsa", "api.alsa.card.name": "Pedal",
             "clock.name": "api.alsa.9", "object.serial": serial}
    return {"id": nid, "type": NODE, "info": {"props": props, "params": {"Format": [{"rate": 48000}]}}}


def _ports(first, nid, direction, names, monitor=False):
    return [{"id": first + i, "type": PORT, "info": {"direction": direction, "props": {
        "node.id": nid, "port.name": n, "audio.channel": n[-2:], "port.id": i,
        "format.dsp": "32 bit float mono audio", "port.monitor": monitor or None}}}
            for i, n in enumerate(names)]


def _pedal(dump):
    """A two-in, two-out USB pedal, wired as an effect loop for the guitar."""
    dump = [o for o in dump if not (o["type"] == LINK and o["info"]["input-port-id"] in {
        _port(dump, "instrument_mix_capture", "input_FL"), _port(dump, "instrument_mix_capture", "input_FR")})]
    dump += [_node(901, "alsa_output.usb-Example_Guitar_Pedal-00.analog-stereo", "Audio/Sink",
                   "Pedal Analog Stereo", 5001),
             _node(902, "alsa_input.usb-Example_Guitar_Pedal-00.analog-stereo", "Audio/Source",
                   "Pedal Analog Stereo", 5002)]
    dump += [{"id": 900, "type": "PipeWire:Interface:Device", "info": {"props": {
        "device.name": "alsa_card.usb-Example_Guitar_Pedal-00", "device.description": "Pedal"}}}]
    dump += _ports(910, 901, "input", ["playback_FL", "playback_FR"])
    dump += _ports(912, 901, "output", ["monitor_FL", "monitor_FR"], monitor=True)
    dump += _ports(914, 902, "output", ["capture_FL", "capture_FR"])
    dump += [_link(920, _port(dump, UR_IN, "capture_RL"), 910),
             _link(921, _port(dump, UR_IN, "capture_RR"), 911),
             _link(922, 914, _port(dump, "instrument_mix_capture", "input_FL")),
             _link(923, 915, _port(dump, "instrument_mix_capture", "input_FR"))]
    return dump


def _problems(dump):
    """A ring Mic Mix → Desktop Mix → Mic Mix, Input1 Mix fed on the left only, a failed link."""
    p = lambda n, s: _port(dump, n, s)  # noqa: E731
    one_sided = {p("input1_mix_capture", "input_FR")}
    dump = [o for o in dump if not (o["type"] == LINK and o["info"]["input-port-id"] in one_sided)]
    return dump + [
        _link(930, p("mic_mix", "capture_FL"), p("desktop_mix", "playback_FL")),
        _link(931, p("mic_mix", "capture_FR"), p("desktop_mix", "playback_FR")),
        _link(932, p("desktop_mix_playback", "output_AUX0"), p("mic_mix_capture", "input_FL")),
        _link(933, p("desktop_mix_playback", "output_AUX1"), p("mic_mix_capture", "input_FR")),
        _link(934, p("music_player", "output_FL"), p("music_mix", "playback_FL"), state="error"),
        _link(935, p("music_player", "output_FR"), p("music_mix", "playback_FR"), state="error"),
    ]


def load(name="machine"):
    dump = json.loads((HERE / "pw-dump.json").read_text())
    return {"machine": lambda d: d, "pedal": _pedal, "problems": _problems}[name](dump)
