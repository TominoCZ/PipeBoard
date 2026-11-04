"""Taking over the virtual devices a systemd service made (a setup script), once, confirmed.

Which devices: cards made of loopback halves whose owning process runs inside a systemd user
service, by its cgroup, never by name. The take-over stops and disables that service (systemd
ends its pw-loopback processes with it), then makes each device again as one of PipeBoard's
own, under the same node.name, so apps and the default devices keep finding them; the board's
cards keep their place and nickname, and their wires come back as each device appears.
Undo: delete those devices in PipeBoard, then `systemctl --user enable --now <service>`.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from pathlib import Path

NODE, CLIENT = "PipeWire:Interface:Node", "PipeWire:Interface:Client"


def cgroup_unit(pid: int) -> str | None:
    """The systemd user service a process runs in, from /proc/<pid>/cgroup, or None."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    found = re.findall(r"/([^/\n]+\.service)", text)
    units = [u for u in found if not u.startswith(("user@", "app-", "session-"))]
    return units[-1] if units else None


def _pids(dump: list[dict]) -> dict[int, int]:
    """Node id → the process id of the client that owns it."""
    clients = {o["id"]: (o.get("info") or {}).get("props", {}) for o in dump if o["type"] == CLIENT}
    pids = {}
    for o in dump:
        if o["type"] != NODE:
            continue
        props = (o.get("info") or {}).get("props", {})
        pid = clients.get(props.get("client.id"), {}).get("application.process.id")
        if pid:
            pids[o["id"]] = int(pid)
    return pids


def find(dump: list[dict], model: dict, unit_of: Callable[[int], str | None] = cgroup_unit) -> dict:
    """{service: [cards]} for the virtual devices a systemd service made (not PipeBoard's)."""
    pids = _pids(dump)
    found: dict[str, list[dict]] = {}
    for card in model["cards"]:
        if card["kind"] != "virtual" or card["owned"] or card["role"] not in ("mic", "speaker"):
            continue
        units = {unit_of(pids[i]) for i in card["ids"] if i in pids}
        if len(units) == 1 and None not in units:
            found.setdefault(units.pop(), []).append(card)
    return found


def node_name(card: dict) -> str:
    """The name apps know the device by: the device half of the loopback (mic_mix, not
    mic_mix_capture)."""
    names = card["names"]
    return next((n for n in names if not n.endswith(("_capture", "_playback"))), names[0])


def device(card: dict) -> dict:
    side = card["outputs"] if card["role"] == "mic" else card["inputs"]
    channels = 1 if side and len(side[0]["ports"]) == 1 else 2
    return {"id": "d" + secrets.token_hex(5), "type": card["role"], "name": card["title"],
            "node": node_name(card), "channels": channels}


def rewrite(board: dict, cards: list[dict]) -> list[str]:
    """Move each taken-over card to its new PipeBoard key; return the new keys."""
    keys = []
    for card in cards:
        dev = device(card)
        new, old = f"pb:{dev['id']}", card["key"]
        board.setdefault("devices", {})[new] = dev
        board["cards"][new] = {f: v for f, v in board["cards"].pop(old, {}).items() if f != "seen"}
        for w in board["wires"]:
            for end in ("from", "to"):
                if w[end][0] == old:
                    w[end] = [new, w[end][1]]
        keys.append(new)
    return keys
