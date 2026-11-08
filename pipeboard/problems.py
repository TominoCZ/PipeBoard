"""Find problems in a board model, worst first (PLAN §5a). A free socket is never a problem."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

ORDER = {"error": 0, "warning": 1, "info": 2}


def _passes(card: dict) -> bool:
    """Whether sound entering this card comes out of it: not for hardware jacks or apps."""
    return card["kind"] not in ("hardware", "app")


def _problem(severity: str, title: str, said: tuple[str, str, str], cards=(), wires=()) -> dict:
    """said: (where, what, fix) — short, the key facts only (owner: concise, laid out): the page
    shows them on their own lines; "text" joins them for the tray and tooltips."""
    where, what, fix = said
    return {"severity": severity, "title": title, "where": where, "what": what, "fix": fix,
            "text": " ".join(x for x in (f"{where}:" if where else "", what, fix) if x),
            "cards": list(cards), "wires": list(wires)}


def find(model: dict) -> list[dict]:
    """Return the model's problems, worst first."""
    cards = {c["key"]: c for c in model["cards"]}
    wires = model["wires"]
    found = _loops(cards, wires) + _failed(cards, wires) + _one_sided(cards, wires)
    found += _doubled(cards, wires) + _same_names(cards)
    found += _resampled(cards, model.get("clock", {})) + _two_clocks(cards, wires)
    return sorted(found, key=lambda p: ORDER[p["severity"]])


def _name(cards: dict, key: str) -> str:
    return cards[key]["title"] if key in cards else key


def _loops(cards: dict, wires: list[dict]) -> list[dict]:
    """Cycles through cards that pass sound on: each one howls."""
    edges = defaultdict(set)
    for w in wires:
        a, b = w["from"][0], w["to"][0]
        if a in cards and b in cards and _passes(cards[a]) and _passes(cards[b]):
            edges[a].add(b)
    found, reported = [], set()
    for start in sorted(edges):
        path = _cycle_from(start, edges)
        if path and frozenset(path) not in reported:
            reported.add(frozenset(path))
            ring = path + [path[0]]
            steps = set(zip(ring, ring[1:], strict=False))
            loop_wires = [w["id"] for w in wires if (w["from"][0], w["to"][0]) in steps]
            found.append(_problem(
                "error", "Feedback loop",
                (" → ".join(_name(cards, k) for k in ring), "Sound feeds back into itself.",
                 "Remove one of these wires."), path, loop_wires))
    return found


def _cycle_from(start: str, edges: dict) -> list[str] | None:
    """The shortest cycle through start, as a list of cards, or None."""
    queue, seen = [[start]], {start}
    while queue:
        path = queue.pop(0)
        for nxt in sorted(edges[path[-1]]):
            if nxt == start:
                return path
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return None


def _failed(cards: dict, wires: list[dict]) -> list[dict]:
    return [_problem(
        "error", "Connection failed",
        (f"{_name(cards, w['from'][0])} → {_name(cards, w['to'][0])}",
         "PipeWire could not connect them (often a format mismatch).",
         "Remove the wire and make it again."),
        (w["from"][0], w["to"][0]), [w["id"]]) for w in wires if w["failed"]]


def _one_sided(cards: dict, wires: list[dict]) -> list[dict]:
    """A stereo input that receives sound on one channel only."""
    fed = defaultdict(set)
    into = defaultdict(list)
    for w in wires:
        fed[tuple(w["to"])].update(w["toPorts"])
        into[tuple(w["to"])].append(w["id"])
    found = []
    for (key, sock), used in fed.items():
        card = cards.get(key)
        socket = next((s for s in card["inputs"] if s["id"] == sock), None) if card else None
        if socket and len(socket["ports"]) == 2 and len(used) == 1:
            side, other = ("left", "right") if 0 in used else ("right", "left")
            found.append(_problem(
                "warning", "Only one side of a stereo input",
                (f"{card['title']} {socket['label']}", f"Sound on the {side} only.",
                 f"Wire the {other} too, or a mono source to both."), [key], into[(key, sock)]))
    return found


def _origins(key: str, cards: dict, into: dict, seen: set) -> set:
    """Where the sound leaving a card started: outputs of cards that do not pass sound on."""
    if key in seen:
        return set()
    seen.add(key)
    found = set()
    for w in into.get(key, ()):
        src = w["from"][0]
        if src in cards and _passes(cards[src]):
            found |= _origins(src, cards, into, seen)
        else:
            found.add(tuple(w["from"]))
    return found


def _doubled(cards: dict, wires: list[dict]) -> list[dict]:
    """The same source reaching one input socket by two routes: it phases, echoes or doubles."""
    live = [w for w in wires if not w["failed"]]
    into, by_socket = defaultdict(list), defaultdict(list)
    for w in live:
        into[w["to"][0]].append(w)
        by_socket[tuple(w["to"])].append(w)
    found = []
    # ponytail: a card passes every input to every output; a real mixer's routing is finer.
    for (key, _sock), incoming in by_socket.items():
        counts = defaultdict(list)
        for w in incoming:
            src = w["from"][0]
            starts = _origins(src, cards, into, {key}) if src in cards and _passes(cards[src]) \
                else {tuple(w["from"])}
            for origin in starts:
                counts[origin].append(w["id"])
        for (okey, osock), ids in counts.items():
            if len(ids) > 1:
                source = f"{_name(cards, okey)} {_socket_label(cards, okey, osock)}".strip()
                found.append(_problem(
                    "warning", "Same sound arrives twice",
                    (f"{source} → {_name(cards, key)}", f"{len(ids)} routes: louder, may echo.",
                     "Keep one."), [key, okey], ids))
    return found


def _socket_label(cards: dict, key: str, sock: str) -> str:
    card = cards.get(key)
    found = next((s["label"] for s in card["outputs"] if s["id"] == sock), "") if card else ""
    return f"({found})" if found else ""


def _same_names(cards: dict) -> list[dict]:
    by = defaultdict(list)
    for c in cards.values():
        if c["kind"] == "virtual" and not c["hidden"]:
            by[c["title"]].append(c["key"])
    return [_problem("info", "Two devices with one name",
                     (title, f"{len(keys)} devices share it: easy to pick the wrong one.",
                      "Rename one."), keys)
            for title, keys in by.items() if len(keys) > 1]


def _resampled(cards: dict, clock: dict) -> list[dict]:
    rate = clock.get("rate")
    return [_problem(
        "info", "Converted to another sample rate",
        (c["title"], f"{c['rate'] / 1000:g} kHz, the rest {rate / 1000:g} kHz: converted "
         "(a little CPU and delay).", ""), [c["key"]])
        for c in cards.values() if c["rate"] and rate and c["rate"] != rate and not c["hidden"]]


def _two_clocks(cards: dict, wires: list[dict]) -> list[dict]:
    """Sound passing from one hardware clock to another: kept in step by adaptive resampling."""
    found, seen = [], set()
    into = defaultdict(list)
    for w in wires:
        into[w["to"][0]].append(w)
    for c in cards.values():
        if not c["clock"]:
            continue
        for okey, _ in _feeds(c, cards, into):
            other = cards.get(okey)
            pair = frozenset((okey, c["key"]))
            if other and other["clock"] and other["clock"] != c["clock"] and pair not in seen:
                seen.add(pair)
                found.append(_problem(
                    "info", "Two clocks in one chain",
                    (f"{other['title']} → {c['title']}", "Two clocks, kept in step by "
                     "resampling (a few ms).", ""), [okey, c["key"]]))
    return found


def _feeds(card: dict, cards: dict, into: dict) -> set:
    """The hardware outputs whose sound reaches this card's inputs, through any pass-through."""
    found = set()
    for w in into.get(card["key"], ()):
        src = w["from"][0]
        if src in cards and _passes(cards[src]):
            found |= _origins(src, cards, into, {card["key"]})
        else:
            found.add(tuple(w["from"]))
    return found


def missing_devices(gone: list[dict], wires: list[dict]) -> list[dict]:
    """A device the board names that is not on the system: its wires wait for it."""
    found = []
    for m in gone:
        n = sum(m["key"] in (w["from"][0], w["to"][0]) for w in wires)
        if not n:
            continue  # nothing waits for it: dimmed on the board, no problem
        # In plain words by what it is (owner: "as a layman user I would be so confused"): what
        # happened, what it means for the sound, what to do.
        kept = "its connection waits" if n == 1 else f"its {n} connections wait"
        said = {
            "hardware": (f"{m['title']} isn't connected",
                         f"It's unplugged or turned off; {kept} for it and come back with it.",
                         "Plug it back in, or right-click its card to use another device."),
            "effect": (f"{m['title']} isn't running",
                       f"The effect didn't start, so sound doesn't pass through it; {kept}.",
                       "Right-click its card and start it."),
            "plugin": (f"{m['title']} isn't running",
                       f"The plugin didn't start, so sound doesn't pass through it; {kept}.",
                       "Right-click its card and start it."),
        }.get(m.get("kind"), (f"{m['title']} is gone",
                              f"It isn't on the system (removed, or PipeWire restarted); {kept}.",
                              "Right-click its card to create it again."))
        found.append({**_problem("warning", said[0], (m["title"], said[1], said[2]), [m["key"]]),
                      "id": f"missing:{m['key']}", "kind": "missing", "data": {"wires": n}})
    return found


def dropouts(model: dict, recent: dict[str, int]) -> list[dict]:
    """Dropouts (xruns) logged in the last minute, as one problem: a glitch of the interface shows
    on every app it reaches too (owner: "always duplicate"). The count is the highest of them."""
    # ponytail: the highest card's count, as if the others saw the same glitches; an app late on
    # its own is folded in. Telling them apart would need the cycle each was logged in.
    cards = {c["key"]: c for c in model["cards"]}
    hit = sorted((k for k, n in recent.items() if k in cards and n > 0), key=lambda k: -recent[k])
    if not hit:
        return []
    first = cards[hit[0]]["title"]
    return [{**_problem(
        "warning", "Sound dropped out",
        (", ".join(cards[k]["title"] for k in hit),
         f"{recent[hit[0]]} short gaps or clicks in the last minute: the computer didn't hand "
         "over the sound in time.",
         f"Give it more room: a bigger safety margin (the gear on {first})."), hit),
        "id": "dropouts", "kind": "dropouts", "data": {"minute": recent[hit[0]]}}]


def clipping(model: dict, sources: list[tuple]) -> list[dict]:
    """A sound card receiving sound at full scale, seen in the last moments (its monitor: the
    sum of all wired into it). Only there: inside PipeWire sound is float, so an output above full
    scale before a limiter is clean (owner: warnings on the nodes before their limiter); it clips
    on its way to the card, and two sources each under full scale can clip together there.
    ponytail: an app recording in fixed-point clips too; not watched yet (its own input format)."""
    cards = {c["key"]: c for c in model["cards"]}
    found = []
    for key, sock in sources:
        if sock != "hears" or key not in cards:
            continue
        found.append({**_problem(
            "warning", "Too loud",
            (cards[key]["title"], "It gets sound louder than it can play, so it distorts.",
             "Turn down what feeds it, or put a Limiter right before it."), [key]),
            "id": f"clipping:{key}", "kind": "clipping", "data": {}})
    return found


# A device must be missing this long before it is an event (owner: a restart "creates a ton of
# notifications that don't make sense"): at launch, or after PipeWire restarted, PipeBoard's own
# effects take a few seconds to start, and each was logged "isn't running".
MISSING_GRACE_S = 10.0


def settled(gone: list[dict], since: dict[str, float], now: float,
            grace: float = MISSING_GRACE_S) -> tuple[list[dict], float | None]:
    """The missing devices missing for `grace` seconds now (`since`, kept by the caller: when
    each was first seen missing; one back is forgotten), and when the next one would be."""
    keys = {m["key"] for m in gone}
    for k in [k for k in since if k not in keys]:
        del since[k]
    for k in keys:
        since.setdefault(k, now)
    due = [since[m["key"]] + grace for m in gone if now - since[m["key"]] < grace]
    return [m for m in gone if now - since[m["key"]] >= grace], min(due, default=None)


def not_installed(plugins: dict[str, str]) -> list[dict]:
    """Plugins on the board their host could not find on this computer: how things are, a
    problem, not an event at every launch (owner: "Calf Vinyl couldn't load" x6)."""
    return [_problem("warning", f"{name} isn't installed",
                     (name, "PipeBoard can't load this plugin: it isn't on this computer.",
                      "Install it, or remove its card."), [key])
            for key, name in sorted(plugins.items())]


def stopped(name: str, why: str, key: str, error: bool = False) -> dict:
    """An effect or plugin whose process ended by itself, or a plugin that would not load (not
    installed here): over at once, logged (Events). `error`: the host said why."""
    if "find the requested plugin" in why:  # (Carla: not installed here)
        said = (f"{name} couldn't load", "This plugin isn't installed on this computer.",
                "Install it, or remove its card.")
    elif error:
        said = (f"{name} couldn't start", f"{why[:1].upper()}{why[1:]}.",
                "Right-click its card and start it again.")
    else:
        said = (f"{name} stopped unexpectedly",
                "It quit by itself, so sound no longer passes through it.",
                "Right-click its card and start it again.")
    return {**_problem("error", said[0], (name, said[1], said[2]), [key]),
            "id": f"stopped:{key}", "kind": "stopped", "data": {"error": error}}


# ---- the event log (owner: "more of a problem log than a notification center, so they don't
# disappear"): what happened (dropouts, clipping, a device gone, an effect that stopped), kept for
# a week or 100 entries in the config folder. An event has a stable id (kind and where); happening
# again bumps its entry to the top with the new values; Seen hides it until it happens again.
KEEP_S, KEEP_N, SAVE_EVERY_S = 7 * 86400, 100, 30


class Events:
    """The log. `note(current, now)`: the events happening now (problem dicts with id, kind and
    data); `happened(event, now)`: one that is over at once (an effect that stopped)."""

    def __init__(self, path: Path | None = None):
        self.path, self.saved, self.dirty = path, 0.0, False
        self.items: dict[str, dict] = {}
        # Muted (owner: "clipping before my limiter: I know"): never shown, counted or told until
        # unmuted; kept apart from the entries, so a mute outlives the week an entry is kept.
        self.muted: set[str] = set()
        if path:
            try:
                found = json.loads(path.read_text())
                found = found if isinstance(found, dict) else {"entries": found}  # (the first form)
                self.items = {e["id"]: e for e in found.get("entries", [])}
                self.muted = set(found.get("muted", []))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.items, self.muted = {}, set()
            for e in self.items.values():  # (a restart: nothing is happening yet)
                e["active"] = False

    def _open(self, ev: dict, now: float, active: bool) -> None:
        e = self.items.get(ev["id"])
        again = e is None or not e["active"]
        e = e or {"id": ev["id"], "first": now, "count": 0, "seen": None, "peak": {}}
        if again:  # a new occurrence: counted, at the top, new again even if seen
            e["count"] += 1
            e["since"] = now
        data = ev.get("data") or {}
        peak = {k: max(v, e["peak"].get(k, v)) for k, v in data.items()
                if isinstance(v, int | float)}
        e.update({k: ev[k] for k in ("kind", "severity", "title", "where", "what", "fix", "cards")},
                 data=data, peak={**e["peak"], **peak}, last=now, active=active)
        self.items[ev["id"]] = e
        self.dirty = self.dirty or again

    def note(self, current: list[dict], now: float) -> None:
        for ev in current:
            self._open(ev, now, True)
        ids = {ev["id"] for ev in current}
        for e in self.items.values():
            if e["active"] and e["id"] not in ids:
                e["active"], e["ended"], self.dirty = False, now, True
        self._keep(now)

    def happened(self, ev: dict, now: float) -> None:
        self._open(ev, now, False)
        self.items[ev["id"]]["ended"] = now
        self._keep(now)

    def seen(self, ids: list[str] | None, now: float) -> None:
        """These entries (None: all) seen: hidden until they happen again."""
        for e in self.items.values():
            if ids is None or e["id"] in ids:
                e["seen"] = now
        self.dirty = True
        self._keep(now, save=True)

    def mute(self, ident: str, on: bool, now: float) -> None:
        (self.muted.add if on else self.muted.discard)(ident)
        self.dirty = True
        self._keep(now, save=True)

    def entries(self) -> list[dict]:
        """Newest occurrence first; `new`: not seen since it last began (never when muted)."""
        out = [{**e, "muted": e["id"] in self.muted,
                "new": e["id"] not in self.muted and (e["seen"] is None or e["seen"] < e["since"])}
               for e in self.items.values()]
        return sorted(out, key=lambda e: -e["since"])

    def _keep(self, now: float, save: bool = False) -> None:
        old = [i for i, e in self.items.items() if not e["active"] and now - e["last"] > KEEP_S]
        for i in old:
            del self.items[i]
        for e in sorted(self.items.values(), key=lambda e: -e["since"])[KEEP_N:]:
            del self.items[e["id"]]
        if self.path and (save or (self.dirty and now - self.saved > SAVE_EVERY_S) or old):
            self.save(now)

    def save(self, now: float) -> None:
        """Written whole, through a temporary file (a crash mid-write leaves the old one)."""
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"entries": list(self.items.values()),
                                       "muted": sorted(self.muted)}))
            os.replace(tmp, self.path)
            self.saved, self.dirty = now, False
        except OSError:
            pass
