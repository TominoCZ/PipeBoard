"""Turn a pw-dump snapshot into the board model: cards, sockets and wires (docs/PLAN.md §2)."""

from __future__ import annotations

from collections import defaultdict

NODE, PORT, LINK, DEVICE = (f"PipeWire:Interface:{t}" for t in ("Node", "Port", "Link", "Device"))
# Groups of nodes shown as one card, by node.name prefix: (prefixes, key, title, kind).
PREFIX_GROUPS = [
    (("easyeffects_sink", "ee_soe_", "ee_test_signals"), "easyeffects:out", "Easy Effects",
     "effect"),
    (("easyeffects_source", "ee_sie_"), "easyeffects:in", "Easy Effects Mic", "effect"),
]
HIDDEN_BY_DEFAULT = ("ee_",)
STEREO = ("FL", "FR")


def _props(obj: dict) -> dict:
    return (obj.get("info") or {}).get("props") or {}


def _group(props: dict) -> tuple[str, str | None, str | None]:
    """Return (group key, fixed title, fixed kind) for a node."""
    name = props.get("node.name", "")
    for prefixes, key, title, kind in PREFIX_GROUPS:
        if name.startswith(prefixes):
            return key, title, kind
    if props.get("pipeboard.id"):  # a plugin is two nodes (Carla's engine, the plugin): one card
        return "pb:" + props["pipeboard.id"], None, None
    if props.get("node.link-group"):
        return "lg:" + props["node.link-group"], None, None
    if props.get("device.id") and props.get("media.class", "").startswith("Audio/"):
        # One card per physical device: its outputs left, its inputs right, like a pedal.
        return f"dev:{props['device.id']}", None, None
    return name, None, None


def _clock(dump: list[dict]) -> dict:
    """The graph's one rate and buffer size, from the settings metadata."""
    found = {"rate": 48000, "quantum": 1024}
    for o in dump:
        settings = (o.get("props") or {}).get("metadata.name") == "settings"
        if o["type"].endswith("Metadata") and settings:
            for m in o.get("metadata") or []:
                key = m.get("key", "").removeprefix("clock.")
                if key in ("rate", "quantum", "force-rate", "force-quantum") and m.get("value"):
                    found[key.removeprefix("force-")] = int(m["value"])  # a forced one wins
    return found


def _rate(obj: dict) -> int | None:
    formats = ((obj.get("info") or {}).get("params") or {}).get("Format") or [{}]
    rate = formats[0].get("rate")
    return rate if isinstance(rate, int) else None


def _plumbing(p: dict) -> bool:
    """Not audio the user routes: level meters (Plasma's volume applet, pavucontrol: the Pulse
    side marks them stream.monitor / resample.peaks) and live wires' own capture."""
    flags = (p.get("stream.monitor"), p.get("resample.peaks"))
    return any(f in (True, "true", "1") for f in flags) or (p.get("node.name") or "").startswith(
        "pipeboard_scope") or "VolumeMeter" in (p.get("media.name") or "")


def _stream_key(props: dict, key: str, groups: dict) -> str:
    """An app stream's own key: its name (or its app's, when a stream has none), #2, #3… for
    more streams of the same name. Merging them made one card with dozens of sockets."""
    base = key or props.get("application.name") or props.get("media.name") or "stream"
    n, found = 1, base
    while found in groups:
        n += 1
        found = f"{base}#{n}"
    return found


def _audio_nodes(dump: list[dict]) -> dict[int, dict]:
    """Audio nodes by id; MIDI, video, drivers and level meters are not on the board at all."""
    nodes = {}
    for o in dump:
        p = _props(o)
        if _plumbing(p):
            continue
        if o["type"] == NODE and ("Audio" in (p.get("media.class") or "")
                                  or p.get("node.name", "").startswith("ee_")
                                  or p.get("pipeboard.owned")):  # a plugin: a JACK node, no class
            nodes[o["id"]] = {**p, "pb.rate": _rate(o)}
    return nodes


def _ports(dump: list[dict], nodes: dict[int, dict]) -> dict[int, dict]:
    ports = {}
    for o in dump:
        p = _props(o)
        if o["type"] == PORT and p.get("node.id") in nodes and "audio" in p.get("format.dsp", ""):
            direction = o["info"].get("direction") or p.get("port.direction")
            node = nodes[p["node.id"]]
            # A monitor copies what a node receives, except on a null-sink virtual source, whose
            # "monitors" are its only outputs.
            # PipeBoard's own speakers send their sound out through their monitor ports.
            # (Its own EQ's input is a stream: its monitors are echoes like anyone's.)
            cls = node.get("media.class", "")
            echo = bool(p.get("port.monitor")) and not cls.startswith("Audio/Source") and not (
                node.get("pipeboard.owned") and cls.startswith("Audio/"))
            ports[o["id"]] = {**p, "id": o["id"], "serial": int(node.get("object.serial", 0)),
                              "dir": "in" if direction in ("input", "in") else "out", "echo": echo,
                              "device": node.get("media.class", "").startswith("Audio/")}
    return ports


def _links(dump: list[dict], ports: dict[int, dict]) -> list[dict]:
    links = []
    for o in dump:
        info = o.get("info") or {}
        out, inp = info.get("output-port-id"), info.get("input-port-id")
        if o["type"] == LINK and out in ports and inp in ports:
            links.append({"id": o["id"], "out": out, "in": inp, "state": info.get("state")})
    return links


def _main(members: list[dict]) -> dict:
    """The device half of a loopback names it, not its stream half."""
    return next((p for p in members if p.get("media.class", "").startswith("Audio/")), members[0])


def _title(members: list[dict], fixed: str | None) -> str:
    if fixed:
        return fixed
    main = _main(members)
    # A whole device is named for the device, not for one of its profiles.
    device = ("api.alsa.card.name", "alsa.card_name", "device.description") if len(members) > 1 \
        and main.get("device.id") else ()
    for key in (*device, "node.description", "node.nick", "application.name", "media.name",
                "node.name"):
        if main.get(key):
            return main[key]
    return "?"


def _key(members: list[dict], group: str, devices: dict) -> str:
    """An identifier that survives restarts and renames: positions, names and wires hang on it.

    Hardware: PipeWire's device.name (bus, vendor, product, serial), never its display name.
    PipeBoard's own devices: their pipeboard.id. Everything else: its node.name.
    """
    main = _main(members)
    if main.get("pipeboard.id"):
        return "pb:" + main["pipeboard.id"]
    if group.startswith("dev:"):
        name = (devices.get(main.get("device.id")) or {}).get("device.name")
        return "device:" + (name or main.get("node.name", group))
    if group.startswith("lg:"):  # a loopback pair is named by its device half
        return main.get("node.name") or group
    return group  # node.name, or an app stream's own key (_stream_key), or easyeffects:…


def _kind(members: list[dict], fixed: str | None) -> tuple[str, str]:
    """Return (kind, role) for a card's member nodes."""
    if fixed:
        return fixed, ""
    classes = {p.get("media.class", "") for p in members}
    source = any(c.startswith("Audio/Source") for c in classes)
    sink = "Audio/Sink" in classes
    role = "interface" if source and sink else "mic" if source else "speaker" if sink else ""
    if members[0].get("pipeboard.kind"):
        return members[0]["pipeboard.kind"], role
    if any(p.get("device.api") or p.get("api.alsa.path") for p in members):
        return "hardware", role
    if len(members) > 1 or any(p.get("node.virtual") for p in members):
        return "virtual", role
    if any(c.startswith("Stream/") for c in classes):
        return "app", "playback" if any("Output" in c for c in classes) else "recording"
    return "virtual", role


def _stereo_pairs(ports: list[dict]) -> bool:
    chans = [p.get("audio.channel") for p in ports]
    return len(chans) % 2 == 0 and all(c == STEREO[i % 2] for i, c in enumerate(chans))


def _sockets(side: str, ports: list[dict]) -> list[dict]:
    """Group one side's ports into sockets with short labels (PLAN §2.2)."""
    word = "IN" if side == "in" else "OUT"
    if len(ports) == 2 or (len(ports) <= 8 and _stereo_pairs(ports)):
        groups = [ports[i:i + 2] for i in range(0, len(ports), 2)]
    else:
        groups = [[p] for p in ports]
    many = len(groups) > 1
    return [{
        "id": f"{side}{i + 1 if many else ''}",
        "label": f"{word} {i + 1}" if many else word,
        "ports": [p["id"] for p in g],
        "names": [p.get("port.name", "") for p in g],
    } for i, g in enumerate(groups)]


def _card_ports(ids: set[int], ports: dict[int, dict], peers: dict, prefix_group: bool) -> list:
    """Ports a card shows: not internal to its group, monitors only when used."""
    shown = []
    for p in ports.values():
        if p["node.id"] not in ids:
            continue
        outside = peers[p["id"]] - ids
        if peers[p["id"]] and not outside:
            continue  # wired only inside the group
        if p["echo"] and not outside:
            continue  # an unused monitor
        if prefix_group and not outside and not p["device"]:
            continue  # an idle part of a chain (an unused equalizer); the device half stays,
            # so Easy Effects Mic stays a mic on the board while its effects sleep
        shown.append(p)
    return sorted(shown, key=lambda p: (p["serial"], p.get("port.id", 0)))


def build(dump: list[dict]) -> dict:
    """Return the board model for a pw-dump snapshot."""
    nodes = _audio_nodes(dump)
    ports = _ports(dump, nodes)
    links = _links(dump, ports)
    peers = defaultdict(set)
    for link in links:
        peers[link["out"]].add(ports[link["in"]]["node.id"])
        peers[link["in"]].add(ports[link["out"]]["node.id"])
    groups: dict[str, list[int]] = defaultdict(list)
    fixed: dict[str, tuple] = {}
    for nid, props in sorted(nodes.items(), key=lambda kv: int(kv[1].get("object.serial", 0))):
        key, title, kind = _group(props)
        if props.get("media.class", "").startswith("Stream/") and not props.get(
                "node.link-group") and not props.get("pipeboard.id"):  # (an EQ's two streams)
            key = _stream_key(props, key, groups)  # one card per app stream, never merged
        groups[key].append(nid)
        fixed[key] = (title, kind)
    devices = {o["id"]: _props(o) for o in dump if o["type"] == DEVICE}
    seen = {"nodes": nodes, "ports": ports, "peers": peers, "devices": devices}
    every = _unique([_card(key, ids, seen, fixed[key]) for key, ids in groups.items()])
    cards = [c for c in every if c["inputs"] or c["outputs"]]
    # Everything on the system, shown or not: a device with nothing to show right now (Easy
    # Effects' mic chain while idle) is still there, not missing.
    present = sorted(c["key"] for c in every)
    return {"cards": cards, "wires": _wires(cards, links), "clock": _clock(dump),
            "present": present}


def _card(group: str, ids: list[int], seen: dict, fixed: tuple) -> dict:
    members = [seen["nodes"][i] for i in ids]
    kind, role = _kind(members, fixed[1])
    shown = _card_ports(set(ids), seen["ports"], seen["peers"], prefix_group=bool(fixed[1]))
    names = [m.get("node.name", "") for m in members]
    card = {
        "key": _key(members, group, seen["devices"]),
        "title": _title(members, fixed[0]),
        "kind": kind,
        "role": role,
        "names": names,
        "ids": ids,  # PipeWire node ids, for acting on PipeBoard's own devices
        # Never reused, unlike ids (a remade node got its old id back): tells a new node from the
        # one it replaces.
        "serials": [int(m.get("object.serial", 0)) for m in members],
        "inputs": _sockets("in", [p for p in shown if p["dir"] == "in"]),
        "outputs": _sockets("out", [p for p in shown if p["dir"] == "out"]),
        "hidden": any(n.startswith(HIDDEN_BY_DEFAULT) for n in names) and not fixed[1],
        "owned": any(m.get("pipeboard.owned") for m in members),
        # Hardware only: the rate it runs at, and its clock (two devices, two clocks).
        "rate": next((m["pb.rate"] for m in members if m["pb.rate"]), None) if kind == "hardware"
        else None,
        "clock": next((m["clock.name"] for m in members if m.get("clock.name")), None)
        if kind == "hardware" else None,
    }
    card["detail"] = f"{' + '.join(names[:3])}{' …' if len(names) > 3 else ''}"
    return card


def _unique(cards: list[dict]) -> list[dict]:
    """Make keys unique: two app streams may share a name."""
    seen: dict[str, int] = defaultdict(int)
    for c in cards:
        seen[c["key"]] += 1
        if seen[c["key"]] > 1:
            c["key"] = f"{c['key']}#{seen[c['key']]}"
    return cards


def _wires(cards: list[dict], links: list[dict]) -> list[dict]:
    """One wire per pair of sockets, standing for all the links between them."""
    where = {}
    for c in cards:
        for s in c["inputs"] + c["outputs"]:
            for i, pid in enumerate(s["ports"]):
                where[pid] = (c["key"], s["id"], i, len(s["ports"]))
    wires: dict[tuple, dict] = {}
    for link in links:
        a, b = where.get(link["out"]), where.get(link["in"])
        if not a or not b:
            continue
        w = wires.setdefault((a[:2], b[:2]), {
            "id": f"{a[0]}:{a[1]}>{b[0]}:{b[1]}",
            "from": list(a[:2]), "to": list(b[:2]), "links": [], "pairs": [], "failed": False,
            "ends": (set(), set()), "sizes": (a[3], b[3])})
        w["links"].append(link["id"])
        w["pairs"].append([a[2], b[2]])  # which channel of one socket feeds which of the other
        w["failed"] |= link["state"] == "error"
        w["ends"][0].add(a[2])
        w["ends"][1].add(b[2])
    for w in wires.values():
        _channel_tags(w)
    return list(wires.values())


def _channel_tags(w: dict) -> None:
    """Tag a wire that carries one side of a stereo socket: ch is L or R, at end 'from' or 'to'."""
    ends, sizes = w.pop("ends"), w.pop("sizes")
    w["fromPorts"], w["toPorts"] = sorted(ends[0]), sorted(ends[1])
    w["partial"] = len(w["links"]) < min(sizes)
    w["ch"], w["chEnd"] = "", ""
    for end, used, size in zip(("from", "to"), ends, sizes, strict=True):
        if size == 2 and len(used) == 1:
            w["ch"], w["chEnd"] = "LR"[next(iter(used))], end


def next_side(wires: list[dict], src) -> int:
    """A stereo output wired into a mono input gives one channel at a time, in order (user):
    L, unless L already goes somewhere on its own and R does not; then R."""
    alone = [w["fromPorts"] for w in wires if w["from"] == list(src) and len(w["fromPorts"]) == 1]
    return 1 if [0] in alone and [1] not in alone else 0


def pair(outs: list[int], ins: list[int]) -> list[tuple[int, int]]:
    """Which port links a wire between two sockets makes: L→L and R→R; mono fans out or sums."""
    if len(outs) == 1:
        return [(outs[0], i) for i in ins]
    if len(ins) == 1:
        return [(o, ins[0]) for o in outs]
    return list(zip(outs, ins, strict=False))



def _places(model: dict) -> tuple[dict, dict]:
    """Port → (card, socket, channel); (card, socket) → its ports."""
    where, socks = {}, {}
    for c in model["cards"]:
        for s in c["inputs"] + c["outputs"]:
            socks[(c["key"], s["id"])] = s["ports"]
            for i, p in enumerate(s["ports"]):
                where[p] = (c["key"], s["id"], i)
    return where, socks


def _passes(model: dict, through: set[str]) -> dict[int, list[int]]:
    """Through a card that is not there: its input k comes out at output k (the last input also
    feeds any extra outputs: a mono-in, stereo-out reverb)."""
    passes = {}
    for c in model["cards"]:
        ins = [p for s in c["inputs"] for p in s["ports"]]
        outs = [p for s in c["outputs"] for p in s["ports"]]
        if c["key"] not in through or not outs:
            continue
        for k, p in enumerate(ins):
            extra = outs[len(ins):] if k == len(ins) - 1 else []
            passes[p] = [outs[min(k, len(outs) - 1)], *extra]
    return passes


def around(model: dict, through: set[str]) -> list[dict]:
    """Wires that carry the sound around the cards in `through` (EQs, plugins) as if they were not
    there: every link into one, followed through it and through any others after it, to the first
    card that is not one. For when PipeBoard quits and they stop (user: quitting went
    silent; like Easy Effects turned off). Grouped as wires: {from, to, pairs}."""
    where, socks = _places(model)
    edges = [(socks[tuple(w["from"])][i], socks[tuple(w["to"])][j]) for w in model["wires"]
             if tuple(w["from"]) in socks and tuple(w["to"]) in socks for i, j in w["pairs"]]
    passes = _passes(model, through)

    def ends(port: int, seen: frozenset) -> list[int]:
        nxt = [i for out in passes.get(port, []) for o, i in edges if o == out]
        return [e for i in nxt for e in ([i] if where[i][0] not in through
                                          else ends(i, seen | {i}) if i not in seen else [])]

    wires: dict[tuple, set] = defaultdict(set)
    for o, i in edges:
        if where[o][0] not in through and where[i][0] in through:
            for end in ends(i, frozenset({i})):
                wires[(where[o][:2], where[end][:2])].add((where[o][2], where[end][2]))
    return [{"from": list(a), "to": list(b), "pairs": sorted(map(list, p))}
            for (a, b), p in wires.items()]
