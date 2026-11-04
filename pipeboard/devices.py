"""PipeBoard's own virtual devices: what they are, and the PipeWire properties that make them.

Each is one null-sink node created inside PipeWire with object.linger, so it outlives PipeBoard
(a stream mic must not vanish because the window closed) and needs no process. A PipeWire restart
clears it; the board remembers it, so it can be created again. Its card key is pb:<id>, which
never changes; its node.name never changes either (apps remember devices by it), only the name
apps show (node.description).
"""

from __future__ import annotations

import math
import re
import secrets
import unicodedata

from pipeboard import graph

# A gain is a null sink too: its volume is the gain on its monitor outputs. /Internal keeps it
# out of apps' lists of outputs.
CLASS = {"mic": "Audio/Source/Virtual", "speaker": "Audio/Sink", "gain": "Audio/Sink/Internal"}
WORD = {"mic": "microphone", "speaker": "speaker", "gain": "gain"}
# Which board made a node: set once by the server (a hash of its config folder). A PipeBoard
# with another config (a test run, a second user setup) never shows or takes another's devices.
# ponytail: one module-level value; one Board per process is all the app ever runs.
BOARD = ""
# Shown to apps and written into PipeWire's property syntax: no quotes, braces, brackets or '='.
NAME = re.compile(r'^[^"\\{}\[\]=\n\r\t]{1,40}$')


def valid_name(name: str) -> bool:
    return bool(NAME.match(name or "")) and name.strip() == name


def _slug(name: str, taken: set[str]) -> str:
    """A node.name from the first name: ascii words joined by _, unique on this system."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = "_".join(re.findall(r"[a-z0-9]+", ascii_name.lower()))[:32] or "device"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}_{n}", n + 1
    return slug


def new(kind: str, name: str, channels: int, taken: set[str]) -> dict:
    """A device definition, as kept on the board and in profiles."""
    if kind not in CLASS or channels not in (1, 2) or not valid_name(name):
        raise ValueError("not a device PipeBoard can make")
    dev = {"id": "d" + secrets.token_hex(5), "type": kind, "name": name,
           "node": _slug(name, taken), "channels": channels}
    if kind == "gain":
        dev["gain_db"] = 0.0
    return dev


def props(dev: dict) -> str:
    """The node's properties, in PipeWire's syntax, for `pw-cli create-node adapter`."""
    position = "[ FL FR ]" if dev["channels"] == 2 else "[ MONO ]"
    return ("{ factory.name=support.null-audio-sink "
            f"node.name={dev['node']} node.description=\"{dev['name']}\" "
            f"media.class={CLASS[dev['type']]} audio.position={position} "
            "object.linger=true node.virtual=true monitor.channel-volumes=true "
            f"pipeboard.owned=true pipeboard.id={dev['id']} pipeboard.board={BOARD or 'none'} "
            f"pipeboard.kind={'effect' if dev['type'] == 'gain' else 'virtual'} }}")


def adopt(dump: list[dict]) -> dict[str, dict]:
    """This board's own lingering devices found on the system, as definitions rebuilt from their
    nodes (gains with their current level). The board takes back one it lost — a launch profile
    saved before the device was made replaced the board, and the device lingered on, a card with
    no level box that nothing could remove (user). Nodes made before boards were
    tagged count as this board's."""
    kinds = {cls: kind for kind, cls in CLASS.items()}
    found = {}
    for o in dump:
        info = o.get("info") or {}
        p = graph._props(o)
        mine = p.get("pipeboard.board") in (None, BOARD)  # untagged: made before boards were
        if not o["type"].endswith("Node") or not p.get("pipeboard.owned") or not mine \
                or p.get("media.class") not in kinds:
            continue
        kind = kinds[p["media.class"]]
        ports = info.get("n-output-ports" if kind == "mic" else "n-input-ports") or 2
        dev = {"id": str(p["pipeboard.id"]), "type": kind, "channels": 1 if ports == 1 else 2,
               "name": p.get("node.description") or p.get("node.name", "Device"),
               "node": p.get("node.name", "")}
        if kind == "gain":
            vols = next((pr["channelVolumes"] for pr in (info.get("params") or {}).get("Props", [])
                         if isinstance(pr, dict) and pr.get("channelVolumes")), [1.0])
            dev["gain_db"] = round(20 * math.log10(vols[0]), 1) if vols[0] > 0 else -144.0
        found[f"pb:{dev['id']}"] = dev
    return found


def moved_links(dump: list[dict], old: set[int], new: set[int],
                apps: set[int]) -> list[tuple[int, int]] | None:
    """The links that wire node(s) `new` like `old`: each of old's links again, its end on the new
    port of the same name. None while a port is missing (the new node's ports come a moment
    after it). Not links to `apps` (node ids): WirePlumber routes apps, and moves only links it
    made itself; a copy pinned the app here (user: switching its device did nothing)."""
    ports = {o["id"]: o["info"]["props"] for o in dump if o["type"].endswith("Port")}
    named = {(p.get("port.name")): pid for pid, p in ports.items() if p.get("node.id") in new}
    swap = lambda pid: (named.get(ports[pid].get("port.name"), -1)  # noqa: E731
                        if pid in ports and ports[pid].get("node.id") in old else pid)
    out = []
    for o in dump:
        if o["type"].endswith("Link"):
            a, b = o["info"]["output-port-id"], o["info"]["input-port-id"]
            if {ports.get(a, {}).get("node.id"), ports.get(b, {}).get("node.id")} & apps:
                continue
            if (a, b) != (pair := (swap(a), swap(b))):
                if -1 in pair:
                    return None
                out.append(pair)
    return out

