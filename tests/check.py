"""Checks for the board model, against a trimmed pw-dump of the user's machine.

    ./run.sh check
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "fixtures")]
import asyncio  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402

import scenarios  # noqa: E402

from pipeboard import graph, problems, profiles, workspace  # noqa: E402

UR = "device:alsa_card.usb-Example_Studio_Interface-00"


def labels(card, side):
    return [s["label"] for s in card[side]]


def check_cards(cards):
    voice = cards["mic_mix"]
    assert (voice["title"], voice["kind"], voice["role"]) == ("Mic Mix", "virtual", "mic")
    assert labels(voice, "inputs") == ["IN"] and labels(voice, "outputs") == ["OUT"], voice
    assert len(voice["inputs"][0]["ports"]) == 2, "a loopback pair is one card with a stereo IN"
    ur = cards[UR]
    assert (ur["title"], ur["kind"], ur["role"]) == ("Studio Interface", "hardware", "interface")
    assert labels(ur, "inputs") == [f"IN {i}" for i in range(1, 7)], "one card: 6 outs on the left"
    assert labels(ur, "outputs") == [f"OUT {i}" for i in range(1, 9)], "and 8 inputs on the right"
    assert ur["outputs"][1]["names"] == ["capture_FR"], "jack 2 is capture_FR, as in the user's script"
    mic = cards["easyeffects:in"]
    assert labels(mic, "outputs") == ["OUT"], "Easy Effects Mic stays a mic while its effects sleep"
    ee = cards["easyeffects:out"]
    assert labels(ee, "inputs") == ["IN"] and labels(ee, "outputs") == ["OUT"], "ee_* chain is one card"
    assert "ee_test_signals" not in cards and not voice["hidden"], "EE's test tone is part of EE"
    assert not any("monitor" in n for c in cards.values() if c["kind"] != "effect"
                   for s in c["inputs"] + c["outputs"] for n in s["names"]), "unused monitors hidden"


def check_wires(wires):
    by = {(tuple(w["from"]), tuple(w["to"])): w for w in wires}
    voice = by[((UR, "out2"), ("mic_mix", "in"))]
    assert len(voice["links"]) == 2 and voice["ch"] == "" and not voice["partial"], "mono fans out"
    guitar_l = by[((UR, "out3"), ("instrument_mix", "in"))]
    assert (guitar_l["ch"], guitar_l["chEnd"]) == ("L", "to"), "jack 3 feeds the left of Instrument Mix"
    assert by[(("desktop_mix", "out"), ("easyeffects:out", "in"))]["links"], "speakers reach EE"


def check_pedal():
    """A pedal with USB in and out is one card, and an effect loop through it is no feedback."""
    model = graph.build(scenarios.load("pedal"))
    cards = {c["key"]: c for c in model["cards"]}
    pedal = cards["device:alsa_card.usb-Example_Guitar_Pedal-00"]
    assert labels(pedal, "inputs") == ["IN"] and labels(pedal, "outputs") == ["OUT"], pedal
    ends = {(w["from"][0], w["to"][0]) for w in model["wires"]}
    assert (UR, "device:alsa_card.usb-Example_Guitar_Pedal-00") in ends and ("device:alsa_card.usb-Example_Guitar_Pedal-00", "instrument_mix") in ends
    found = [(p["severity"], p["title"]) for p in problems.find(model)]
    assert found == [("info", "Converted to another sample rate"),
                     ("info", "Two clocks in one chain")], \
        f"an effect loop through hardware is no feedback loop, but costs resampling: {found}"
    assert pedal["rate"] == 48000 and model["clock"] == {"rate": 192000, "quantum": 1024}


def check_problems():
    assert problems.find(graph.build(scenarios.load("machine"))) == [], "the user's graph is fine"
    found = problems.find(graph.build(scenarios.load("problems")))
    titles = [p["title"] for p in found]
    assert titles == ["Feedback loop", "Connection failed", "Only one side of a stereo input"], titles
    assert set(found[0]["cards"]) == {"mic_mix", "desktop_mix"} and len(found[0]["wires"]) == 2


def check_virtual_source():
    """A null-sink virtual mic flags its outputs as monitors; they are still its outputs."""
    node = {"id": 1, "type": scenarios.NODE, "info": {"props": {
        "node.name": "lr_test", "media.class": "Audio/Source/Virtual", "object.serial": 1}}}
    ports = [{"id": 2 + i, "type": scenarios.PORT, "info": {"direction": "output", "props": {
        "node.id": 1, "port.name": f"capture_{ch}", "audio.channel": ch, "port.monitor": True,
        "format.dsp": "32 bit float mono audio"}}} for i, ch in enumerate(("FL", "FR"))]
    card = graph.build([node, *ports])["cards"][0]
    assert labels(card, "outputs") == ["OUT"], card


def check_gain_math():
    from pipeboard import gain

    path = gain.steps(-6.0, 6.0)
    assert path[-1] == 6.0 and len(path) == 120, "0.1 dB steps, ending exactly at the target"
    assert all(abs(b - a) <= 0.1 + 1e-9 for a, b in zip([-6.0, *path], path, strict=False)), \
        "never a bigger step"
    assert len(gain.steps(-30.0, 24.0)) == 200, "a long ramp is capped at 200 steps"
    assert gain.steps(0.0, -144.0)[-2:] == [-40.0, -144.0], "fine steps down to −40 dB, then the rest"
    assert gain.steps(-144.0, 0.0)[0] == -40.0 and gain.steps(-144.0, -60.0) == [-60.0]
    assert gain.clamp(99) == 24.0 and gain.clamp(-999) == -144.0 and gain.clamp(-99.94) == -99.9
    assert gain.clamp(3.55) == 3.6 and gain.clamp(None) == -144.0 and gain.clamp("-inf") == -144.0
    assert gain.clamp(float("nan")) == 0.0 and gain.clamp(float("-inf")) == -144.0
    assert abs(gain.factor(24.0) - 15.848932) < 1e-6 and gain.factor(0) == 1.0


def check_streams():
    """Level meters are not on the board; same-named or nameless app streams are separate apps."""
    nid = iter(range(5000, 6000))

    def stream(props):
        node = {"id": next(nid), "type": scenarios.NODE, "info": {"props": {
            "media.class": "Stream/Input/Audio", "object.serial": 1, **props}}}
        ports = [{"id": next(nid), "type": scenarios.PORT, "info": {"direction": "input", "props": {
            "node.id": node["id"], "port.name": f"input_{ch}", "audio.channel": ch,
            "format.dsp": "32 bit float mono audio"}}} for ch in ("FL", "FR")]
        return [node, *ports]

    dump = scenarios.load("machine")
    for _ in range(3):  # the Plasma volume applet's meters: nameless, marked as monitors
        dump += stream({"media.name": "PlasmaPA-VolumeMeter", "stream.monitor": True})
    dump += stream({"node.name": "Discord", "application.name": "Discord"})
    dump += stream({"node.name": "Discord", "application.name": "Discord"})
    dump += stream({"application.name": "OBS"})  # no node.name at all
    cards = {c["key"]: c for c in graph.build(dump)["cards"]}
    assert not any("Plasma" in c["title"] for c in cards.values()), "meters are not on the board"
    assert {"Discord", "Discord#2", "OBS"} <= set(cards) and "" not in cards, sorted(cards)
    assert all(cards[k]["kind"] == "app" for k in ("Discord", "Discord#2", "OBS"))
    board = profiles.snapshot(graph.build(dump), {"cards": {"": {"seen": {"kind": "virtual"}}}})
    assert "" not in board["cards"], "a card saved under an empty key is dropped"


def check_workspace():
    card = {}
    for name in ("Podcast", "Stream Mic", "Podcast", ""):
        workspace.rename(card, name)
    assert "nick" not in card and card["names"] == ["Podcast", "Stream Mic"], card
    ws = {"cards": {}}
    assert workspace.apply(ws, {"op": "forget-name", "key": "k", "name": "x"})
    assert not workspace.apply(ws, {"op": "rename"}), "no key, no change"
    assert graph.pair([1, 2], [3, 4]) == [(1, 3), (2, 4)] and graph.pair([1], [3, 4]) == [(1, 3), (1, 4)]
    assert graph.pair([1, 2], [3]) == [(1, 3), (2, 3)]


def check_profiles():
    """Saving, comparing, applying, and a device that went away."""
    dump = scenarios.load("machine")
    model = graph.build(dump)
    saved = profiles.snapshot(model, {"cards": {"mic_mix": {"x": 5, "y": 6, "nick": "Mic"}}})
    assert len(saved["wires"]) == 18 and saved["cards"]["mic_mix"]["nick"] == "Mic"
    assert profiles.plan(saved, model) == {"make": [], "remove": [], "missing": []}, "a board matches itself"
    music_player = {w["id"]: w for w in model["wires"]}["music_player:out>desktop_mix:in"]["links"]
    later = graph.build([o for o in dump if o["id"] not in music_player])
    todo = profiles.plan(saved, later)
    assert len(todo["make"]) == 2 and not todo["remove"], todo
    # The app's system wire going (the app closed, or WirePlumber moved it): not a change (user);
    # a wire the user made to it going: one.
    assert profiles.changes(saved, profiles.snapshot(later, saved)) == []
    mine = {**saved, "made": ["music_player:out>desktop_mix:in"]}
    assert profiles.changes(mine, profiles.snapshot(later, mine)) == ["− Music Player → Desktop Mix"]
    unplugged = profiles.snapshot(later, {**saved, "cut": ["music_player:out>desktop_mix:in"]})
    assert profiles.changes(saved, unplugged) == ["− Music Player → Desktop Mix"], "the user unplugged it"
    assert profiles.snapshot(model, unplugged)["cut"] == [], "back: no longer cut"
    # An app appearing, linked by the system: not a change; linked by the user: one.
    bare = profiles.snapshot(later, {"cards": {}})
    now = profiles.snapshot(model, bare)
    assert profiles.changes(bare, now) == [], profiles.changes(bare, now)
    now["made"] = ["music_player:out>desktop_mix:in"]
    assert profiles.changes(bare, now) == ["+ Music Player → Desktop Mix"], profiles.changes(bare, now)
    for bad in ("", "../etc", "a/b", ".hidden", "x" * 41):
        assert not profiles.valid_name(bad), bad
    assert profiles.valid_name("Studio (night)")


def check_missing():
    """The Pedal unplugged: still on the board, its wires kept, and it can be swapped for another."""
    pedal_board = profiles.snapshot(graph.build(scenarios.load("pedal")), {"cards": {}})
    model = graph.build(scenarios.load("machine"))
    board = profiles.snapshot(model, pedal_board)
    gone = profiles.missing(board, model)
    assert [m["title"] for m in gone] == ["Pedal"], gone
    pedal = gone[0]["key"]
    found = problems.missing_devices(gone, board["wires"])
    assert "Its 3 connections are kept" in found[0]["text"], found
    hidden = {**board, "cards": {**board["cards"], pedal: {**board["cards"][pedal], "hidden": True}}}
    assert profiles.missing(hidden, model) == [], "a card removed from the board is never missing"
    idle = {**model, "cards": [c for c in model["cards"] if c["key"] != "mic_mix"]}
    assert all(m["key"] != "mic_mix" for m in profiles.missing(board, idle)), \
        "on the system with nothing to show is not missing"
    lonely = [{**gone[0], "key": "device:nothing-wired"}]
    assert problems.missing_devices(lonely, board["wires"]) == [], "nothing waits: no problem"
    assert sum(pedal in (w["from"][0], w["to"][0]) for w in board["wires"]) == 3, "wires kept"
    cards = {c["key"]: c for c in model["cards"]}
    import copy
    swap = copy.deepcopy(board)
    assert profiles.retarget(swap, pedal, cards["easyeffects:out"]) == []
    assert profiles.plan(swap, model)["make"], "the swapped device gets the Pedal's wires"
    for other in ("music_player", UR):  # outputs only; six mono inputs instead of one stereo
        swap = copy.deepcopy(board)
        dropped = profiles.retarget(swap, pedal, cards[other])
        assert pedal not in str(swap["wires"]) and pedal not in swap["cards"]
        assert (other == "music_player") == bool(dropped), dropped
        profiles.plan(swap, model)  # never crashes on a socket count mismatch
        problems.find(model)


async def check_revert_all(page, next_state):
    """Revert undoes what the card lists, even a wire to a card the profile does not know and a
    PipeBoard device made since the save (it once left both behind)."""
    await page.send_json({"op": "create-device", "type": "gain", "name": "Since", "channels": 2,
                          "x": 0, "y": 0, "connect": {"from": ["music_player", "out"]}})
    for _ in range(6):
        state = await next_state()
        if any(c["title"] == "Since" for c in state["model"]["cards"]) and len(state["profile"]["changes"]) >= 2:
            break
    assert any("Since" in line for line in state["profile"]["changes"]), state["profile"]["changes"]
    await page.send_json({"op": "profile-revert"})
    for _ in range(6):
        state = await next_state()
        if not state["profile"]["changes"]:
            break
    assert state["profile"]["changes"] == [], state["profile"]["changes"]
    assert not any(c["title"] == "Since" for c in state["model"]["cards"]), "the device made since is gone"


async def check_profile_flow(page, next_state):
    """Save a profile, change the board, see the changes, revert them; launch setting; bad names."""
    await page.send_json({"op": "profile-save", "name": "Studio"})
    state = await next_state()
    assert state["profile"]["active"] == "Studio"
    await page.send_json({"op": "disconnect", "wire": "music_player:out>desktop_mix:in"})
    changes = (await next_state())["profile"]["changes"]
    assert changes == ["− Music Player → Desktop Mix"], changes
    await page.send_json({"op": "profile-plan", "name": "Studio"})
    while (plan := json.loads((await page.receive(timeout=3)).data))["type"] != "plan":
        pass
    assert plan["make"] == ["Music Player → Desktop Mix"] and plan["remove"] == [], plan
    await page.send_json({"op": "profile-revert"})
    for _ in range(3):  # the links come back a moment after the board does
        state = await next_state()
        if not state["profile"]["changes"]:
            break
    assert state["profile"]["changes"] == [], state["profile"]["changes"]
    await page.send_json({"op": "disconnect", "card": "music_player"})  # a card's Disconnect
    state = await next_state()
    assert not any("music_player" in (w["from"][0], w["to"][0]) for w in state["model"]["wires"])
    assert state["profile"]["changes"], "its wires count as changes"
    await page.send_json({"op": "profile-revert"})
    for _ in range(3):
        state = await next_state()
        if not state["profile"]["changes"]:
            break
    assert state["profile"]["changes"] == [], state["profile"]["changes"]
    await check_revert_all(page, next_state)
    await page.send_json({"op": "profile-launch", "name": "Studio"})
    assert (await next_state())["profile"]["launch"] == "Studio"
    await page.send_json({"op": "profile-save", "name": "../evil"})
    while (msg := json.loads((await page.receive(timeout=3)).data))["type"] != "toast":
        pass
    assert "profile name" in msg["text"]


def check_scope_math():
    import math
    from array import array

    from pipeboard import scope

    assert scope.level(1.0) == 127 and scope.level(-1.2) == -127 and scope.level(0.0) == 0
    assert 0 < scope.level(0.1) < scope.level(0.5) < 126 and scope.level(-0.5) == -scope.level(0.5)
    assert scope.level(float("nan")) == 0 and scope.level(float("inf")) == 127
    two = array("f", [0.5, -0.1] * scope.BUCKET * 2)  # L loud, R quiet, two buckets
    left, right = scope.peaks(two, 2)
    assert left == [0.5, 0.5] and abs(right[0] + 0.1) < 1e-6, "the strongest signed sample per ms"
    sine = [0.1 * math.sin(i / 3) for i in range(40)]
    shape = scope.scaled(sine)
    assert max(shape) == scope.level(max(sine)), "the peak sits where the dB scale puts it"
    gain = max(shape) / max(sine)
    assert all(abs(v - x * gain) <= 1 for v, x in zip(shape, sine, strict=True)), "one gain: shape kept"
    assert scope.scaled([0.0] * 40) == [] and scope.scaled([float("nan")] * 4) == []
    from collections import deque
    rings = [deque([0.0] * 96, maxlen=96), deque([0.0] * 95 + [1.0], maxlen=96)]
    frame, clipping = scope.encode(rings, [[0], [1]], new=40, total=1000)
    assert int.from_bytes(frame[:4], "little") == 1000, "frames carry the running count"
    assert frame[4:6] == bytes([1, 40]) and len(frame) == 47 and clipping == {1}, "only new, only sound"
    # Brightness follows the dominant sound (owner): a kick low, a hi-hat high, a voice between;
    # a kick with a quiet hi-hat over it stays near the kick.
    sine = lambda hz, a=0.5: array("f", [a * math.sin(2 * math.pi * hz * k / scope.RATE) for k in range(1600)])  # noqa: E731
    tone = lambda s: scope.bright(scope.bands(s, 1, [[0.0, 0.0]])[0])  # noqa: E731
    kick, hat, voice = tone(sine(70)), tone(sine(4000 - 1)), tone(sine(500))
    mix = tone(array("f", [b + t for b, t in zip(sine(70), sine(3000, 0.08), strict=True)]))
    assert kick < voice < hat, (kick, voice, hat)
    assert mix - kick < (hat - kick) / 4, (kick, mix, hat)
    assert scope.bright([(0.0, 0.0, 0.0)]) == 0
    assert scope.encode(rings, [[0]], new=40, total=1040) == ((1040).to_bytes(4, "little"), set()), \
        "silence sends only the count, so the page keeps time"
    assert scope.encode(rings, [[1]], new=0, total=1040)[0] == b"", "nothing new, nothing sent"


async def check_rename_stale():
    """Renames: on the board at once; at the next start a speaker is made again under its new
    name, wired before its old node goes; and if that cannot be done, a snapshot still showing
    the old node does not count as its return."""
    from pipeboard import devices, pipewire, server

    with tempfile.TemporaryDirectory() as tmp:
        board = server.Board(pipewire.Fixture(scenarios.load("machine")), Path(tmp))
        await board.refresh()
        for kind in ("gain", "speaker"):
            op = {"type": kind, "name": kind.title(), "channels": 2, "x": 0, "y": 0}
            assert await board.op_create_device(None, op) is None
        for _ in range(3):
            await board.refresh()
        keys = {d["type"]: k for k, d in board.ws["devices"].items()}
        ids = board.owned(keys["gain"])["ids"]
        assert await board.op_rename_device(None, {"key": keys["gain"], "name": "Quiet"}) is None
        await board.refresh()
        assert board.owned(keys["gain"])["ids"] == ids and board.owned(keys["gain"])["title"] == "Quiet", \
            "a gain is renamed in place: apps never see it, and remaking it cut the sound"
        # A speaker (apps see its name) is made again, wired like the old one before that goes:
        # a player on it moves over instead of losing its device. Not an app's links: WirePlumber
        # moves only its own, and a copy pinned the app (switching its device did nothing).
        key = keys["speaker"]
        cards = {c["key"]: c for c in board.model["cards"]}
        spk, app, mic = board.owned(key), cards["music_player"], cards["mic_mix"]
        app_wire = ([app["key"], app["outputs"][0]["id"]], [key, spk["inputs"][0]["id"]])
        assert await board.connect(*app_wire) is None  # the user's own wire to an app
        assert await board.connect([key, spk["outputs"][0]["id"]], ["mic_mix", mic["inputs"][0]["id"]]) is None
        await board.refresh()
        old, dump = board.owned(key)["ids"], board.source.dump

        def linked(src, dst):  # links from node src (by key's ids) into node dst, in the dump now
            nodes = {o["id"]: o["info"]["props"] for o in dump if o["type"].endswith("Node")}
            ports = {o["id"]: nodes.get(o["info"]["props"]["node.id"], {}) for o in dump
                     if o["type"].endswith("Port")}
            return sum(src(ports[o["info"]["output-port-id"]]) and dst(ports[o["info"]["input-port-id"]])
                       for o in dump if o["type"].endswith("Link"))

        mine = lambda p: p.get("pipeboard.id") == board.ws["devices"][key]["id"]  # noqa: E731
        assert await board.op_rename_device(None, {"key": key, "name": "Speaker 2"}) is None
        await board.refresh()
        assert board.owned(key)["ids"] == old and board.owned(key)["title"] == "Speaker 2", \
            "renamed on the board only: remade now, Spotify paused itself"
        await board.apply_names()  # the next start: apps get the new name
        assert not any(o["id"] in old for o in dump), "the old node is gone"
        assert linked(mine, lambda p: str(p.get("node.name")).startswith("mic_mix")) == 2, \
            "wired before it went"
        assert linked(lambda p: p.get("node.name") == "music_player", mine) == 0, "no app link copied"
        for _ in range(2):
            await board.refresh()
        assert board.owned(key)["title"] == "Speaker 2" and key not in board.waiting
        wires = {(w["from"][0], w["to"][0]) for w in board.model["wires"]}
        assert ("music_player", key) in wires and (key, "mic_mix") in wires, "the user's wires kept"
        # If the new node's ports do not come, it is wired when it appears; a snapshot still
        # showing the old node must not count as that (its wires were forgotten, the sound stopped).
        stale = board.model
        real, devices.moved_links = devices.moved_links, lambda *a: None
        try:
            assert await board.op_rename_device(None, {"key": key, "name": "Speaker 3"}) is None
            await board.apply_names()
        finally:
            devices.moved_links = real
        board.model = stale  # taken before the old node went
        assert not await board.wire_arrivals() and key in board.waiting, "the old node is not it"
        for _ in range(3):
            await board.refresh()
        assert key not in board.waiting and board.owned(key)["title"] == "Speaker 3"
        assert ("music_player", key) in {(w["from"][0], w["to"][0]) for w in board.model["wires"]}
        # An app route the user did not make (WirePlumber's) is never made here: it would pin it.
        board.mark(*app_wire, made=False)
        assert await board.op_rename_device(None, {"key": key, "name": "Speaker 4"}) is None
        await board.apply_names()
        for _ in range(3):
            await board.refresh()
        assert ("music_player", key) not in {(w["from"][0], w["to"][0]) for w in board.model["wires"]}


async def check_scope_follows():
    """Sources come and go in the running capture: gaps reused, channels freed, links made."""
    from pipeboard import scope

    links = []

    class Linked(scope.FakeScope):
        async def _pw_link(self, *args):
            links.append(args)
            return True

    a, b, c = ("a", "out", [1, 2]), ("b", "out", [3, 4]), ("c", "out", [5, 6, 7])
    cap = Linked([a, b])
    await cap.start()
    assert cap.capacity == 4 + scope.SPARE and cap.groups == [[0, 1], [2, 3]]
    assert await cap.update([b, c])
    assert cap.sources[0][0] == "c" and cap.sources[1][0] == "b", "a's place goes to the new one"
    assert ("-d", "1", f"{cap.name}:input_AUX0") in links and ("5", f"{cap.name}:input_AUX4") in links
    assert sorted(cap.free + cap.groups[0] + cap.groups[1]) == list(range(cap.capacity))
    assert not await cap.update([("big", "out", list(range(100, 100 + scope.MAX_CHANNELS)))]), \
        "what does not fit asks for a fresh capture"
    await cap.stop()


async def check_scope_failures():
    """Live wires failing in every way leaves the board serving, with plain wires."""
    import os

    from pipeboard import pipewire, scope, server

    class LiveLike(pipewire.Fixture):
        mode = "live"  # so the board uses the real capture, which will fail

    class Dies(scope.FakeScope):
        async def _run(self, n):
            raise RuntimeError("capture died")

    import logging
    logging.disable(logging.CRITICAL)  # the failures below are logged on purpose
    with tempfile.TemporaryDirectory() as tmp:
        board = server.Board(LiveLike(scenarios.load("machine")), Path(tmp))
        await board.refresh()
        path, os.environ["PATH"] = os.environ["PATH"], tmp  # no pw-cat to be found
        try:
            await board.op_live(object(), {"visible": True})
            assert board.scope is not None and board.scope.failed, "start failed, quietly"
            for _ in range(10):
                await board.send_frame()  # retries a few times, never raises
        finally:
            os.environ["PATH"] = path
        board.scope = Dies(board.scope_sources())
        await board.scope.start()
        await asyncio.sleep(0.01)
        assert not board.scope.alive
        await board.send_frame()
        junk = scope.FakeScope(board.scope_sources())
        junk.rings[0].extend([300.0, -999.0, float("nan"), float("inf")])  # nonsense must still encode
        junk.total += 4
        junk.frame()
        assert json.loads(board.state())["model"]["cards"], "the board still serves"
        await board.stop_scope()
        await check_scope_restarts(Path(tmp))
    logging.disable(logging.NOTSET)


async def check_scope_restarts(tmp):
    """A stalled capture is replaced; many changes at once leave exactly one capture."""
    from pipeboard import pipewire, scope, server

    # A capture that quietly stops delivering is replaced within ~2 s, whatever the cause.
    fixture = server.Board(pipewire.Fixture(scenarios.load("machine")), tmp / "f")
    await fixture.refresh()
    class Page:
        async def send_bytes(self, _):
            pass

        async def send_str(self, _):
            pass

    await fixture.op_live(Page(), {"visible": True})
    stalled = fixture.scope
    await asyncio.sleep(0.1)
    stalled.task.cancel()  # still "running", no longer producing
    for _ in range(60):
        await fixture.send_frame()
        await asyncio.sleep(0.05)
    assert fixture.scope is not stalled and fixture.scope.alive, "a stalled capture is restarted"
    await fixture.stop_scope()
    # Twenty changes at once leave exactly one capture running (overlapping restarts leaked).
    running = []

    class Counted(scope.FakeScope):
        async def _start(self):  # like a real capture, starting and stopping take a moment
            await asyncio.sleep(0.01)
            running.append(self)
            await super()._start()

        async def stop(self):
            if self in running:
                running.remove(self)
            await asyncio.sleep(0.01)
            await super().stop()

    real, scope.FakeScope = scope.FakeScope, Counted
    try:
        busy = server.Board(pipewire.Fixture(scenarios.load("machine")), tmp / "b")
        await busy.refresh()
        page = Page()
        await busy.op_live(page, {"visible": True})
        first = busy.scope
        await asyncio.gather(*(busy.act(page, {"op": "hide", "key": "music_player", "hidden": i % 2 == 0})
                               for i in range(20)))
        assert len(running) == 1 and running[0] is busy.scope, f"{len(running)} captures running"
        assert busy.scope is first, "a wire coming and going keeps the capture (and every wire's sound)"
        await busy.stop_scope()
    finally:
        scope.FakeScope = real


async def check_devices(page, next_state):
    """Make a mic, wire it, rename it (same card, wire back), refuse to delete the user's own mix,
    bring it back from a profile, delete it."""
    async def until(test, tries=8):
        for _ in range(tries):
            state = await next_state()
            if test(state):
                return state
        raise AssertionError("never got there")

    await page.send_json({"op": "create-device", "type": "mic", "name": "Stream Mic", "channels": 2,
                          "x": 100, "y": 100})
    state = await until(lambda s: any(c["key"].startswith("pb:") for c in s["model"]["cards"]))
    mic = next(c for c in state["model"]["cards"] if c["key"].startswith("pb:"))
    assert (mic["title"], mic["role"], mic["owned"]) == ("Stream Mic", "mic", True), mic
    assert [s["label"] for s in mic["inputs"]] == ["IN"] and [s["label"] for s in mic["outputs"]] == ["OUT"]
    key, wire = mic["key"], f"{UR}:out2>{mic['key']}:in"
    await page.send_json({"op": "connect", "from": [UR, "out2"], "to": [key, "in"]})
    await until(lambda s: any(w["id"] == wire for w in s["model"]["wires"]))
    await page.send_json({"op": "rename-device", "key": key, "name": "Podcast Mic"})
    state = await until(lambda s: any(c["title"] == "Podcast Mic" for c in s["model"]["cards"])
                        and any(w["id"] == wire for w in s["model"]["wires"]))
    assert state["workspace"]["cards"][key]["names"][0] == "Stream Mic", "the old name is kept"
    await page.send_json({"op": "delete-device", "key": "mic_mix"})
    while (msg := json.loads((await page.receive(timeout=3)).data))["type"] != "toast":
        pass
    assert "only deletes devices it made" in msg["text"]
    await page.send_json({"op": "profile-save", "name": "WithMic"})
    await next_state()
    await page.send_json({"op": "delete-device", "key": key})
    await until(lambda s: key not in {c["key"] for c in s["model"]["cards"]} and not s["workspace"]["devices"])
    await page.send_json({"op": "profile-load", "name": "WithMic"})
    await until(lambda s: any(w["id"] == wire for w in s["model"]["wires"]))  # made again, wired again
    await page.send_json({"op": "delete-device", "key": key})
    await until(lambda s: key not in {c["key"] for c in s["model"]["cards"]})


async def check_gain(board, page, next_state):
    """Make a gain, move it to +6 dB: ramped on the node, saved, no graph re-read per step; made
    again, it comes back at +6 dB at once."""
    await page.send_json({"op": "create-device", "type": "gain", "name": "Gain", "channels": 2,
                          "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if c["title"] == "Gain"), None)
        if card:
            break
    assert card and card["kind"] == "effect" and card["owned"], card
    assert [s["label"] for s in card["outputs"]] == ["OUT"], "a gain's output is its monitor"
    key, node = card["key"], card["ids"][0]
    await asyncio.sleep(0.6)  # let the re-reads that making it caused finish first
    reads = board.source.snapshot
    calls = []
    board.source.snapshot = lambda: (calls.append(1), reads())[1]
    await page.send_json({"op": "set-gain", "key": key, "db": 6})
    await asyncio.sleep(0.6)
    board.source.snapshot = reads
    sent = [db for n, db in board.gains.sent if n == node]
    assert sent[-1] == 6.0 and len(sent) >= 60, f"ramped to +6 dB in small steps ({len(sent)})"
    assert not calls, "moving a slider does not re-read the graph"
    assert board.ws["devices"][key]["gain_db"] == 6.0
    board.gains.sent.clear()
    await page.send_json({"op": "rename-device", "key": key, "name": "Boost"})  # in place
    for _ in range(8):
        state = await next_state()
        if any(c["title"] == "Boost" for c in state["model"]["cards"]):
            break
    await asyncio.sleep(0.1)
    boost = next(c for c in state["model"]["cards"] if c["title"] == "Boost")
    assert boost["ids"] == [node] and not board.gains.sent, "the same node, its level untouched"
    # Its power button: bypassed it passes at 0 dB, its level kept; a level set meanwhile is only
    # remembered; on again, back to the level.
    for op, want in (({"op": "gain-bypass", "key": key, "on": True}, 0.0),
                     ({"op": "set-gain", "key": key, "db": -3}, 0.0),
                     ({"op": "gain-bypass", "key": key, "on": False}, -3.0)):
        await page.send_json(op)
        await asyncio.sleep(0.5)
        assert board.gains.now[node] == want, (op, board.gains.now[node])
    assert board.ws["devices"][key]["gain_db"] == -3.0 and not board.ws["devices"][key]["bypass"]
    await page.send_json({"op": "delete-device", "key": key})
    await next_state()


async def check_takeover():
    """The user's seven mixes, made by virtual-devices.service: offered, taken over,
    made again as PipeBoard's own under the same names, wires and all."""
    from pipeboard import pipewire, server

    dump = scenarios.load("machine")
    unit, fixture = "virtual-devices.service", None
    loopbacks = [o for o in dump if o["type"] == scenarios.NODE
                 and o["info"]["props"].get("node.group", "").startswith("pw-loopback")]
    clients = {o["info"]["props"]["client.id"] for o in loopbacks}
    dump += [{"id": c, "type": "PipeWire:Interface:Client",
              "info": {"props": {"application.process.id": 40000 + c}}} for c in clients]
    fixture = pipewire.Fixture(dump)
    fixture.units = {40000 + c: unit for c in clients}
    with tempfile.TemporaryDirectory() as tmp:
        board = server.Board(fixture, Path(tmp))
        board.unit_of = fixture.units.get
        await board.refresh()
        offered = json.loads(board.state())["takeover"]
        assert sorted(offered[unit]) == sorted(["Desktop Mix", "Music Mix", "Mic Mix", "Instrument Mix",
                                                "Mic Instrument Mix", "Stream Mix",
                                                "Input1 Mix"]), offered
        assert await board.op_take_over(None, {"op": "take-over", "unit": unit}) is None
        for _ in range(6):
            await asyncio.sleep(0.2)
            await board.refresh()
        cards = {c["title"]: c for c in board.model["cards"]}
        mine = [c for c in cards.values() if c["owned"]]
        assert len(mine) == 7 and all(c["key"].startswith("pb:") for c in mine), sorted(cards)
        assert "mic_mix" in cards["Mic Mix"]["names"], "same node.name: apps keep finding it"
        voice, system = cards["Mic Mix"]["key"], cards["Desktop Mix"]["key"]
        assert any(w["from"][0] == UR and w["to"][0] == voice for w in board.model["wires"])
        assert any(w["from"][0] == system and w["to"][0] == "easyeffects:out" for w in board.model["wires"])
        # An app's route is WirePlumber's (it finds the device by its node.name): a link made here
        # would pin the app, and switching its device would do nothing.
        assert not any(w["from"][0] == "music_player" and w["to"][0] == system
                       for w in board.model["wires"])
        assert json.loads(board.state())["takeover"] == {}, "nothing left to take over"


async def check_insert(page, next_state):
    """A wire dropped on empty board makes a gain connected to it; a gain inserted into the
    left-only jack 3 → Instrument Mix wire gives jack 3 → Gain → Instrument Mix, still left-only, and the
    direct wire goes."""
    async def until(test, tries=10):
        for _ in range(tries):
            state = await next_state()
            if test(state):
                return state
        raise AssertionError("never got there")

    await page.send_json({"op": "create-device", "type": "gain", "name": "Dropped", "channels": 2,
                          "x": 0, "y": 0, "connect": {"from": ["music_player", "out"]}})
    state = await until(lambda s: any(c["title"] == "Dropped" for c in s["model"]["cards"])
                        and any(w["from"][0] == "music_player" and w["to"][1] == "in"
                                and w["to"][0].startswith("pb:") for w in s["model"]["wires"]))
    dropped = next(c["key"] for c in state["model"]["cards"] if c["title"] == "Dropped")
    jack3 = f"{UR}:out3>instrument_mix:in"
    await page.send_json({"op": "insert-device", "wire": jack3, "type": "gain", "name": "Inserted",
                          "channels": 2, "x": 0, "y": 0})

    def inserted(s):
        gain = next((c["key"] for c in s["model"]["cards"] if c["title"] == "Inserted"), None)
        wires = {(w["from"][0], w["to"][0]): w for w in s["model"]["wires"]}
        return gain and (UR, gain) in wires and (gain, "instrument_mix") in wires \
            and jack3 not in {w["id"] for w in s["model"]["wires"]}

    state = await until(inserted)
    gain = next(c["key"] for c in state["model"]["cards"] if c["title"] == "Inserted")
    wires = {(w["from"][0], w["to"][0]): w for w in state["model"]["wires"]}
    assert wires[(UR, gain)]["toPorts"] == [0] and wires[(gain, "instrument_mix")]["toPorts"] == [0], \
        "the left stays the left, through the gain"
    for key in (dropped, gain):
        await page.send_json({"op": "delete-device", "key": key})
        await next_state()


async def message(page, kind: str) -> dict:
    """The next message of one type from the server, skipping others."""
    while (msg := json.loads((await page.receive(timeout=3)).data))["type"] != kind:
        pass
    return msg


async def check_plugins(board, page, next_state):
    """A plugin from the picker: one card from its two nodes, named as on the board; Bypass and
    the editor reach its host and Bypass is saved; a value moved in its own window is saved; a
    rename stays on the board; a profile line; deleting it stops its host."""
    await page.send_json({"op": "plugin-list"})
    hum = next(p for p in (await message(page, "plugins"))["list"] if p["name"] == "Hum Remover")
    await page.send_json({"op": "create-device", "type": "plugin", "plugin": "vst3:/etc/evil.so:x",
                          "x": 0, "y": 0})
    await message(page, "toast")
    assert all(d["type"] != "plugin" for d in board.ws["devices"].values()), "only listed ones load"
    await page.send_json({"op": "create-device", "type": "plugin", "plugin": hum["ref"], "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if c["kind"] == "plugin"), None)
        if card and state["workspace"]["devices"][card["key"]].get("params"):
            break
    key, dev = card["key"], state["workspace"]["devices"][card["key"]]
    assert card["title"] == "Hum Remover" and len(card["ids"]) == 2, card
    assert [s["label"] for s in card["inputs"] + card["outputs"]] == ["IN", "OUT"], "stereo in, out"
    assert dev["editor"] and dev["bypassable"], dev
    sent = []
    board.plugins.send = lambda k, m: sent.append((k, m))
    await page.send_json({"op": "plugin-bypass", "key": key, "on": True})
    await page.send_json({"op": "plugin-editor", "key": key, "on": True})
    board.heard(key, {"param": [2, 0.3]})  # moved in the plugin's own window
    await page.send_json({"op": "rename-device", "key": key, "name": "Guitar Clean"})
    for _ in range(8):
        state = await next_state()
        dev = state["workspace"]["devices"][key]
        if dev["name"] == "Guitar Clean" and dev.get("bypass"):
            break
    assert sent == [(key, {"bypass": True}), (key, {"editor": True})], sent
    assert dev["params"][2]["value"] == 0.3, "what its window set is saved"
    assert "Made the plugin Guitar Clean" in state["profile"]["changes"], state["profile"]["changes"]
    board.heard(key, {"param": [dev["switch"]["i"], dev["switch"]["off"]]})  # un-bypassed in its window
    await page.send_json({"op": "place", "cards": {key: {"x": 14, "y": 14}}})
    assert not (await next_state())["workspace"]["devices"][key]["bypass"], "the card follows its window"
    del board.plugins.send
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert not any(c["kind"] == "plugin" for c in state["model"]["cards"]), "its nodes are gone"


async def check_plugin_startup():
    """Plugins end with PipeBoard: the next launch starts the board's again, with their saved
    values and bypass."""
    from pipeboard import pipewire, plugins, server, workspace

    with tempfile.TemporaryDirectory() as tmp:
        dev = dict(plugins.new(plugins.FAKE_INSTALLED[3]), id="dkeep", node="pb-dkeep", bypass=True,
                   params=[{"i": 0, "name": "Strength", "min": 0, "max": 1, "def": 0.6, "value": 0.9}])
        workspace.save(Path(tmp) / "workspace.json", {"cards": {}, "devices": {"pb:dkeep": dev}})
        board = server.Board(pipewire.Fixture(scenarios.load("machine")), Path(tmp))
        started = []
        start = board.plugins.start
        board.plugins.start = lambda key, d: (started.append((key, d)), start(key, d))[1]
        await server.startup(board)
        await board.refresh()
        assert [k for k, _ in started] == ["pb:dkeep"], started
        assert started[0][1]["params"][0]["value"] == 0.9 and started[0][1]["bypass"]
        assert any(c["key"] == "pb:dkeep" for c in board.model["cards"]), "its card is back"


def check_eq_math():
    """The EQ's curve: each band type where it should be; nonsense from the page made safe."""
    from pipeboard import eq

    rate = 192000

    def at(bands, freq, bypass=False):
        return round(eq.response(eq.slots(eq.clean(bands), rate, bypass), freq, rate), 1)

    bell = {"type": "bell", "freq": 1000, "gain": 6, "q": 1}
    assert at([bell], 1000) == 6.0 and abs(at([bell], 100)) < 0.2 and at([], 1000) == 0.0
    assert at([bell], 1000, bypass=True) == 0.0, "bypass is flat"
    for slope in (12, 24, 48):
        cut = {"type": "low-cut", "freq": 100, "slope": slope}
        assert at([cut], 100) == -3.0 and abs(at([cut], 50) + slope) < 0.5, (slope, at([cut], 50))
    assert at([{"type": "high-cut", "freq": 10000, "slope": 24}], 10000) == -3.0
    assert at([{"type": "low-shelf", "freq": 200, "gain": 6, "q": 0.71}], 20) == 6.0
    assert at([{"type": "high-shelf", "freq": 4000, "gain": -6, "q": 0.71}], 18000) == -6.0
    assert at([{"type": "notch", "freq": 1000, "q": 4}], 1000) < -100
    assert eq.clean([{"type": "bell", "freq": "nan", "gain": 99, "q": 0}, {"type": "evil"}]) == [
        {"type": "bell", "freq": 1000, "gain": 18.0, "q": 0.1, "on": True, "dyn": False, "thr": -24.0,
         "range": -6.0, "attack": 10.0, "release": 150.0}], "clamped, unknown dropped"
    assert len(eq.clean([{"type": "low-cut", "slope": 48}] * 5)) == 3, "no more than the slots hold"
    assert eq.slots([], rate)[0] == eq.FLAT and len(eq.slots([], rate)) == eq.SLOTS


def check_eq_range():
    """PipeWire clamps every coefficient to ±10 (a +16 dB high shelf once came out +28 dB in the
    lows). Every band type, all over its range, at every common rate: inside ±10, and the chain
    (with its makeup gain) gives exactly what the bands ask for."""
    from pipeboard import eq

    worst = 0.0
    for r in (44100, 48000, 96000, 192000):
        for kind in eq.TYPES:
            for f in (20, 60, 200, 1000, 4000, 9000, 16000, 20000):
                for g in (-18, -6, 6, 14, 18):
                    for q in (0.1, 0.7, 4, 18):
                        bands = eq.clean([{"type": kind, "freq": f, "gain": g, "q": q, "slope": 48}] * 3)
                        cs = eq.slots(bands, r)
                        worst = max(worst, max(abs(v) for c in cs for v in c))
                        for ff in (50, 500, 5000):
                            want = sum(eq.response([eq.biquad(b["type"], b["freq"], b["gain"], qq, r)], ff, r)
                                       for b in bands for qq in eq._sections(b))
                            assert abs(eq.response(cs, ff, r) - want) < 0.01 or want < -100, (r, kind, f, g, q, ff)
    assert worst < 10, f"a coefficient of {worst:.2f}: PipeWire clamps it to ±10"


def check_eq_dynamic():
    """Dynamic bands (M15): settings clamped; the processor's curve points (input, output) for a
    cut and a boost; DYN_SLOTS stages after the rows, at rest unless used; only the stages that
    change are sent; a 5th dynamic band waits for a free stage; cuts and notches never dynamic."""
    import asyncio as aio

    from pipeboard import eq

    b = eq.clean([{"type": "bell", "freq": 1000, "gain": 0, "q": 1, "dyn": True, "thr": -99,
                   "range": 40, "attack": 0, "release": 1e9}])[0]
    assert (b["thr"], b["range"], b["attack"], b["release"]) == (-60.0, 18.0, 0.1, 2000.0), b
    g = lambda db: round(10 ** (db / 20), 6)  # noqa: E731
    cut = eq.dyn_curve({"thr": -30.0, "range": -6.0, "attack": 10.0, "release": 150.0})
    assert (cut["Threshold 0 (G)"], cut["Gain 0 (G)"]) == (g(-30), g(-30))
    assert (cut["Threshold 1 (G)"], cut["Gain 1 (G)"]) == (g(-21), g(-27)), cut  # ratio 3: 9 dB in, 3 out
    boost = eq.dyn_curve({"thr": -30.0, "range": 6.0, "attack": 10.0, "release": 150.0})
    assert (boost["Threshold 0 (G)"], boost["Gain 0 (G)"]) == (g(-39), g(-33)), boost
    bands = eq.clean([{"type": t, "freq": 1000, "q": 1, "dyn": True}
                      for t in ("bell", "low-cut", "notch", "high-shelf", "bell", "bell", "bell")])
    assert [x["type"] for x in eq.dynamic(bands)] == ["bell", "high-shelf", "bell", "bell"]
    dev = {**eq.new(), "bands": bands}
    conf = eq.conf(dev, 48000)
    assert conf.count("dyna_processor_mono") == eq.DYN_SLOTS and '"dmix3:Out"' in conf
    sent = []
    async def say(line):
        sent.append(line)
    runner = eq.Eqs(True, say, lambda *a: None)
    runner.dyn_last["k"] = eq.dyn_values(dev, 48000)
    dev["bands"][4]["range"] = -9.0  # the 3rd dynamic band
    aio.run(runner._dyn("k", dev, 7, 48000))
    assert len(sent) == 1 and '"ddp2:' in sent[0] and '"ddp0:' not in sent[0] and '"dbp2:b0"' in sent[0], sent
    # Coefficients set before the EQ's node first runs are lost (PipeWire starts its biquads from
    # the config's then, M25): once it runs, everything goes again, both rows and every stage.
    runner.unrun["k"], runner.last["k"], runner.on["k"] = "n_in", (48000, [None, None]), 0
    runner.ran({"other_in"})
    assert "k" in runner.unrun
    runner.last["k"] = (48000, [eq.slots(dev["bands"], 48000, False)] * 2)
    runner.ran({"n_in"})
    sent.clear()
    aio.run(runner._dyn("k", dev, 7, 48000))
    assert all(f'"ddp{j}:' in sent[0] for j in range(eq.DYN_SLOTS)), sent
    assert runner.last["k"] == (None, [None, None]) and "k" not in runner.unrun


async def check_eq(board, page, next_state):
    """An EQ: one card, IN and OUT; bands sent as coefficients to its input node, only the slots
    that changed; bypass flattens every slot; saved; deleting it removes it."""
    from pipeboard import eq

    await page.send_json({"op": "create-device", "type": "eq", "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"]
                     if state["workspace"]["devices"].get(c["key"], {}).get("type") == "eq"), None)
        if card:
            break
    key = card["key"]
    assert [s["label"] for s in card["inputs"] + card["outputs"]] == ["IN", "OUT"], card
    board.eqs.sent.clear()
    bell = {"type": "bell", "freq": 1000, "gain": 6, "q": 1}
    # Each change goes to the silent row (PipeWire empties a biquad's memory when it changes: a
    # click if heard), only the slots that differ from that row's own, then is faded in.
    for op in ({"bands": [bell, {"type": "evil"}]}, {"bands": [bell, {**bell, "freq": 4000}]},
               {"bands": [bell, {**bell, "freq": 4000}], "bypass": True}):
        await page.send_json({"op": "eq-set", "key": key, **op})
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)  # saved once the changes settle (0.5 s)
    lines = [line for _, line in board.eqs.sent]
    assert len(lines) == 3 and '"t0:b0"' in lines[0] and '"t1:b0"' not in lines[0], lines
    assert '"s0:b0"' in lines[1] and '"s1:b0"' in lines[1] and '"s2:' not in lines[1], lines[1]
    assert lines[2].startswith('"t0:b0" 1.0 "t0:b1" 0.0') and '"t1:' not in lines[2], "bypass: flat"
    assert board.eqs.heard(key) == eq.slots([], board.rate()) and board.eqs.on[key] == 1
    dev = board.ws["devices"][key]
    assert [b["freq"] for b in dev["bands"]] == [1000, 4000] and dev["bypass"]
    assert json.loads((board.store.board).read_text())["devices"][key]["bypass"], "saved"
    # Changes during a swap: the last one wins, nothing in between is sent.
    board.eqs.sent.clear()
    for f in (2000, 3000, 5000, 6000):
        await page.send_json({"op": "eq-set", "key": key, "bands": [{**bell, "freq": f}], "bypass": False})
    await asyncio.sleep(0.6)
    want = eq.slots(eq.clean([{**bell, "freq": 6000}]), board.rate())
    assert board.eqs.heard(key) == want and len(board.eqs.sent) < 4, board.eqs.sent
    conf = eq.conf(dev, 48000)
    assert conf.count("label = bq_raw") == 2 * eq.SLOTS + eq.DYN_SLOTS and '"go:Notify" input = "fade:Stop"' in conf
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert all(c["key"] != key for c in state["model"]["cards"]), "its nodes are gone"


async def check_limiter(board, page, next_state):
    """A limiter: one card, IN and OUT, an EQ-like filter-chain around LSP's limiter with its
    makeup gain off; the ceiling (clamped) and bypass go live to its input node, and are saved."""
    from pipeboard import dynamics as limiter

    await page.send_json({"op": "create-device", "type": "limiter", "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if state["workspace"]["devices"]
                     .get(c["key"], {}).get("type") == "limiter"), None)
        if card:
            break
    key = card["key"]
    assert [s["label"] for s in card["inputs"] + card["outputs"]] == ["IN", "OUT"], card
    sent, say = [], board.gains.command
    board.gains.command = lambda line: sent.append(line) or asyncio.sleep(0)
    try:
        await page.send_json({"op": "dyn-set", "key": key, "ceiling_db": -99, "bypass": True})
        await asyncio.sleep(0.9)  # saved once it settles (0.5 s)
    finally:
        board.gains.command = say
    node = card["ids"][card["names"].index(f"{board.ws['devices'][key]['node']}_in")]
    assert sent == [f'set-param {node} Props {{ params = [ "lim:Threshold (G)" 0.063096 '
                    '"lim:Bypass" 1.0 "lim:Automatic level regulation" 0.0 '
                    '"lim:Operating mode" 0.0 ] }'], sent
    sent.clear()
    board.gains.command = lambda line: sent.append(line) or asyncio.sleep(0)
    try:
        await page.send_json({"op": "dyn-set", "key": key, "smooth": True})
        await asyncio.sleep(0.3)
    finally:
        board.gains.command = say
    assert '"lim:Automatic level regulation" 1.0 "lim:Operating mode" 1.0' in sent[0], sent
    assert json.loads(board.store.board.read_text())["devices"][key]["ceiling_db"] == -24.0
    assert limiter.clean("limiter", "ceiling_db", "nan") == -0.5
    assert limiter.clean("limiter", "ceiling_db", 3) == 0.0
    conf = limiter.conf(limiter.new("limiter"))
    assert '"Gain boost" = 0.0' in conf and '"Threshold (G)" = 0.944061' in conf, conf
    assert 'inputs = [ "lim:Input L" "lim:Input R" ]' in conf
    # The live panel: peaks in and out per 10 ms block, from the 4-channel capture.
    from array import array
    block = array("f", [0.5, -2.0, 0.9, -0.9] * limiter.BLOCK).tobytes()
    assert limiter.peaks(block * 2 + block[:16]) == [[2.0, 0.9], [2.0, 0.9]], "whole blocks only"
    outs = card["outputs"][0]["ports"]
    assert limiter.capture(board.model, key) == [(outs[0], "RL"), (outs[1], "RR")], "nothing wired in"
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert all(c["key"] != key for c in state["model"]["cards"]), "its nodes are gone"


async def check_compressor(board, page, next_state):
    """A compressor: its settings clamped and sent live as LSP's controls (threshold and makeup as
    factors), every control LSP's LADSPA defaults get wrong set in its config; deleted cleanly."""
    from pipeboard import dynamics

    await page.send_json({"op": "create-device", "type": "compressor", "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if state["workspace"]["devices"]
                     .get(c["key"], {}).get("type") == "compressor"), None)
        if card:
            break
    key = card["key"]
    assert [s["label"] for s in card["inputs"] + card["outputs"]] == ["IN", "OUT"], card
    sent, say = [], board.gains.command
    board.gains.command = lambda line: sent.append(line) or asyncio.sleep(0)
    try:
        await page.send_json({"op": "dyn-set", "key": key, "threshold_db": -99, "ratio": 2.56,
                              "makeup_db": 6, "smooth": True, "ceiling_db": -3})
        await asyncio.sleep(0.3)
    finally:
        board.gains.command = say
    dev = board.ws["devices"][key]
    assert "smooth" not in dev and "ceiling_db" not in dev, "a limiter's settings stay off it"
    assert sent and sent[0].endswith('[ "comp:Attack threshold (G)" 0.001 "comp:Ratio" 2.6 '
                                     '"comp:Attack time (ms)" 10.0 "comp:Release time (ms)" 150.0 '
                                     '"comp:Makeup gain (G)" 1.995262 "comp:Bypass" 0.0 ] }'), sent
    conf = dynamics.conf(dev)
    assert '"Sidechain reactivity (ms)" = 10.0' in conf and '"Compression mode" = 0.0' in conf
    assert 'label = "http://lsp-plug.in/plugins/ladspa/compressor_stereo"' in conf
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert all(c["key"] != key for c in state["model"]["cards"]), "its nodes are gone"


async def check_gate(board, page, next_state):
    """A noise gate: its settings clamped and sent live as LSP's gate controls (threshold and
    range as factors); peak detection and hysteresis set in its config."""
    from pipeboard import dynamics

    await page.send_json({"op": "create-device", "type": "gate", "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if state["workspace"]["devices"]
                     .get(c["key"], {}).get("type") == "gate"), None)
        if card:
            break
    key = card["key"]
    sent, say = [], board.gains.command
    board.gains.command = lambda line: sent.append(line) or asyncio.sleep(0)
    try:
        await page.send_json({"op": "dyn-set", "key": key, "threshold_db": -20, "range_db": -99,
                              "hold_ms": 5000, "ratio": 8})
        await asyncio.sleep(0.3)
    finally:
        board.gains.command = say
    dev = board.ws["devices"][key]
    assert "ratio" not in dev, "a compressor's setting stays off it"
    assert sent and sent[0].endswith('[ "gate:Curve threshold (G)" 0.1 "gate:Reduction (G)" '
                                     '0.000316 "gate:Attack (ms)" 5.0 "gate:Hold time (ms)" 1000.0 '
                                     '"gate:Release (ms)" 150.0 "gate:Bypass" 0.0 ] }'), sent
    conf = dynamics.conf(dev)
    assert '"Sidechain mode" = 0.0' in conf and '"Hysteresis" = 1.0' in conf, "peaks, hysteresis"
    assert '"Curve zone size (G)" = 0.501187' in conf, "a soft zone: no 40 dB jump on open and close"
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert all(c["key"] != key for c in state["model"]["cards"]), "its nodes are gone"


async def check_denoise(board, page, next_state):
    """Noise suppression: one card, IN and OUT; our plugin, one per channel at 48 kHz, Strength
    and Bypass set live; deleted cleanly."""
    from pipeboard import dynamics

    await page.send_json({"op": "create-device", "type": "denoise", "x": 0, "y": 0})
    for _ in range(8):
        state = await next_state()
        card = next((c for c in state["model"]["cards"] if state["workspace"]["devices"]
                     .get(c["key"], {}).get("type") == "denoise"), None)
        if card:
            break
    key = card["key"]
    assert [s["label"] for s in card["inputs"] + card["outputs"]] == ["IN", "OUT"], card
    dev = board.ws["devices"][key]
    conf = dynamics.conf(dev)
    assert 'label = "pb_denoise"' in conf and "pb_denoise.so" in conf and "audio.rate = 48000" in conf
    assert 'inputs = [ "ns:Input" ]' in conf and '"Strength (dB)" = -12.0' in conf, conf
    dynamics.update(dev, {"strength_db": -55, "bypass": True})
    assert dynamics.params(dev) == '"ns:Strength (dB)" -40.0 "ns:Bypass" 1.0', dynamics.params(dev)
    await page.send_json({"op": "delete-device", "key": key})
    for _ in range(8):
        state = await next_state()
        if key not in state["workspace"]["devices"]:
            break
    assert all(c["key"] != key for c in state["model"]["cards"]), "its nodes are gone"


def check_portals():
    """Portals (board-only): set by hand and by Tidy (with its badges' spots, or None beside the
    socket); a Tidy's own replaced by the next Tidy, the user's kept; junk refused; kept in the
    board's snapshot only for wires that exist."""
    from pipeboard import profiles, workspace

    ws = {"cards": {}}
    tidy = {"how": "tidy", "out": [10.4, 20], "in": None}
    assert workspace.apply(ws, {"op": "portals", "set": {"a:out>b:in": "hand", "b:out>c:in": tidy,
                                                         "c:out>d:in": {"how": "tidy", "out": "x"}}})
    assert ws["portals"] == {"a:out>b:in": "hand", "b:out>c:in": {"how": "tidy", "out": [10, 20], "in": None},
                             "c:out>d:in": {"how": "tidy", "out": None, "in": None}}, ws["portals"]
    workspace.apply(ws, {"op": "portals", "tidy": True, "set": {"d:out>e:in": tidy}})
    assert set(ws["portals"]) == {"a:out>b:in", "d:out>e:in"}, ws["portals"]  # the user's stays
    workspace.apply(ws, {"op": "portals", "set": {"a:out>b:in": None}})
    assert set(ws["portals"]) == {"d:out>e:in"}
    model = {"cards": [{"key": k, "title": k, "kind": "app", "role": "source", "inputs": [], "outputs": []}
                       for k in "de"],
             "wires": [{"from": ["d", "out"], "to": ["e", "in"], "pairs": [], "links": []}]}
    snap = profiles.snapshot(model, {**ws, "portals": {**ws["portals"], "gone:out>x:in": "hand"}})
    assert snap["portals"] == {"d:out>e:in": ws["portals"]["d:out>e:in"]}, snap["portals"]
    assert snap["tidy_portals"] is True  # the bar's switch: on unless turned off
    workspace.apply(ws, {"op": "tidy", "cards": {"d": {"x": 70, "y": 84}}, "set": {"e:out>f:in": tidy}})
    assert ws["cards"]["d"] == {"x": 70, "y": 84} and set(ws["portals"]) == {"e:out>f:in"}  # one message
    workspace.apply(ws, {"op": "tidy-portals", "on": False})
    assert profiles.snapshot(model, ws)["tidy_portals"] is False


def check_aligned():
    """A dynamics panel's peaks: the delays an effect's output may trail its input by (its own plus
    0, 1, 2 graph cycles: live, 240 behind the owner's EQ, 496 fed by a player), the right one
    found from the sound within about a second, and in delayed by it to the sample, so a block's
    in and out are the same sound, across calls and channel by channel."""
    import random
    from array import array

    from pipeboard import dynamics

    clock = {"rate": 192000, "quantum": 1024}
    assert dynamics.delays("limiter", clock) == [240, 496, 752], dynamics.delays("limiter", clock)
    assert dynamics.delays("compressor", clock)[:2] == [0, 256]
    d, n, rnd = 496, dynamics.BLOCK * 160, random.Random(4)
    sig = [[rnd.uniform(-1, 1) * (1 + (i // 300) % 5) / 6 for i in range(n)] for _ in "LR"]
    frames = array("f")
    for i in range(n):  # in L, in R, then out: in, d samples later
        frames.extend([sig[0][i], sig[1][i], sig[0][i - d] if i >= d else 0.0,
                       sig[1][i - d] if i >= d else 0.0])
    al, got, raw = dynamics.Aligned("k"), [], frames.tobytes()
    step = dynamics.BLOCK_BYTES * 4
    for at in range(0, len(raw), step):  # 40 calls, as the capture arrives
        got += al.peaks(raw[at:at + step], dynamics.delays("limiter", clock))
    assert al.delay == d, al.delay  # it started on 240 and found 496
    late = got[-40:]
    assert len(got) == 160 and all(abs(i - o) < 1e-6 for i, o in late), late


def check_deesser():
    """The de-esser's graph: each channel split by two low-passes and two high-passes at its
    frequency, only the top band through the stereo compressor, mixed back; its frequency is set
    live on all eight biquads; out-of-range values clamped."""
    from pipeboard import dynamics

    dev = {**dynamics.new("deesser"), "freq_hz": 99999, "threshold_db": -20}
    conf = dynamics.conf(dev)
    assert conf.count("label = bq_lowpass") == 4 and conf.count("label = bq_highpass") == 4
    assert '"Freq" = 12000.0' in conf and '"Sidechain lookahead (ms)" = 0.0' in conf
    for link in ('"hiL2:Out" input = "ds:Input L"', '"ds:Output R" input = "mixR:In 2"',
                 '"loL2:Out" input = "mixL:In 1"'):
        assert link in conf, link
    line = dynamics.params(dev)
    assert line.count(":Freq\" 12000.0") == 8 and '"ds:Attack threshold (G)" 0.1' in line, line


def check_delete_races():
    """Deleting a device (owner: a ghost card stayed, sometimes): pw-cli's "no global N" noise is
    not a failed delete; a just-deleted device's nodes are looked past in graph reads; a new
    device does not take a node name that is on the system but not on this board."""
    import asyncio as aio

    from pipeboard import pipewire, server

    said = {"err": ""}
    async def fake_run(*cmd):
        return 0, "", said["err"]
    real, pipewire._run = pipewire._run, fake_run
    try:
        live = pipewire.Live.__new__(pipewire.Live)
        said["err"] = "remote 0: error id:435 seq:868 res:-2 (No such file or directory): no global 358"
        assert aio.run(live.destroy([427])) is None, "noise about another object is not a failure"
        said["err"] = "remote 0: error id:3 res:-1 (Operation not permitted): permission denied"
        assert "permission" in aio.run(live.destroy([427])), "a real error still is"
    finally:
        pipewire._run = real
    board = server.Board.__new__(server.Board)
    board.gone = {"pb:dabc": __import__("time").monotonic() + 5}
    node = lambda nid, pid: {"id": nid, "type": "PipeWire:Interface:Node",  # noqa: E731
                             "info": {"props": {"pipeboard.id": pid, "node.name": f"n{nid}"}}}
    port = {"id": 11, "type": "PipeWire:Interface:Port", "info": {"props": {"node.id": 10}}}
    link = {"id": 30, "type": "PipeWire:Interface:Link", "info": {"output-port-id": 21, "input-port-id": 11}}
    left = board.without_gone([node(10, "dabc"), port, link, node(20, "dother")])
    assert [o["id"] for o in left] == [20], left


async def check_own_devices():
    """The board shows PipeBoard nodes of its own only: a lost gain of this board is taken back
    (shown, with its level box); another board's gain and a test run's EQ are not drawn, and a
    ghost card left on the board by one is forgotten."""
    from pipeboard import devices, eq, pipewire, server, workspace

    with tempfile.TemporaryDirectory() as tmp:
        workspace.save(Path(tmp) / "workspace.json", {"cards": {
            "pb:dghost": {"seen": {"title": "EQ", "kind": "effect", "inputs": [], "outputs": []}},
            "pb:dlost": {"hidden": True}}, "devices": {}})
        fixture = pipewire.Fixture(scenarios.load("machine"))
        board = server.Board(fixture, Path(tmp))
        lost = {"id": "dlost", "type": "gain", "name": "Gain", "node": "gain_2", "channels": 2}
        await fixture.create(lost)  # this board's, lost from it
        mine = devices.BOARD
        devices.BOARD = "someoneelse"
        await fixture.create({**lost, "id": "dother", "node": "gain_9"})  # another board's
        await fixture.create({**eq.new(), "id": "dtest", "node": "pb_eq_dtest"})  # a test run's EQ
        devices.BOARD = mine
        await board.refresh()
        keys = {c["key"] for c in board.model["cards"]}
        dev = board.ws["devices"].get("pb:dlost")
        assert dev and dev["type"] == "gain" and dev["gain_db"] == 0.0, board.ws["devices"]
        assert not board.ws["cards"].get("pb:dlost", {}).get("hidden"), "taken back, and shown"
        assert "pb:dlost" in keys and not keys & {"pb:dother", "pb:dtest"}, keys
        assert "pb:dghost" not in board.ws["cards"], "the ghost is forgotten"
        assert not any(m["key"] == "pb:dghost" for m in profiles_missing(board)), "and not missing"
        # A Revert never deletes a device that was already there (it once deleted two gains the
        # board had just taken back, because the saved profile was older than them).
        await board.op_save(None, {"name": "Before"})
        await fixture.create({**lost, "id": "dlater", "node": "gain_7"})  # lost from an older board
        await board.refresh()
        assert "pb:dlater" in board.ws["devices"], "taken back"
        await board.op_revert(None, {})
        assert any(o["info"]["props"].get("pipeboard.id") == "dlater" for o in fixture.dump
                   if o["type"].endswith("Node")), "Revert left it on the system"


def profiles_missing(board):
    from pipeboard import profiles
    return profiles.missing(board.ws, board.model)


async def effects_chain(tmp):
    """A board with Music Player → EQ → plugin → Desktop Mix, all live in a fixture."""
    from pipeboard import eq, pipewire, plugins, server, workspace

    fixture = pipewire.Fixture(scenarios.load("machine"))
    fx = {**eq.new(), "id": "deqq", "node": "pb_eq_deqq"}
    plug = {**plugins.new(plugins.FAKE_INSTALLED[3]), "id": "dplug", "node": "pb-dplug",
            "params": [], "editor": True}
    workspace.save(Path(tmp) / "workspace.json", {"cards": {}, "devices": {"pb:deqq": fx, "pb:dplug": plug}})
    board = server.Board(fixture, Path(tmp))
    for dev in (fx, plug):
        await fixture.create(dev)
    await board.refresh()
    socks = {c["key"]: c for c in board.model["cards"]}
    for link in {w["id"]: w for w in board.model["wires"]}["music_player:out>desktop_mix:in"]["links"]:
        await fixture.unlink(link)
    for a, sa, b, sb in (("music_player", "out", "pb:deqq", "in"), ("pb:deqq", "out", "pb:dplug", "in"),
                         ("pb:dplug", "out", "desktop_mix", "in")):
        outs = next(x for x in socks[a]["outputs"] if x["id"] == sa)["ports"]
        ins = next(x for x in socks[b]["inputs"] if x["id"] == sb)["ports"]
        for o, i in zip(outs, ins, strict=True):
            await fixture.link(o, i)
    await board.refresh()
    return fixture, board


CHAIN = {"music_player:out>pb:deqq:in", "pb:deqq:out>pb:dplug:in", "pb:dplug:out>desktop_mix:in"}


async def check_route_around():
    """Quitting: the sound goes around the EQ and plugins that stop with PipeBoard (Music Player → EQ →
    plugin → Desktop Mix becomes Music Player → Desktop Mix, L→L, R→R); the next launch brings them back,
    makes their wires again and only then removes the links around them."""
    from pipeboard import graph, server

    with tempfile.TemporaryDirectory() as tmp:
        fixture, board = await effects_chain(tmp)
        assert graph.around(board.model, {"pb:deqq", "pb:dplug"}) == [
            {"from": ["music_player", "out"], "to": ["desktop_mix", "in"], "pairs": [[0, 0], [1, 1]]}]
        await board.route_around()  # quitting
        wires = {w["id"] for w in graph.build(fixture.dump)["wires"]}
        assert "music_player:out>desktop_mix:in" in wires, "the sound goes around them"
        for key in ("deqq", "dplug"):  # their processes end with PipeBoard
            await fixture.destroy([o["id"] for o in fixture.dump if o["type"].endswith("Node")
                                   and o["info"]["props"].get("pipeboard.id") == key])
        again = server.Board(fixture, Path(tmp))  # the next launch
        await server.startup(again)
        for _ in range(6):
            await again.refresh()
        wires = {w["id"] for w in again.model["wires"]}
        assert wires >= CHAIN, wires
        assert "music_player:out>desktop_mix:in" not in wires, "the way around is gone again"
        assert "around" not in again.ws
    await check_relaunch_with_profile()


async def check_relaunch_with_profile():
    """The user's launch: a profile loads at launch, the EQ comes up first and the plugin host a
    moment later. Nothing is started twice, the sound keeps going around until both are back and
    wired, and the board then matches the profile (it used to need a Revert)."""
    from pipeboard import server

    def wired(board):
        return {w["id"] for w in board.model["wires"]} & (CHAIN | {"music_player:out>desktop_mix:in"})

    with tempfile.TemporaryDirectory() as tmp:
        fixture, board = await effects_chain(tmp)
        await board.op_save(None, {"name": "Chain"})
        await board.op_launch(None, {"name": "Chain"})
        await board.route_around()
        for key in ("deqq", "dplug"):
            await fixture.destroy([o["id"] for o in fixture.dump if o["type"].endswith("Node")
                                   and o["info"]["props"].get("pipeboard.id") == key])
        held, create = [], fixture.create
        fixture.create = lambda dev: held.append(dev) or asyncio.sleep(0)  # they take a while
        again = server.Board(fixture, Path(tmp))
        await server.startup(again)
        for _ in range(3):
            await again.refresh()
        assert wired(again) == {"music_player:out>desktop_mix:in"}, wired(again)
        assert sorted(d["id"] for d in held) == ["deqq", "dplug"], "each started once"
        fixture.create = create
        for dev in sorted(held, key=lambda d: d["type"] != "eq"):  # the EQ first, the plugin later
            await create(dev)
            for _ in range(3):
                await again.refresh()
            if dev["type"] == "eq":
                assert "music_player:out>desktop_mix:in" in wired(again), "still around while one is missing"
        assert wired(again) == CHAIN, wired(again)
        assert again.profile_state()["changes"] == [], again.profile_state()["changes"]
    await check_ports_late()


async def check_ports_late():
    """As on the real system: an EQ's nodes appear a moment before their ports (a plugin host's
    too). Its wires must wait for the ports, not be tried once and dropped (the user's EQs came
    back disconnected after every relaunch)."""
    from pipeboard import server

    with tempfile.TemporaryDirectory() as tmp:
        fixture, board = await effects_chain(tmp)
        await board.op_save(None, {"name": "Chain"})
        await board.op_launch(None, {"name": "Chain"})
        await board.route_around()
        for key in ("deqq", "dplug"):
            await fixture.destroy([o["id"] for o in fixture.dump if o["type"].endswith("Node")
                                   and o["info"]["props"].get("pipeboard.id") == key])
        create, ports = fixture.create, []

        async def nodes_first(dev):
            await create(dev)
            mine = {o["id"] for o in fixture.dump if o["type"].endswith("Node")
                    and o["info"]["props"].get("pipeboard.id") == dev["id"]}
            ports.extend(o for o in fixture.dump if o["type"].endswith("Port")
                         and o["info"]["props"]["node.id"] in mine)
            fixture.dump[:] = [o for o in fixture.dump if o not in ports]
        fixture.create = nodes_first
        again = server.Board(fixture, Path(tmp))
        await server.startup(again)
        for _ in range(3):
            await again.refresh()  # their cards are there, without sockets
        fixture.dump += ports  # now the ports
        for _ in range(3):
            await again.refresh()
        assert {w["id"] for w in again.model["wires"]} & (CHAIN | {"music_player:out>desktop_mix:in"}) == CHAIN
        assert again.profile_state()["changes"] == [], again.profile_state()["changes"]


async def check_settings_revert():
    """A gain's level, an EQ's bands and a plugin's controls and bypass are part of the profile:
    changing them is a change, and Revert puts them back on the running devices themselves."""
    from pipeboard import eq, pipewire, plugins, server

    with tempfile.TemporaryDirectory() as tmp:
        board = server.Board(pipewire.Fixture(scenarios.load("machine")), Path(tmp))
        await board.refresh()
        hum = plugins.ref(plugins.FAKE_INSTALLED[3])
        for op in ({"type": "gain", "name": "Gain", "channels": 2}, {"type": "eq"},
                   {"type": "plugin", "plugin": hum}):
            assert await board.op_create_device(None, {**op, "x": 0, "y": 0}) is None
        for _ in range(3):
            await board.refresh()
        keys = {d["type"]: k for k, d in board.ws["devices"].items()}
        await board.op_save(None, {"name": "Set"})
        await board.op_set_gain({"key": keys["gain"], "db": 6})
        await board.act(None, {"op": "eq-set", "key": keys["eq"], "bands": [
            {"type": "bell", "freq": 1000, "gain": 6, "q": 1}]})
        board.heard(keys["plugin"], {"param": [0, 0.9]})
        await board.op_plugin_bypass(None, {"key": keys["plugin"], "on": True})
        await board.refresh()
        changed = board.profile_state()["changes"]
        assert len([c for c in changed if c.startswith("Changed the settings")]) == 3, changed
        sent = []
        board.plugins.send = lambda k, m: sent.append(m)
        board.gains.sent.clear()
        board.eqs.sent.clear()
        await board.op_revert(None, {})
        await asyncio.sleep(0.6)  # the gain's ramp
        await board.refresh()
        assert board.gains.sent and board.gains.sent[-1][1] == 0.0, board.gains.sent[-3:]
        assert board.eqs.heard(keys["eq"]) == eq.slots([], board.rate()), "EQ flat again"
        assert {"values": {"0": 0.6, "1": 0.5, "2": 0.0, "3": 1.0, "4": 0.0}} in sent and {"bypass": False} in sent, sent
        assert board.profile_state()["changes"] == [], board.profile_state()["changes"]


class FakeCli:
    """A pw-cli that says it is connected and takes any line."""

    returncode = None

    def __init__(self):
        self.stdin, self.lines = self, iter([b"remote 0 is named 'pipewire-0'\n"])
        self.stdout = self

    def write(self, data):
        pass

    async def drain(self):
        pass

    async def readline(self):
        return next(self.lines, b"")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def check_one_pw_cli():
    """Commands sent at once (two gains and two EQs at launch) share one pw-cli: each started
    its own while the first was still connecting, and the forgotten ones' pipes errored at exit."""
    from pipeboard import gain

    started = []

    async def spawn(*_a, **_k):
        await asyncio.sleep(0.01)  # starting a process takes a moment
        started.append(FakeCli())
        return started[-1]

    async def run():
        real, asyncio.create_subprocess_exec = asyncio.create_subprocess_exec, spawn
        try:
            g = gain.Gains(live=True)
            await asyncio.gather(*(g.command("info 0") for _ in range(6)))
        finally:
            asyncio.create_subprocess_exec = real
        assert len(started) == 1, f"{len(started)} pw-cli started for six commands at once"

    asyncio.run(run())


def check_create_errors():
    """Making a device: pw-cli's notes about objects that vanished meanwhile (a busy graph at
    login) are not a failure — they once stopped the profile load at login; a real error is."""
    from pipeboard import pipewire

    dev = {"type": "gain", "name": "G", "node": "g", "id": "d1", "channels": 2}
    real = pipewire._run
    try:
        async def churn(*_cmd):
            return 0, "", ("remote 0: error id:88 seq:174 res:-116 (Stale file handle): no global 87 any more\n"
                           "remote 0: error id:0 seq:276 res:-2 (No such file or directory): unknown resource 88")
        pipewire._run = churn
        assert asyncio.run(pipewire.Live().create(dev)) is None

        async def bad(*_cmd):
            return 0, "", 'Error: "create-node" failed: Invalid argument'
        pipewire._run = bad
        assert "Invalid argument" in asyncio.run(pipewire.Live().create(dev))
    finally:
        pipewire._run = real


async def check_server():
    """The whole app on the fixture: connect, disconnect, rename, and the Origin guard."""
    from aiohttp.test_utils import TestClient, TestServer, unused_port

    from pipeboard import pipewire, server

    with tempfile.TemporaryDirectory() as tmp:
        board = server.Board(pipewire.Fixture(scenarios.load("machine")), Path(tmp))
        port = unused_port()
        origin = f"http://127.0.0.1:{port}"
        client = TestClient(TestServer(server.app(board, port), port=port))
        await client.start_server()
        assert (Path(tmp) / "profiles" / "Default.json").exists(), "a first launch saves Default once"
        bad = await client.get("/ws", headers={"Origin": "http://evil.example"})
        assert bad.status == 403, "other websites cannot drive the board"
        page = await client.ws_connect("/ws", headers={"Origin": origin})
        state = json.loads((await page.receive()).data)
        assert len(state["model"]["cards"]) == 13 and state["mode"] == "fixture"

        async def next_state():
            while True:
                msg = json.loads((await page.receive(timeout=3)).data)
                if msg["type"] == "state":
                    return msg

        await page.send_json({"op": "connect", "from": ["music_player", "out"], "to": ["mic_mix", "in"]})
        wires = {w["id"]: w for w in (await next_state())["model"]["wires"]}
        assert len(wires["music_player:out>mic_mix:in"]["links"]) == 2, "stereo to stereo is two links"
        await page.send_json({"op": "move-wire", "wire": "music_player:out>mic_mix:in",
                              "to": ["instrument_mix", "in"]})
        wires = {w["id"]: w for w in (await next_state())["model"]["wires"]}
        assert "music_player:out>mic_mix:in" not in wires and len(wires["music_player:out>instrument_mix:in"]["links"]) == 2
        jack3 = f"{UR}:out3>instrument_mix:in"  # feeds Instrument Mix's left only: must still, after a move
        await page.send_json({"op": "move-wire", "wire": jack3, "to": ["input1_mix", "in"]})
        moved = {w["id"]: w for w in (await next_state())["model"]["wires"]}[f"{UR}:out3>input1_mix:in"]
        assert moved["toPorts"] == [0] and moved["ch"] == "L", "the left stays the left"
        await page.send_json({"op": "move-wire", "wire": f"{UR}:out3>input1_mix:in", "to": ["instrument_mix", "in"]})
        await next_state()
        await page.send_json({"op": "move-wire", "wire": "music_player:out>instrument_mix:in", "to": ["mic_mix", "in"]})
        await next_state()
        await page.send_json({"op": "disconnect", "wire": "music_player:out>mic_mix:in"})
        assert "music_player:out>mic_mix:in" not in {w["id"] for w in (await next_state())["model"]["wires"]}
        # Stereo into mono: one channel per wire, L then R, then L again (user).
        sides = []
        for jack in ("in3", "in4", "in5"):
            await page.send_json({"op": "connect", "from": ["music_player", "out"], "to": [UR, jack]})
            wires = {w["id"]: w for w in (await next_state())["model"]["wires"]}
            sides.append(wires[f"music_player:out>{UR}:{jack}"]["fromPorts"])
        assert sides == [[0], [1], [0]], sides
        for jack in ("in3", "in4", "in5"):
            await page.send_json({"op": "disconnect", "wire": f"music_player:out>{UR}:{jack}"})
            await next_state()
        await page.send_json({"op": "rename", "key": "mic_mix", "title": "Stream Mic"})
        assert (await next_state())["workspace"]["cards"]["mic_mix"]["nick"] == "Stream Mic"
        assert json.loads((Path(tmp) / "workspace.json").read_text())["cards"]["mic_mix"]["nick"]
        default = json.loads((Path(tmp) / "profiles" / "Default.json").read_text())
        assert "nick" not in default["cards"].get("mic_mix", {}), "Default does not save itself"
        await check_profile_flow(page, next_state)
        await page.send_json({"op": "connect", "from": ["music_player", "out"], "to": ["mic_mix", "in"]})
        await next_state()
        await page.send_json({"op": "hide", "key": "mic_mix", "hidden": True})
        state = await next_state()
        ids = {w["id"] for w in state["model"]["wires"]}
        assert "music_player:out>mic_mix:in" not in ids, "the user's wire is set aside with the card"
        assert f"{UR}:out2>mic_mix:in" in ids, "the system's wire stays"
        await page.send_json({"op": "hide", "key": "mic_mix", "hidden": False})
        state = await next_state()
        assert "music_player:out>mic_mix:in" in {w["id"] for w in state["model"]["wires"]}, "back with it"
        await page.send_json({"op": "disconnect", "wire": "music_player:out>mic_mix:in"})
        await next_state()
        await check_devices(page, next_state)
        await check_gain(board, page, next_state)
        await check_insert(page, next_state)
        await check_plugins(board, page, next_state)
        await check_eq(board, page, next_state)
        await check_limiter(board, page, next_state)
        await check_compressor(board, page, next_state)
        await check_gate(board, page, next_state)
        await check_denoise(board, page, next_state)
        await page.close()
        await client.close()


def main():
    model = graph.build(scenarios.load("machine"))
    cards = {c["key"]: c for c in model["cards"]}
    assert len(cards) == len(model["cards"]), "card keys are unique"
    check_cards(cards)
    check_wires(model["wires"])
    check_pedal()
    check_problems()
    check_workspace()
    check_streams()
    check_gain_math()
    check_virtual_source()
    check_profiles()
    check_missing()
    check_scope_math()
    check_eq_math()
    check_eq_range()
    check_eq_dynamic()
    check_delete_races()
    check_aligned()
    check_portals()
    check_deesser()
    check_one_pw_cli()
    check_create_errors()
    asyncio.run(check_rename_stale())
    asyncio.run(check_scope_follows())
    asyncio.run(check_scope_failures())
    asyncio.run(check_takeover())
    asyncio.run(check_server())
    asyncio.run(check_plugin_startup())
    asyncio.run(check_own_devices())
    asyncio.run(check_settings_revert())
    asyncio.run(check_route_around())
    print(f"ok: {len(cards)} cards, {len(model['wires'])} wires; pedal, problems, workspace, server")


if __name__ == "__main__":
    main()
