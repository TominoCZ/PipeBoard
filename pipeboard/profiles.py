"""Profiles: the whole board saved, and how two boards, or a board and the system, differ (PLAN §7).

A profile is {"version", "cards": {key: {x, y, nick, names, hidden, seen}}, "wires": [...], "view"}.
`seen` is the card's last known shape, so a device that is gone can still be drawn and re-wired.
The board the user works on is itself a profile (workspace.json), kept in step with the system
by snapshot(); a named profile is a saved copy of it.
"""

from __future__ import annotations

import copy
import re

from pipeboard import dynamics, graph, workspace

DEFAULT = "Default"
# What the user sets on a card; "parked": the wires they made, set aside while it is off the board.
BOARD = ("x", "y", "nick", "names", "hidden", "parked")
NAME = re.compile(r"^[\w][\w \-().']{0,39}$")  # also a file name: no slashes, no dots first


def valid_name(name: str) -> bool:
    return bool(NAME.match(name or "")) and not name.endswith(" ")


def _shape(card: dict) -> dict:
    side = lambda socks: [{"id": s["id"], "label": s["label"], "n": len(s["ports"])} for s in socks]  # noqa: E731
    return {"title": card["title"], "kind": card["kind"], "role": card["role"],
            "inputs": side(card["inputs"]), "outputs": side(card["outputs"])}


def _wire(w: dict) -> dict:
    return {"from": list(w["from"]), "to": list(w["to"]), "pairs": sorted(map(list, w["pairs"]))}


def snapshot(model: dict, board: dict) -> dict:
    """The board as it is now: live cards and wires, plus what it knew about devices now gone."""
    live = {c["key"] for c in model["cards"]}
    old = {k: c for k, c in board.get("cards", {}).items() if k}  # a nameless key was a bug
    cards = {k: {**{f: v for f, v in c.items() if f in BOARD}} for k, c in old.items()}
    for c in model["cards"]:
        cards.setdefault(c["key"], {})["seen"] = _shape(c)
    for k in set(old) - live:
        if "seen" in old[k]:
            cards[k]["seen"] = old[k]["seen"]
    gone = [w for w in board.get("wires", [])
            if (w["from"][0] not in live or w["to"][0] not in live) and w["from"][0] and w["to"][0]]
    wires = [_wire(w) for w in model["wires"]] + copy.deepcopy(gone)
    ids = {f"{w['from'][0]}:{w['from'][1]}>{w['to'][0]}:{w['to'][1]}" for w in wires}
    return {"version": 1, "grid": workspace.CELLS, "cards": cards, "wires": wires,
            "view": board.get("view"),
            "devices": copy.deepcopy(board.get("devices", {})),
            "made": [i for i in board.get("made", []) if i in ids],  # wires the user made here
            "cut": [i for i in board.get("cut", []) if i not in ids],  # and unplugged here
            # wires drawn as portals (board-only: the sound goes straight from A to B)
            "portals": {i: how for i, how in board.get("portals", {}).items() if i in ids},
            "tidy_portals": board.get("tidy_portals", False)}


def missing(board: dict, model: dict) -> list[dict]:
    """Devices the board names that are not on the system.

    Not missing: anything on the system (even with nothing to show), apps (they come and go), and
    cards the user removed from the board.
    """
    live = set(model.get("present") or (c["key"] for c in model["cards"]))
    devices = board.get("devices", {})
    return [{"key": k, **(c.get("seen") or _unseen(devices[k]))}
            for k, c in board.get("cards", {}).items()
            if k not in live and ("seen" in c or k in devices) and not c.get("hidden")
            and (c.get("seen") or {}).get("kind") != "app"]


def _unseen(dev: dict) -> dict:
    """A device of the board's own that never appeared here (owner: a plugin not installed,
    started at every launch, never drawn, so it could not be removed): what its definition says,
    so it is drawn missing and can be removed or replaced."""
    kind = ("plugin" if dev["type"] == "plugin" else "virtual" if dev["type"] in ("mic", "speaker")
            else "diag" if dev["type"] in ("waveform", "stereo", "counter", "meter", "spectrum")
            else "effect")
    n = dev.get("channels") or 2
    ins, outs = dev.get("ins", n), dev.get("outs", n)
    return {"title": dev.get("name") or dev["type"], "kind": kind,
            "role": dev["type"] if kind == "virtual" else "",
            "inputs": [{"id": "in", "label": "IN", "n": ins}] if ins else [],
            "outputs": [{"id": "out", "label": "OUT", "n": outs}]
            if outs and kind != "diag" else []}


def _name(board: dict, key: str) -> str:
    card = board.get("cards", {}).get(key, {})
    return card.get("nick") or card.get("seen", {}).get("title") or key


def _label(board: dict, end: list, side: str) -> str:
    socks = board.get("cards", {}).get(end[0], {}).get("seen", {}).get(side, [])
    found = next((s["label"] for s in socks if s["id"] == end[1]), "")
    return f" {found}" if len(socks) > 1 and found else ""


def describe(board: dict, w: dict) -> str:
    """'Studio Interface OUT 2 → Mic Mix' in the names the user sees."""
    return (f"{_name(board, w['from'][0])}{_label(board, w['from'], 'outputs')} → "
            f"{_name(board, w['to'][0])}{_label(board, w['to'], 'inputs')}")


def ports(model: dict, end: list, side: str) -> list[int] | None:
    card = next((c for c in model["cards"] if c["key"] == end[0]), None)
    sock = next((s for s in card[side] if s["id"] == end[1]), None) if card else None
    return sock["ports"] if sock else None


def plan(board: dict, model: dict) -> dict:
    """What making the system match the board takes: links to make and to remove.

    Only links between two cards the board knows are removed; anything new is left alone.
    """
    want, make = set(), []
    for w in board.get("wires", []):
        outs, ins = ports(model, w["from"], "outputs"), ports(model, w["to"], "inputs")
        if not outs or not ins:
            continue  # an end is missing: nothing to do until it comes back
        for i, j in w["pairs"]:
            if i < len(outs) and j < len(ins) and (outs[i], ins[j]) not in want:
                want.add((outs[i], ins[j]))
                make.append({"out": outs[i], "in": ins[j], "text": describe(board, w)})
    known, remove, live = set(board.get("cards", {})), [], set()
    for w in model["wires"]:
        outs, ins = ports(model, w["from"], "outputs"), ports(model, w["to"], "inputs")
        for link, (i, j) in zip(w["links"], w["pairs"], strict=True):
            live.add((outs[i], ins[j]))
            if (outs[i], ins[j]) not in want and w["from"][0] in known and w["to"][0] in known:
                remove.append({"link": link, "text": describe(board, w)})
    make = [m for m in make if (m["out"], m["in"]) not in live]
    return {"make": make, "remove": remove,
            "missing": [m["title"] for m in missing(board, model)]}


def texts(items: list[dict]) -> list[str]:
    return list(dict.fromkeys(i["text"] for i in items))


def routed(board: dict, w: dict) -> bool:
    """A wire to an app the user did not make here: the system's (WirePlumber links an app to the
    default device as it appears), so no change of theirs to save or revert (user)."""
    cards = board["cards"]
    kinds = {cards.get(k, {}).get("seen", {}).get("kind") for k in (w["from"][0], w["to"][0])}
    # Never into an effect or plugin of PipeBoard's: WirePlumber cannot route an app to those.
    return ("app" in kinds and not kinds & {"effect", "plugin"}
            and _wid(w) not in board.get("made", []))


def _wid(w: dict) -> str:
    return f"{w['from'][0]}:{w['from'][1]}>{w['to'][0]}:{w['to'][1]}"


def changes(saved: dict, now: dict) -> list[str]:
    """What changed between a saved profile and the board now, one line each, for the user."""
    key = lambda w: (tuple(w["from"]), tuple(w["to"]), tuple(map(tuple, w["pairs"])))  # noqa: E731
    before, after = {key(w): w for w in saved.get("wires", [])}, {key(w): w for w in now["wires"]}
    # Only what the user did here (owner: "when something changes in the graph because of
    # something other than PipeBoard, do we still get a revert/save popup?"): a wire they made
    # (`made`) or unplugged (`cut`). One another program made or removed (Easy Effects rewiring
    # itself, WirePlumber moving an app or a virtual device, qpwgraph) is not theirs to save.
    made, cut = set(now.get("made", [])), set(now.get("cut", []))
    lines = [f"+ {describe(now, after[k])}" for k in after
             if k not in before and _wid(after[k]) in made]
    lines += [f"− {describe(saved, before[k])}" for k in before
              if k not in after and _wid(before[k]) in cut]
    lines = list(dict.fromkeys(lines))
    moved = 0
    for k in set(saved.get("cards", {})) | set(now["cards"]):
        a, b = saved.get("cards", {}).get(k, {}), now["cards"].get(k, {})
        if a.get("nick") != b.get("nick"):
            old = a.get("nick") or a.get("seen", {}).get("title", k)
            lines.append(f"Renamed {old} to {_name(now, k)}" if b.get("nick")
                         else f"{old} has its original name again")
        if bool(a.get("hidden")) != bool(b.get("hidden")):
            lines.append(f"{'Removed' if b.get('hidden') else 'Put back'} {_name(now, k)}"
                         f" {'from' if b.get('hidden') else 'on'} the board")
        moved += _moved(a, b)
    if moved:
        lines.append(f"Moved {moved} card{'s' if moved > 1 else ''}")
    # Portals too (owner: never offered for saving, the launch profile turned them back into wires)
    was, now_p = set(saved.get("portals", {})), set(now.get("portals", {}))
    if n := len(now_p - was):
        lines.append(f"Made {n} portal{'s' if n > 1 else ''}")
    if n := len(was - now_p):
        lines.append(f"Turned {n} portal{'s' if n > 1 else ''} back into {'wires' if n > 1 else 'a wire'}")
    before, after = saved.get("devices", {}), now.get("devices", {})
    lines += [f"Made {WHAT.get(after[k]['type'], 'the')} {after[k]['name']}" for k in after
              if k not in before]
    lines += [f"Deleted {WHAT.get(before[k]['type'], 'the')} {before[k]['name']}" for k in before
              if k not in after]
    lines += [f"Changed the settings of {after[k]['name']}" for k in after
              if k in before and _settings_changed(before[k], after[k])]
    return lines


def unsaved(saved: dict, now: dict) -> dict:
    """The cards with an unsaved change, for the board's dots (owner): moved, renamed, made, or
    their settings changed; a moved one with the spot it was saved at, for its ghost."""
    out = {}
    for k, b in now["cards"].items():
        a = saved.get("cards", {}).get(k, {})
        if _moved(a, b):
            out[k] = {"x": a["x"], "y": a["y"]}
        elif a.get("nick") != b.get("nick"):
            out[k] = {}
    before = saved.get("devices", {})
    for k, dev in now.get("devices", {}).items():
        if k not in before or _settings_changed(before[k], dev):
            out.setdefault(k, {})
    return out


def _settings_changed(was: dict, dev: dict) -> bool:
    """A device's settings changed since the save (a plugin's controls count once the profile has
    them: the first time it loads, it reports them itself, and that read as the user's edit,
    owner's "Changed the settings of Calf Vinyl")."""
    return _settings(was) != _settings(dev if was.get("params") else {**dev, "params": []})


WHAT = {"mic": "the virtual microphone", "speaker": "the virtual speaker", "gain": "the gain",
        "plugin": "the plugin", "eq": "the EQ", "limiter": "the limiter",
        "compressor": "the compressor", "gate": "the noise gate",
        "denoise": "the noise suppression", "deesser": "the de-esser"}


def _settings(dev: dict) -> tuple:
    """What a gain, EQ, limiter, compressor, plugin or diagnostic card is set to: its level, its
    bands, its dynamics settings, its controls' values, bypass, a Waveform's Stereo/Mono, Smooth
    and Zoom (owner: saved and revertible; Smooth is the same field as a limiter's Soft). (EQ
    bands were left out: an EQ edit never showed as a change, so nothing could revert it.) A
    Counter's totals are not settings: Revert never resets them."""
    return (dev.get("gain_db"), dev.get("bands"), [dev.get(k) for k in dynamics.SETTINGS],
            bool(dev.get("smooth")),
            [(p["i"], p.get("value")) for p in dev.get("params", [])], bool(dev.get("bypass")),
            bool(dev.get("mono")), dev.get("zoom", True), bool(dev.get("lines")))


def _moved(a: dict, b: dict) -> bool:
    """Moved by a cell or more (positions are whole cells)."""
    return "x" in a and "x" in b and (a["x"], a["y"]) != (b["x"], b["y"])


def _new_socket(old: dict, new: dict, side: str, sock: str) -> dict | None:
    """The new card's socket for an old one: same id, else same place in the list, else none."""
    socks = new[side]
    same = next((s for s in socks if s["id"] == sock), None)
    if same:
        return same
    old_ids = [s["id"] for s in old.get(side, [])]
    i = old_ids.index(sock) if sock in old_ids else -1
    return socks[i] if 0 <= i < len(socks) else None


def _repair(w: dict, n_out: int, n_in: int) -> None:
    """Keep the pairs that still fit; if a socket changed size, pair it the default way."""
    if all(i < n_out and j < n_in for i, j in w["pairs"]):
        return
    w["pairs"] = [list(p) for p in graph.pair(list(range(n_out)), list(range(n_in)))]


def _size(cards: dict, end: list, side: str, pairs: list) -> int:
    socks = cards.get(end[0], {}).get("seen", {}).get(side, [])
    found = next((s["n"] for s in socks if s["id"] == end[1]), 0)
    return found or 1 + max((p[0 if side == "outputs" else 1] for p in pairs), default=0)


def retarget(board: dict, old: str, card: dict) -> list[str]:
    """Point a missing device's place on the board at an existing card; return what was dropped.

    Never fails on a socket count mismatch: what fits is kept, the rest is listed and dropped.
    """
    cards, new = board["cards"], _shape(card)
    before = cards.pop(old, {})
    shape = before.get("seen", {"inputs": [], "outputs": []})
    mine = lambda c: {f: v for f, v in c.items() if f in BOARD}  # noqa: E731
    # The chosen device takes the missing one's place, name and history on the board.
    cards[card["key"]] = {**mine(cards.get(card["key"], {})), **mine(before), "seen": new}
    kept, dropped = [], []
    for w in board.get("wires", []):
        if old not in (w["from"][0], w["to"][0]):
            kept.append(w)
            continue
        text = describe({**board, "cards": {**cards, old: before}}, w)
        ends = [(end, side, _new_socket(shape, new, side, w[end][1]))
                for end, side in (("from", "outputs"), ("to", "inputs")) if w[end][0] == old]
        if any(sock is None for _, _, sock in ends):
            dropped.append(text)
            continue
        for end, _, sock in ends:
            w[end] = [card["key"], sock["id"]]
        _repair(w, _size(cards, w["from"], "outputs", w["pairs"]),
                _size(cards, w["to"], "inputs", w["pairs"]))
        kept.append(w)
    board["wires"] = kept
    return dropped
