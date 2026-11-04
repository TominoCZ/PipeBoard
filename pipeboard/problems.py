"""Find problems in a board model, worst first (PLAN §5a). A free socket is never a problem."""

from __future__ import annotations

from collections import defaultdict

ORDER = {"error": 0, "warning": 1, "info": 2}


def _passes(card: dict) -> bool:
    """Whether sound entering this card comes out of it: not for hardware jacks or apps."""
    return card["kind"] not in ("hardware", "app")


def _problem(severity: str, title: str, text: str, cards=(), wires=()) -> dict:
    return {"severity": severity, "title": title, "text": text,
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
                " → ".join(_name(cards, k) for k in ring) + ". Sound comes back into itself "
                "and builds up into a howl. Remove one of these wires.", path, loop_wires))
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
        f"PipeWire could not connect {_name(cards, w['from'][0])} to {_name(cards, w['to'][0])}. "
        "Often a format the two ends cannot agree on. Try removing and re-making the wire.",
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
                f"{card['title']} ({socket['label']}) gets sound on the {side} channel only; "
                f"the {other} is silent. Wire a source to the {other} too, or a mono source to "
                "the whole socket.", [key], into[(key, sock)]))
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
        found += [_problem(
            "warning", "Same sound arrives twice",
            f"{_name(cards, okey)} {_socket_label(cards, okey, osock)} reaches "
            f"{_name(cards, key)} by {len(ids)} routes, so it is louder than expected and may "
            "sound hollow or echo. Keep one.", [key, okey], ids)
            for (okey, osock), ids in counts.items() if len(ids) > 1]
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
                     f"There are {len(keys)} devices called {title}. Apps list them by name, so "
                     "it is easy to pick the wrong one.", keys)
            for title, keys in by.items() if len(keys) > 1]


def _resampled(cards: dict, clock: dict) -> list[dict]:
    rate = clock.get("rate")
    return [_problem(
        "info", "Converted to another sample rate",
        f"{c['title']} runs at {c['rate'] / 1000:g} kHz and the rest at {rate / 1000:g} kHz. "
        "PipeWire converts automatically, at the cost of a little CPU and latency.", [c["key"]])
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
                    f"Sound goes from {other['title']} to {c['title']}, which each keep their own "
                    "time. PipeWire keeps them in step by resampling, which works and adds a few "
                    "milliseconds.", [okey, c["key"]]))
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
        kept = "Its connection is kept" if n == 1 else f"Its {n} connections are kept"
        found.append(_problem(
            "warning", "Device missing",
            f"{m['title']} is not on the system. {kept} for when it does. "
            "Right-click it to use another device instead.", [m["key"]]))
    return found


def clipping(model: dict, sources: list[tuple]) -> list[dict]:
    """Sound at full scale on an output, seen by live wires in the last moments."""
    cards = {c["key"]: c for c in model["cards"]}
    found = []
    for key, sock in sources:
        card = cards.get(key)
        if not card:
            continue
        label = next((s["label"] for s in card["outputs"] if s["id"] == sock), "")
        where = f"{card['title']} ({label})" if len(card["outputs"]) > 1 else card["title"]
        found.append(_problem(
            "warning", "Clipping",
            f"{where} reaches full scale, so it distorts. Turn it down at its source.", [key]))
    return found
