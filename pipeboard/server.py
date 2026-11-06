"""The page and its WebSocket: the board model out, the user's actions in (PLAN §3)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import os
import re
import secrets
import time
from collections import Counter
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from pipeboard import (
    autostart,
    devices,
    diag,
    dynamics,
    engine,
    eq,
    gain,
    graph,
    plugins,
    problems,
    profiles,
    takeover,
    workspace,
)
from pipeboard import scope as scopes

log = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web"
SETTLE_S = 0.15  # wait this long after a change before taking a snapshot
# Effects that are a PipeWire process of PipeBoard's (eq.Eqs runs them), and every process that
# ends with PipeBoard and is started again at launch.
FILTERS = ("eq", *dynamics.KINDS)
RUNS_WITH = ("plugin", *FILTERS)
SETTLE_DROPS_S = 30  # dropouts just after an engine change or a device made: its own, not counted
SETTLE_CAPTURE_S = 5  # and just after the live wires' capture started or took new wires in
TRIAL_S = 60  # a new audio engine setting is kept only if Keep is pressed within this
BACK_SHOWN_S = 2.5  # how long the bar says the interface is back, before the trial's own
GONE_S = 5.0  # how long a deleted device's nodes are looked past while PipeWire removes them
NOT_MADE = "A device name is up to 40 characters, without quotes, brackets, braces or =."


class Board:
    """Holds the graph, the board being worked on, its profiles and the open pages."""

    def __init__(self, source, folder: Path):
        self.source, self.store = source, workspace.Store(folder)
        self.ws, self.settings = workspace.load(self.store.board), self.store.settings()
        # This board's id, tagged on everything it makes: made once and kept in its settings (a
        # hash of the folder's path changed when the folder moved, and orphaned its devices).
        devices.BOARD = self.settings.get("board") or secrets.token_hex(5)
        # Links made around EQs/plugins at the last quit; at launch: (those effects, the link ids),
        # unlinked once every one of them is back and wired.
        self.around, self.around_wait = self.ws.pop("around", []), []
        self.model: dict = {"cards": [], "wires": [], "clock": None}
        self.pages: set[web.WebSocketResponse] = set()
        # closing: quitting now; swapping: a renamed device's new node is being wired, and a
        # snapshot would see the old and new one as one card.
        self.pending, self.last, self.closing, self.swapping = False, "", False, False
        # PipeBoard devices being made (wire them when they appear); and the ones created in
        # this session, the only ones a Revert may delete.
        # (first_seen: when a waiting device's card first showed, for the 5 s fallback.)
        # self.gone: a device just deleted → until when its nodes are looked past (without_gone)
        # node_names: every node name on the system at the last read (new devices avoid them)
        self.waiting, self.created, self.first_seen, self.gone, self.node_names = (
            set(), set(), {}, {}, set())
        # Inserted device → the links it replaces; renamed device → its old node's newest serial.
        self.replacing, self.remade = {}, {}
        self.gains = gain.Gains(live=source.mode == "live")
        # Plugin hosts, plugins whose own window is open, EQ processes, the EQ panel each page
        # shows (page → key) and the one spectrum capture for it, with a dynamics panel's peaks
        # aligned in and out (dynamics.Aligned); each dynamics node's settings were sent to.
        (self.plugins, self.editors, self.eqs, self.watching, self.spectrum, self.aligned,
         self.dyn_nodes) = (
            plugins.Plugins(self.heard) if source.mode == "live"
            else plugins.FakePlugins(self.heard, source), set(),
            eq.Eqs(source.mode == "live", self.gains.command, self.ended), {}, None, None, {})
        self.saving: asyncio.TimerHandle | None = None
        self.autostart = False  # launch at login: read at start, changed by the "autostart" op
        self.on_state = None  # the tray's update(alert, tooltip), when there is a tray
        self.offer: dict[str, list[dict]] = {}  # service → its virtual devices, to take over
        self.unit_of = takeover.cgroup_unit
        # Live wires: one capture, only while some page is visible and the setting is on.
        self.visible: set[web.WebSocketResponse] = set()
        self.scope: scopes.Scope | None = None
        self.scope_key: tuple = ()
        # (scope_serials: the node serials of the cards recorded, to see one made again)
        self.scope_tries, self.scope_seen, self.scope_serials = 0, (0, 0.0), {}
        # One restart at a time: overlapping ones each started a capture and forgot all but the
        # last (20 quick wire changes left 12 captures running).
        self.scope_lock = asyncio.Lock()
        self.scope_started = 0.0  # (values captured, when that last changed): spots a stall
        self.clipped: dict[tuple, float] = {}  # source socket → when it last clipped
        self.ops = {"connect": self.op_connect, "disconnect": self.op_disconnect,
                    "profile-save": self.op_save, "profile-plan": self.op_plan,
                    "profile-load": self.op_load, "profile-revert": self.op_revert,
                    "profile-delete": self.op_delete, "profile-launch": self.op_launch,
                    "retarget": self.op_retarget, "drop-card": self.op_drop,
                    "forget-wire": self.op_forget_wire, "live": self.op_live,
                    "move-wire": self.op_move_wire, "autostart": self.op_autostart,
                    "take-over": self.op_take_over, "insert-device": self.op_insert_device,
                    "hide": self.op_hide, "plugin-editor": self.op_plugin_editor,
                    "plugin-bypass": self.op_plugin_bypass, "eq-watch": self.op_eq_watch,
                    "plugin-list": self.op_plugin_list,
                    "create-device": self.op_create_device,
                    "rename-device": self.op_rename_device,
                    "delete-device": self.op_delete_device,
                    "create-missing": self.op_create_missing,
                    "live-setting": self.op_live_setting,
                    "engine-try": self.op_engine_try, "engine-keep": self.op_engine_keep,
                    "engine-back": self.op_engine_back, "show-latency": self.op_show_latency,
                    "live-instant": self.op_live_instant, "drops": self.op_drops,
                    "cstates": self.op_cstates, "diag-on": self.op_diag_on,
                    "diag-set": self.op_diag_set, "diag-reset": self.op_diag_reset}
        # The audio engine as it runs (engine.read), and the task that goes back when a trial's
        # minute is up with no Keep.
        # (trial_drops: the interface's dropout count when the trial's setting began, and now)
        # Pages showing the path panel, and the task counting its cards' dropouts meanwhile;
        # dropped: each card's dropouts in the last minute, for the Dropouts problem; settling:
        # until when dropouts are not counted, an engine change's own burst (owner: Spotify 666).
        # idle: the root helper for CPU idle states (one password prompt per run).
        # starting: while a reopened interface comes back, its step and since when (then for a
        # moment how long it took); listen: the task waiting for its sound. taps: the diagnostic
        # cards' capture, and each card's monitor ports (diag.monitors).
        # tallies: each Counter's cards' error totals a second ago; clipping: the Counters whose
        # sound was at full scale in the last frame (a new clipping moment counts once); counted:
        # a Counter's totals changed (saved and sent once a second).
        (self.engine, self.trial_task, self.trial_drops, self.drop_pages, self.drops_task,
         self.dropped, self.settling, self.idle, self.starting, self.listen, self.taps,
         self.diag_ports, self.tallies, self.clipping, self.counted, self.shown_counts,
         self.diag_keys, self.err_seen) = ({}, None, (None, None), set(), None, {}, 0.0,
                                           engine.IdleHelper(), None, None, None, {}, {}, set(),
                                           False, 0.0, {}, {})

    # ---- state ------------------------------------------------------------------------------
    def first_profile(self) -> None:
        """No profile at all yet (a first launch): save the routing as it is as Default, once.
        No profile saves itself after that (user): changes wait for Save. The
        board's id is saved at the first launch too."""
        if not self.settings.get("board"):
            self._set(board=devices.BOARD)
        if not self.store.names():
            self.store.write(profiles.DEFAULT, self.ws)
            self._set(active=profiles.DEFAULT)

    def profile_state(self) -> dict:
        active = self.settings["active"]
        saved = self.store.read(active) if active and active in self.store.names() else None
        gone = profiles.missing(self.ws, self.model)
        keys = {m["key"] for m in gone}
        return {"active": active, "names": self.store.names(),
                "launch": self.settings["launch"],
                "changes": profiles.changes(saved, self.ws) if saved else [],
                "missing": gone, "ghosts": [w for w in self.ws["wires"]
                                            if w["from"][0] in keys or w["to"][0] in keys]}

    def state(self) -> str:
        prof = self.profile_state()
        found = problems.find(self.model)
        found += problems.missing_devices(prof["missing"], self.ws["wires"])
        found += problems.clipping(self.model, list(self.clipped))
        found += problems.dropouts(self.model, self.dropped)
        found.sort(key=lambda p: problems.ORDER[p["severity"]])
        return json.dumps({"type": "state", "model": self.model, "workspace": self.ws,
                           "profile": prof, "problems": found, "mode": self.source.mode,
                           "autostart": self.autostart, "editors": sorted(self.editors),
                           "takeover": {u: [c["title"] for c in cards]
                                        for u, cards in self.offer.items()},
                           "live": {"on": self.settings["live"],
                                    "instant": self.settings["live_instant"],
                                    "sources": self.scope_sources_sent()},
                           "engine": {**self.engine, "trial": self.trial_state(),
                                      "starting": self.starting_state()},
                           "show_latency": self.settings["show_latency"]})

    def read_engine(self, dump: list[dict]) -> None:
        """The audio engine as it runs, and the CPU idle states (their attempts counted on)."""
        tries = self.engine.get("cstates", {}).get("tries", 0)
        self.engine = {**engine.read(dump), "cstates": {**engine.cstates(), "tries": tries}}

    def dress(self) -> None:
        """PipeBoard's own cards get the names it gave them (a plugin's node is pb-<id>), and every
        card its latency (Show latency): PipeBoard knows its own effects' delays, not a plugin's."""
        devs = self.ws.get("devices", {})
        for card in self.model["cards"]:
            if dev := devs.get(card["key"]):
                card["title"] = dev["name"]
        self.model["latency"] = graph.latency(self.model, {
            k: dynamics.latency_ms(d["type"]) for k, d in devs.items() if d["type"] != "plugin"})

    async def refresh(self) -> None:
        self.pending = False
        try:
            dump = self.without_gone(await self.source.snapshot())
            # Every node name on the system, the hidden ones too (another board's, a test's):
            # a new device must not take one (a test board's gain was made as gain_2 while the
            # owner's gain_2 was live: its board does not show other boards' nodes).
            self.node_names = {o["info"]["props"].get("node.name") for o in dump
                               if o["type"].endswith("Node")}
            self.model = self.own(graph.build(dump), dump)
            self.dress()
            self.read_engine(dump)
            self.offer = takeover.find(dump, self.model, self.unit_of)
        except Exception:
            log.exception("could not read the graph")
            return
        if await self.wire_arrivals():
            self.changed()  # their links appear in the next snapshot; this one would drop them
            return
        # The wires of devices still on their way stay on the board until they are made: the
        # snapshot keeps only what is live, and would forget them.
        coming = [w for w in self.ws.get("wires", []) if {w["from"][0], w["to"][0]} & self.waiting]
        self.ws = profiles.snapshot(self.model, self.ws)
        have = {(tuple(w["from"]), tuple(w["to"])) for w in self.ws["wires"]}
        self.ws["wires"] += [w for w in coming if (tuple(w["from"]), tuple(w["to"])) not in have]
        await self.sync_scope()
        await self.sync_eqs(dump)
        await self.sync_taps(dump)
        text = self.state()
        if text != self.last:
            self.last = text
            self.persist()
            await self.broadcast(text)
            self.tell_tray()

    def own(self, model: dict, dump: list[dict]) -> dict:
        """PipeBoard nodes on the board: this board's devices only. Its lost lingering devices are
        taken back (and shown again); any other PipeBoard's nodes (a test run's EQ, another
        config's gain) are not drawn and not remembered: they once piled up as ghost cards."""
        devs = self.ws.setdefault("devices", {})
        for key, dev in devices.adopt(dump).items():
            if key not in devs:
                log.info("taking back %s (%s), lost from the board", dev["name"], dev["node"])
                devs[key] = dev
                self.ws.setdefault("cards", {}).get(key, {}).pop("hidden", None)
        cards = self.ws.setdefault("cards", {})
        foreign = {c["key"] for c in model["cards"] if c["key"].startswith("pb:")} - set(devs)
        foreign |= {k for k in cards if k.startswith("pb:") and k not in devs}
        if not foreign:
            return model
        for key in foreign:
            cards.pop(key, None)
        self.ws["wires"] = [w for w in self.ws.get("wires", [])
                            if w["from"][0] not in foreign and w["to"][0] not in foreign]
        return {**model, "cards": [c for c in model["cards"] if c["key"] not in foreign],
                "wires": [w for w in model["wires"]
                          if w["from"][0] not in foreign and w["to"][0] not in foreign],
                "present": [k for k in model.get("present", []) if k not in foreign]}

    def tell_tray(self) -> None:
        """The tray's dot and tooltip follow the board. Never fatal."""
        if not self.on_state:
            return
        try:
            state = json.loads(self.last)
            errors = [p for p in state["problems"] if p["severity"] == "error"]
            warnings = [p for p in state["problems"] if p["severity"] == "warning"]
            parts = [state["profile"]["active"] or "No profile"]
            if errors or warnings:
                n = len(errors) + len(warnings)
                parts.append(f"{n} problem{'s' if n > 1 else ''}")
            self.on_state(bool(errors), " · ".join(parts))
        except Exception:  # noqa: BLE001
            log.exception("tray: could not update")

    def persist(self) -> None:
        workspace.save(self.store.board, self.ws)  # the board itself, not a profile

    def changed(self) -> None:
        """The graph moved: take one snapshot once it settles, however many events arrive. Not
        while quitting: its own processes ending would be saved as the board."""
        if not self.pending and not self.closing:
            self.pending = True
            asyncio.get_running_loop().call_later(SETTLE_S, self.settled)

    def settled(self) -> None:
        if self.swapping:  # a renamed device is being swapped: look again once that is done
            self.pending = False
            self.changed()
        else:
            asyncio.ensure_future(self.refresh())

    async def broadcast(self, text: str) -> None:
        for page in list(self.pages):
            try:
                await page.send_str(text)
            except ConnectionError:
                self.pages.discard(page)

    async def act(self, page: web.WebSocketResponse, op: dict) -> None:
        quick = {"set-gain": self.op_set_gain, "eq-set": self.op_eq_set,
                 "dyn-set": self.op_dyn_set, "gain-bypass": self.op_gain_bypass}
        if op.get("op") in quick:  # a level or a band: no graph re-read per step
            await quick[op["op"]](op)
            return
        handler = self.ops.get(op.get("op"))
        if handler:
            error = await handler(page, op)
        elif workspace.apply(self.ws, op):
            error = None
        else:
            error = f"Unknown action {op.get('op')!r}"
        if error:
            await toast(page, error)
        await self.refresh()
        self.changed()  # links settle a moment later

    # ---- PipeBoard's own virtual devices (M6) -------------------------------------------------
    async def wire_arrivals(self) -> bool:
        """A device being made has appeared: make its saved links before anything forgets them."""
        ready = {k for k in self.waiting & set(self.model.get("present", []))
                 if self.new_node(k) and self.sockets_up(k)}
        if not ready:
            return False
        self.waiting -= ready
        for key in ready:
            self.first_seen.pop(key, None)
            self.remade.pop(key, None)
        for key in ready:  # a gain made (again) starts at its saved level, at once
            card = self.owned(key)
            dev = self.ws["devices"].get(key, {})
            if card and dev.get("type") == "gain":
                await self.gains.set(card["ids"][0], gain.level(dev),
                                     dev["channels"], ramp=False)
        # Not the routes WirePlumber gave apps: it routes them to the device by itself, and a
        # link made here would pin the app (switching its device did nothing: user).
        theirs = [w for w in self.ws["wires"] if {w["from"][0], w["to"][0]} & ready
                  and not profiles.routed(self.ws, w)]
        plan = profiles.plan({**self.ws, "wires": theirs}, self.model)
        await self.apply({"make": plan["make"], "remove": []})
        for key in ready:  # an inserted device: its new path is made, now the old link goes
            for link in self.replacing.pop(key, []):
                await self.source.unlink(link)
        await self.close_around()
        return True

    async def close_around(self) -> None:
        """At launch: the links made around the EQs and plugins go only once every one of them is
        back and wired (the EQ comes up in a moment, a plugin host takes about a second: going
        early left the chain open in between)."""
        here = set(self.model.get("present", []))
        for keys, links in list(self.around_wait):
            if keys <= here and not keys & self.waiting:
                self.around_wait.remove((keys, links))
                for link in links:
                    await self.source.unlink(link)

    def new_node(self, key: str) -> bool:
        """A device made again (a rename) is back only as its new node. A snapshot can still show
        the old one, links and all: taken as the return, its wires were "there", so none were
        made, and the next snapshot forgot them (user: renaming a gain stopped the sound)."""
        card = self.owned(key)
        return key not in self.remade or bool(card) and min(card["serials"]) > self.remade[key]

    def sockets_up(self, key: str) -> bool:
        """A device is there for its wires once the sockets they use exist: an EQ's ports come a
        moment after its nodes (a plugin host's too), and wiring it at its first sight left its
        wires unmade and then forgotten — the EQs came back disconnected, sometimes.
        After 5 s it counts as there anyway (a profile may name a socket it no longer has)."""
        now = time.monotonic()
        if now - self.first_seen.setdefault(key, now) > 5:
            return True
        for w in self.ws.get("wires", []):
            if w["from"][0] == key and not self._socket(w["from"], "outputs") \
                    or w["to"][0] == key and not self._socket(w["to"], "inputs"):
                return False
        return True

    def without_gone(self, dump: list[dict]) -> list[dict]:
        """The graph without devices just deleted. PipeWire removes their nodes a moment after
        being asked; a read in between put the device back on the board, which then stayed as a
        "missing" card with its wire (owner saw it; 7 times in 10 in a test). Looked past for
        GONE_S whatever one read shows: reads can finish out of order, and one taken before the
        delete, landing after a newer one without the node had ended this, let the board adopt
        the device back (1 in 20)."""
        now = time.monotonic()
        self.gone = {k: until for k, until in self.gone.items() if until > now}
        ids = {k.removeprefix("pb:") for k in self.gone}
        nodes = {o["id"] for o in dump if o["type"].endswith("Node")
                 and o["info"]["props"].get("pipeboard.id") in ids}
        if not nodes:
            return dump
        ports = {o["id"] for o in dump if o["type"].endswith("Port")
                 and o["info"]["props"].get("node.id") in nodes}
        ends = lambda o: {o["info"].get("output-port-id"), o["info"].get("input-port-id")}  # noqa: E731
        return [o for o in dump if o["id"] not in nodes | ports
                and not (o["type"].endswith("Link") and ends(o) & ports)]

    def owned(self, key: str) -> dict | None:
        """A live card PipeBoard made itself: the only kind it may delete or re-create."""
        card = next((c for c in self.model["cards"] if c["key"] == key), None)
        return card if card and card["owned"] and key in self.ws.get("devices", {}) else None

    def settle(self, seconds: float) -> None:
        """PipeBoard changed the graph itself (a device or effect started, the engine set, the live
        wires' capture started): PipeWire sets it up anew and the nodes waiting on it log errors
        nobody hears. Not counted as dropouts for a while (owner: 1477 right after PipeBoard
        started its six effects; the entry back for a moment each time the window came back)."""
        self.settling = max(self.settling, time.monotonic() + seconds)

    async def make(self, key: str) -> str | None:
        self.settle(SETTLE_DROPS_S)
        self.waiting.add(key)
        self.gone.pop(key, None)  # made again (a profile's, a Revert's): no longer looked past
        dev = self.ws["devices"][key]
        if dev["type"] in FILTERS:
            error = await self.eqs.start(key, dev, self.rate())
            if self.source.mode != "live":
                await self.source.create(dev)  # the fixture's stand-in for its process
        else:
            error = await (self.plugins.start(key, dev) if dev["type"] == "plugin"
                           else self.source.create(dev))
        return f"PipeWire could not make the device: {error}" if error else None

    async def define(self, op: dict) -> tuple[str, dict]:
        """A new device on the board (not yet on the system); ValueError for a bad request."""
        taken = {n for c in self.model["cards"] for n in c["names"]} | self.node_names
        taken |= {d["node"] for d in self.ws.get("devices", {}).values()}
        if op.get("type") == "eq":
            dev = eq.new()
        elif op.get("type") in dynamics.KINDS:
            dev = dynamics.new(op["type"])
        elif op.get("type") == "plugin":  # only an installed plugin, named by its ref
            entry = next((p for p in await self.plugins.installed()
                          if plugins.ref(p) == op.get("plugin")), None)
            if not entry:
                raise ValueError("That plugin is not installed (any more).")
            dev = plugins.new(entry)
        else:
            try:
                dev = devices.new(op.get("type"), str(op.get("name", "")).strip(),
                                  int(op.get("channels", 2)), taken)
            except TypeError as e:
                raise ValueError from e
        key = f"pb:{dev['id']}"
        self.ws.setdefault("devices", {})[key] = dev
        self.created.add(key)
        workspace.place(self.ws["cards"], {key: {"x": op.get("x", 0), "y": op.get("y", 0)}})
        return key, dev

    async def op_create_device(self, _page, op) -> str | None:
        """Make a device; with "connect" (a wire dropped on empty board), wire it to that socket
        as soon as it appears."""
        try:
            key, dev = await self.define(op)
        except ValueError as e:
            return str(e) if e.args else NOT_MADE
        conn, ch = op.get("connect") or {}, dev["channels"]
        if outs := self._socket(conn.get("from"), "outputs"):
            pairs = graph.pair(list(range(len(outs))), list(range(ch)))
            if len(outs) == 2 and ch == 1:  # a new mono device takes one side, as a drop would
                pairs = [(graph.next_side(self.model["wires"], conn["from"]), 0)]
            self.ws["wires"].append({"from": list(conn["from"]), "to": [key, "in"], "pairs": pairs})
            self.mark(conn["from"], [key, "in"])
        if ins := self._socket(conn.get("to"), "inputs"):
            pairs = graph.pair(list(range(ch)), list(range(len(ins))))
            self.ws["wires"].append({"from": [key, "out"], "to": list(conn["to"]), "pairs": pairs})
            self.mark([key, "out"], conn["to"])
        return await self.make(key)

    async def op_insert_device(self, _page, op) -> str | None:
        """Put a new device in the middle of a wire: A → B becomes A → new → B, each channel on
        its own path (a left-only wire stays left-only). The old links go only once the new path
        is made, so the sound does not drop."""
        wire = next((w for w in self.model["wires"] if w["id"] == op.get("wire")), None)
        if not wire:
            return "That wire is gone; the graph changed. Try again."
        try:
            key, dev = await self.define(op)
        except ValueError as e:
            return str(e) if e.args else NOT_MADE
        top = dev["channels"] - 1
        into = sorted({(i, min(j, top)) for i, j in wire["pairs"]})
        out_of = sorted({(min(j, top), j) for _, j in wire["pairs"]})
        self.ws["wires"] = [w for w in self.ws["wires"]
                            if (w["from"], w["to"]) != (wire["from"], wire["to"])]
        self.ws["wires"] += [{"from": list(wire["from"]), "to": [key, "in"], "pairs": into},
                             {"from": [key, "out"], "to": list(wire["to"]), "pairs": out_of}]
        self.replacing[key] = list(wire["links"])
        self.mark(wire["from"], [key, "in"])
        self.mark([key, "out"], wire["to"])
        return await self.make(key)

    async def op_rename_device(self, _page, op) -> str | None:
        """On the board at once; apps see the new name from PipeBoard's next start (apply_names).
        Made again now, the device went away under the players on it, and Spotify paused itself
        (user). Its node.name never changes: apps and WirePlumber remember devices by it."""
        key, name = op.get("key"), str(op.get("name", "")).strip()
        dev = self.ws.get("devices", {}).get(key)
        if not dev or not devices.valid_name(name) or name == dev["name"]:
            return None if dev and name == dev["name"] else "That name will not do."
        workspace.remember(self.ws["cards"].setdefault(key, {}), dev["name"])
        dev["name"] = name
        return None

    async def apply_names(self) -> None:
        """At launch: a mic or speaker renamed since it was made is made again under its new name,
        so apps show it. (Plugins and EQs start with PipeBoard, named; apps never see a gain.)"""
        shown = {o["info"]["props"].get("pipeboard.id"): o["info"]["props"].get("node.description")
                 for o in await self.source.snapshot() if o["type"].endswith("Node")}
        for key, dev in list(self.ws.get("devices", {}).items()):
            card = self.owned(key)
            if dev["type"] in ("mic", "speaker") and card and shown.get(dev["id"]) != dev["name"]:
                self.swapping = True
                try:
                    if error := await self.remake(key, card):
                        log.warning("could not rename %s: %s", dev["name"], error)
                finally:
                    self.swapping = False

    async def remake(self, key: str, card: dict) -> str | None:
        """Make the device again under its new name, wired like the old one before the old one
        goes: a player on it moves over instead of losing its device, and pausing (user). If the
        new node does not show its ports within 3 s, it is wired when it appears, as at a load;
        either way, wires the user made to apps are made then too (stays waiting)."""
        dev, old = self.ws["devices"][key], max(card["serials"])
        apps = {i for c in self.model["cards"] if c["kind"] == "app" for i in c["ids"]}
        self.remade[key] = old
        if error := await self.make(key):  # waiting: wired when it appears, if not here
            return error
        for _ in range(60):
            dump = await self.source.snapshot()
            new = {o["id"] for o in dump if o["type"].endswith("Node")
                   and o["info"]["props"].get("pipeboard.id") == dev["id"]
                   and int(o["info"]["props"].get("object.serial", 0)) > old}
            links = devices.moved_links(dump, set(card["ids"]), new, apps) if new else None
            if links is not None:
                for a, b in links:
                    await self.source.link(a, b)  # one that fails is made when it appears
                break
            await asyncio.sleep(0.05)
        error = await self.source.destroy(card["ids"])
        return f"PipeWire could not rename it: {error}" if error else None

    async def op_delete_device(self, _page, op) -> str | None:
        key = op.get("key")
        if key not in self.ws.get("devices", {}):
            return "PipeBoard only deletes devices it made itself."
        card = self.owned(key)
        self.gone[key] = time.monotonic() + GONE_S
        if self.ws["devices"][key]["type"] in RUNS_WITH:
            error = await (self.plugins.stop(key) if self.ws["devices"][key]["type"] == "plugin"
                           else self.eqs.stop(key))
            if self.source.mode != "live" and card:
                await self.source.destroy(card["ids"])  # the fixture's nodes (a real EQ's go
            self.editors.discard(key)                   # with its process)
        else:
            error = await self.source.destroy(card["ids"]) if card else None
        if error:
            return f"PipeWire could not delete it: {error}"
        del self.ws["devices"][key]
        # Off the graph already read too: the next state is sent before the next read, and showed
        # the deleted card for a moment.
        self.model["cards"] = [c for c in self.model["cards"] if c["key"] != key]
        self.model["wires"] = [w for w in self.model["wires"]
                               if key not in (w["from"][0], w["to"][0])]
        return await self.op_drop(_page, {"key": key})

    async def op_set_gain(self, op) -> None:
        """Move a gain: ramped on the node, remembered on the board (saved once it settles)."""
        key = op.get("key")
        dev = self.ws.get("devices", {}).get(key)
        card = self.owned(key)
        if not dev or dev.get("type") != "gain" or not card:
            return
        dev["gain_db"] = gain.clamp(op.get("db", 0))
        await self.gains.set(card["ids"][0], gain.level(dev), dev["channels"])
        self.save_soon()

    async def op_gain_bypass(self, op) -> None:
        """A gain's power button: bypassed it passes the sound at 0 dB, its level kept (ramped,
        as any change of level)."""
        key = op.get("key")
        dev, card = self.ws.get("devices", {}).get(key), self.owned(key)
        if not dev or dev.get("type") != "gain" or not card:
            return
        dev["bypass"] = bool(op.get("on"))
        await self.gains.set(card["ids"][0], gain.level(dev), dev["channels"])
        self.save_soon()

    def save_soon(self) -> None:
        """Levels move in quick steps: save the board once they settle."""
        if self.saving:
            self.saving.cancel()
        self.saving = asyncio.get_running_loop().call_later(0.5, self.persist)

    # ---- plugins (M8) ---------------------------------------------------------------------------
    def plugin(self, key) -> dict | None:
        dev = self.ws.get("devices", {}).get(key)
        return dev if dev and dev["type"] == "plugin" else None

    async def op_plugin_editor(self, _page, op) -> str | None:
        if self.plugin(op.get("key")):
            self.plugins.send(op["key"], {"editor": bool(op.get("on"))})
        return None

    async def op_plugin_bypass(self, _page, op) -> str | None:
        """Let the sound through untouched, or not; remembered on the board."""
        dev = self.plugin(op.get("key"))
        if dev and dev.get("bypassable"):
            dev["bypass"] = bool(op.get("on"))
            self.plugins.send(op["key"], {"bypass": dev["bypass"]})
        return None

    async def op_plugin_list(self, page, _op) -> str | None:
        """The installed plugins, for the picker. The first listing can take a while (a big VST3
        bundle is discovered once): it is sent when ready, without holding up the page."""
        async def send() -> None:
            found = await self.plugins.installed()
            with contextlib.suppress(ConnectionError):
                await page.send_str(json.dumps({"type": "plugins", "list": [
                    {**p, "ref": plugins.ref(p)} for p in found]}))
        asyncio.create_task(send())
        return None

    # ---- the EQ (M11) ---------------------------------------------------------------------------
    def rate(self) -> float:
        return float((self.model.get("clock") or {}).get("rate") or 48000)

    async def op_eq_set(self, op) -> None:
        """New bands (and bypass) for an EQ: checked, sent to it live, saved once they settle."""
        dev = self.ws.get("devices", {}).get(op.get("key"))
        if not dev or dev["type"] != "eq":
            return
        dev["bands"] = eq.clean(op.get("bands"))
        if "bypass" in op:
            dev["bypass"] = bool(op["bypass"])
        await self.send_eq(op["key"], dev)
        self.save_soon()

    def input_node(self, key: str, dev: dict) -> int | None:
        """An EQ's, limiter's or compressor's input node: its filter-chain's settings live there."""
        card = self.owned(key)
        if card and f"{dev['node']}_in" in card["names"]:
            return card["ids"][card["names"].index(f"{dev['node']}_in")]
        return None

    async def send_eq(self, key: str, dev: dict) -> None:
        if (node := self.input_node(key, dev)) is not None:
            await self.eqs.set(key, dev, node, self.rate())

    async def op_dyn_set(self, op) -> None:
        """A limiter's or compressor's settings: checked, sent live, saved once they settle."""
        dev = self.ws.get("devices", {}).get(op.get("key"))
        if not dev or dev["type"] not in dynamics.KINDS:
            return
        dynamics.update(dev, op)
        await self.send_dyn(op["key"], dev)
        self.save_soon()

    async def send_dyn(self, key: str, dev: dict) -> None:
        if (node := self.input_node(key, dev)) is not None:
            line = dynamics.params(dev)
            await self.gains.command(f"set-param {node} Props {{ params = [ {line} ] }}")

    async def sync_eqs(self, dump: list | None = None) -> None:
        """A profile loaded or the graph's rate changed: running EQs get what the board says.
        With the graph's dump: an EQ whose node just ran for the first time gets all of it."""
        if dump:
            self.eqs.ran({o["info"]["props"].get("node.name") for o in dump
                          if o["type"].endswith("Node") and o["info"].get("state") == "running"})
        for key, dev in self.ws.get("devices", {}).items():
            if dev["type"] == "eq":
                await self.send_eq(key, dev)
            # A dynamics node gets its settings once more when it appears (or comes back): one
            # changed while its process was still starting had no node to go to, and was lost.
            elif dev["type"] in dynamics.KINDS and (node := self.input_node(key, dev)) is not None \
                    and self.dyn_nodes.get(key) != node:
                self.dyn_nodes[key] = node
                await self.send_dyn(key, dev)

    async def op_eq_watch(self, page, op) -> str | None:
        """A page opened or closed an EQ's or dynamics panel: its capture runs only while one
        shows."""
        if op.get("on"):
            self.watching[page] = op.get("key")
        else:
            self.watching.pop(page, None)
        await self.sync_spectrum()
        return None

    async def sync_taps(self, dump: list[dict] | None = None) -> None:
        """The diagnostic cards' capture (diag.Taps): every card that is on and has its monitor,
        while a window shows the board (owner: only then, and off saves the work). The EQ's
        spectrum follows the same windows, so it is kept current here too. Never raises."""
        await self.sync_spectrum()
        try:
            devs = self.ws.get("devices", {})
            if dump is not None:
                nodes = {d["node"]: k for k, d in devs.items() if d["type"] in devices.DIAG}
                self.diag_ports = diag.monitors(dump, nodes)
            want = [(k, ports) for k, ports in sorted(self.diag_ports.items())
                    if k in devs and not devs[k].get("bypass") and len(ports) >= 2]
            want = want if self.visible else []
            if want == (self.taps.cards if self.taps else []):
                return
            if self.taps:
                await self.taps.stop()
            self.taps = (diag.FakeTaps if self.source.mode == "fixture" else diag.Taps)(want) \
                if want else None
            if self.taps:
                self.settle(SETTLE_CAPTURE_S)
                await self.taps.start()
        except Exception:  # noqa: BLE001 - a picture failing must never touch the board
            log.exception("diagnostics: could not set up the capture")

    async def send_taps(self) -> None:
        """What the diagnostic cards heard since the last frame, to the windows that show them."""
        taps = self.taps
        data = taps.take() if taps else b""
        if not data:
            return
        self.count_clips(data, taps)
        if self.counted and time.monotonic() - self.shown_counts > 0.25:  # (a clip shown at once;
            self.shown_counts = time.monotonic()  # saved with the next second's tally)
            await self.broadcast(self.state())
        # Raw floats, binary (owner: the visualizers' load; base64 in JSON cost both ends): the
        # cards' order told once per page, and again when it changes.
        keys, frame = [k for k, _ in taps.cards], diag.MARK + data
        for page in list(self.visible):
            with contextlib.suppress(Exception):
                if self.diag_keys.get(page) != keys:
                    await page.send_str(json.dumps({"type": "diag-keys", "keys": keys}))
                    self.diag_keys[page] = keys
                await page.send_bytes(frame)

    async def op_diag_on(self, _page, op) -> str | None:
        """A diagnostic card's power button: off, nothing is captured or drawn for it."""
        dev = self.ws.get("devices", {}).get(op.get("key"))
        if not dev or dev["type"] not in devices.DIAG:
            return None
        dev["bypass"] = not op.get("on")
        await self.sync_taps()
        return None

    def tally(self, errors: dict[str, int], settling: bool) -> bool:
        """Each Counter's dropouts this second (owner): the most new errors any card on its path,
        or the interface clocking the graph, logged (one glitch reaches them all: counted once),
        apps left out (heard), nothing while
        PipeBoard's own change settles. Counted window or not (owner: it costs nothing). A node
        made again (its total lower) starts over. True if a Counter's totals changed."""
        changed = False
        for key, dev in self.ws.get("devices", {}).items():
            if dev["type"] != "counter" or dev.get("bypass"):
                continue
            base, new = self.tallies.setdefault(key, {}), 0
            for card in upstream(self.model, key) + self.clocking(key):
                total = listed(card, errors)
                if total is None:
                    continue  # (not listed now: its last count stands, see listed)
                was, base[card["key"]] = base.get(card["key"], total), total
                new = max(new, total - was)
            if new > 0 and not settling:
                counts = dev.setdefault("counts", devices.counts())
                counts["drops"] += new
                counts["last_drop"] = round(time.time())
                changed = True
        return changed

    def count_second(self, errors: dict[str, int], settling: bool) -> bool:
        """This second's errors: the Counters' tallies (saved when they changed) and the log line.
        True if a Counter's totals changed."""
        before = {k: d.get("counts", {}).get("drops", 0)
                  for k, d in self.ws.get("devices", {}).items() if d.get("type") == "counter"}
        counted = self.tally(errors, settling) or self.counted
        self.note_drops(errors, settling, before)
        if counted:
            self.counted = False
            self.persist()
        return counted

    def note_drops(self, errors: dict[str, int], settling: bool, before: dict[str, int]) -> None:
        """One log line a second when any card logged new errors: which, how many, and what each
        Counter added (owner: "some don't get counted"; a mismatch can be read back exactly)."""
        new = []
        for card in heard(self.model):
            if (total := listed(card, errors)) is None:
                continue
            was, self.err_seen[card["key"]] = self.err_seen.get(card["key"], total), total
            if total > was:
                new.append(f"{card['title']} +{total - was}")
        if new:
            devs = self.ws.get("devices", {})
            added = [f"{devs[k]['name']} +{devs[k].get('counts', {}).get('drops', 0) - n}"
                     for k, n in before.items() if k in devs]
            log.info("dropouts: %s%s; counters: %s", ", ".join(new),
                     " (settling: not counted)" if settling else "", ", ".join(added) or "none")

    def clocking(self, key: str) -> list[dict]:
        """The interface clocking the graph, for a Counter it does not already feed: when it drops
        out every sound stops for a moment, this one too (owner: the Problems list had 2 dropouts
        the Counter on the Bass EQ chain never counted; the UR44C was downstream of it)."""
        names = set(self.engine.get("interface") or ())
        path = {c["key"] for c in upstream(self.model, key)}
        return [c for c in heard(self.model) if names & set(c["names"]) and c["key"] not in path]

    def count_clips(self, data: bytes, taps: diag.Taps) -> None:
        """A Counter's clipping in this frame: a new clipping moment counts once (sound held at
        full scale for a second is one, not fifty)."""
        devs = self.ws.get("devices", {})
        for i, (key, _) in enumerate(taps.cards):
            if devs.get(key, {}).get("type") != "counter":
                continue
            hit = diag.clips(data, taps.channels, i)
            if hit and key not in self.clipping:
                counts = devs[key].setdefault("counts", devices.counts())
                counts["clips"] += 1
                counts["last_clip"] = round(time.time())
                self.counted = True
            (self.clipping.add if hit else self.clipping.discard)(key)

    async def op_diag_reset(self, _page, op) -> str | None:
        """A Counter's Reset: its totals from now."""
        dev = self.ws.get("devices", {}).get(op.get("key"))
        if dev and dev["type"] == "counter":
            dev["counts"] = devices.counts()
            self.tallies.pop(op["key"], None)
        return None

    async def op_diag_set(self, _page, op) -> str | None:
        """A diagnostic card's own setting: the Waveform's Stereo/Mono, Smooth and Zoom (owner),
        kept with it (profiles count them: profiles._settings)."""
        dev = self.ws.get("devices", {}).get(op.get("key"))
        for field in ("mono", "smooth", "zoom"):
            if dev and dev["type"] == "waveform" and field in op:
                dev[field] = bool(op[field])
        return None

    async def sync_spectrum(self) -> None:
        """One capture, for the EQ, limiter or compressor a visible page has open; none
        otherwise. A dynamics one follows what is wired into it. Never raises."""
        try:
            want = next((k for p, k in self.watching.items() if p in self.visible), None)
            card = self.owned(want) if want else None
            ports, channels = self.watched_ports(want, card)
            now = self.spectrum
            if now and now.key == want and now.ports == ports:
                return
            self.spectrum = None
            if now:
                await now.stop()
            if ports:
                kind = eq.Spectrum if self.source.mode == "live" else eq.FakeSpectrum
                self.spectrum = kind(want, ports, channels)
                await self.spectrum.start()
        except Exception:  # noqa: BLE001
            log.exception("eq: could not set up the spectrum")

    def watched_ports(self, key: str | None, card: dict | None) -> tuple[list, tuple]:
        """A dynamics one: in and out, per side. An EQ's: what goes in summed to one channel and
        what comes out to another (its spectrum is the second; the first tells how far each
        dynamic band moves, M16)."""
        if not card or not card["outputs"]:
            return [], ()
        both = dynamics.capture(self.model, key)
        if self.ws["devices"].get(key, {}).get("type") in dynamics.KINDS:
            return both, dynamics.CAPTURE
        return [(p, "FL" if ch in ("FL", "FR") else "FR") for p, ch in both], ("FL", "FR")

    async def send_spectrum(self) -> None:
        """The EQ's newest audio to the pages that show its panel (their FFT draws it); for a
        limiter's or compressor's panel, the peaks going in and coming out per 10 ms."""
        spec = self.spectrum
        dyn = spec and spec.channels == dynamics.CAPTURE
        data = spec.take(dynamics.BLOCK_BYTES if dyn else 4 * len(spec.channels)) if spec else b""
        if not data:
            return
        if dyn and (not self.aligned or self.aligned.key != spec.key):
            self.aligned = dynamics.Aligned(spec.key)
        kind = self.ws.get("devices", {}).get(spec.key, {}).get("type", "")
        text = json.dumps({"type": "dyn-peaks", "key": spec.key, "peaks": self.aligned.peaks(
                              data, dynamics.delays(kind, self.model.get("clock")))}
                          if dyn else {"type": "eq-pcm", "key": spec.key,
                                       "pcm": base64.b64encode(data).decode()})
        for page, key in list(self.watching.items()):
            if key == spec.key and page in self.visible:
                with contextlib.suppress(Exception):
                    await page.send_str(text)

    def ended(self, key: str, code: int) -> None:
        """An EQ's process ended by itself: its card turns missing; say so."""
        self.heard(key, {"ended": code})

    def heard(self, key: str, msg: dict) -> None:
        """What a plugin's host says: its controls once it runs, a value its own window moved,
        its window opening or closing, or that it failed or stopped."""
        dev = self.ws.get("devices", {}).get(key)
        if not dev or dev["type"] not in RUNS_WITH or ("ended" in msg and self.closing):
            return  # (quitting: they end with PipeBoard, nothing to tell)
        if "ready" in msg:
            ready = msg["ready"]
            dev.update(params=ready["params"], editor=ready["editor"],
                       bypassable=ready.get("bypassable", False), switch=ready.get("switch"))
        elif "param" in msg:
            i, value = msg["param"]
            for p in dev.get("params", []):
                if p["i"] == i:
                    p["value"] = round(value, 6)
            if (dev.get("switch") or {}).get("i") == i:  # its own bypass, flipped in its window
                dev["bypass"] = abs(value - dev["switch"]["on"]) < 0.5
        elif "editor" in msg:
            (self.editors.add if msg["editor"] else self.editors.discard)(key)
        else:
            self.editors.discard(key)
            why = msg.get("error") or f"its process ended ({msg.get('ended')})"
            text = f"{dev['name']} stopped: {why}"
            log.warning("%s", text)
            asyncio.ensure_future(self.broadcast(json.dumps({"type": "toast", "text": text,
                                                             "level": "error"})))
        self.save_soon()
        self.changed()

    async def op_create_missing(self, _page, op) -> str | None:
        key = op.get("key")
        if key not in self.ws.get("devices", {}) or key in self.model.get("present", []):
            return None
        return await self.make(key)

    # ---- profiles ---------------------------------------------------------------------------
    def _set(self, **changes) -> None:
        self.settings.update(changes)
        self.store.save_settings(self.settings)

    async def apply(self, plan: dict) -> str | None:
        """Make the system match a board: make and remove links; return the first complaint."""
        errors = [e for m in plan["make"] if (e := await self.source.link(m["out"], m["in"]))]
        errors += [e for r in plan["remove"] if (e := await self.source.unlink(r["link"]))]
        return f"PipeWire refused a change: {errors[0]}" if errors else None

    async def load(self, name: str) -> str | None:
        saved = self.store.read(name)
        if saved is None:
            return f"There is no profile called {name}."
        counted = {k: d["counts"] for k, d in self.ws.get("devices", {}).items()
                   if "counts" in d}
        self.ws = {**saved, "cards": saved.get("cards", {}), "wires": saved.get("wires", [])}
        for key, counts in counted.items():  # (a Counter's totals run on, not the profile's)
            self.ws.get("devices", {}).get(key, {})["counts"] = counts
        self._set(active=name)
        present = set(self.model.get("present", []))
        errors = [e for key in self.ws.get("devices", {}) if key not in present
                  and key not in self.waiting  # already on its way (launch: started just before)
                  and (e := await self.make(key))]  # the profile's own devices, made again
        await self.apply_settings(present)
        plan = profiles.plan(self.ws, self.model)
        waiting = {lid for _, links in self.around_wait for lid in links}  # still carry the sound
        plan["remove"] = [r for r in plan["remove"] if r["link"] not in waiting]
        applied = await self.apply(plan)  # the wiring, even if a device failed to be made
        return errors[0] if errors else applied

    async def apply_settings(self, present: set[str]) -> None:
        """A profile loaded (or a Revert): the running gains, EQs and plugins get its settings.
        Before, only the board changed for a gain (its level stayed) and a plugin's bypass."""
        for key, dev in self.ws.get("devices", {}).items():
            card = self.owned(key) if key in present else None
            if not card:
                continue  # made again with these settings when it appears
            if dev["type"] == "gain":
                await self.gains.set(card["ids"][0], gain.level(dev),
                                     dev["channels"])
            elif dev["type"] == "eq":
                await self.send_eq(key, dev)
            elif dev["type"] in dynamics.KINDS:
                await self.send_dyn(key, dev)
            elif dev["type"] == "plugin":
                self.plugins.send(key, {"values": {str(p["i"]): p["value"]
                                                   for p in dev.get("params", [])}})
                if dev.get("bypassable"):
                    self.plugins.send(key, {"bypass": bool(dev.get("bypass"))})

    async def op_save(self, _page, op) -> str | None:
        name = str(op.get("name") or self.profile_state()["active"] or "").strip()
        if not profiles.valid_name(name):
            return "A profile name is letters, digits, spaces and - _ ( ), up to 40."
        self.store.write(name, self.ws)
        self._set(active=name)
        return None

    async def op_plan(self, page, op) -> str | None:
        saved = self.store.read(str(op.get("name", ""))) if profiles.valid_name(op.get("name")) \
            else None
        if saved is None:
            return "That profile is gone."
        plan = profiles.plan(saved, self.model)
        await page.send_str(json.dumps({
            "type": "plan", "name": op["name"], "missing": plan["missing"],
            "make": profiles.texts(plan["make"]), "remove": profiles.texts(plan["remove"])}))
        return None

    async def op_load(self, _page, op) -> str | None:
        return await self.load(str(op.get("name", ""))) if profiles.valid_name(op.get("name")) \
            else "That profile is gone."

    async def op_revert(self, _page, _op) -> str | None:
        """Undo exactly what the changes card lists: links added since the save go, whatever they
        connect (loading a profile spares cards it does not know; a revert must not), and
        PipeBoard's own devices made since the save are deleted; then the saved board returns."""
        active = self.settings["active"]
        saved = self.store.read(active) if active in self.store.names() else None
        if saved is None:
            return None
        ends = lambda w: (tuple(w["from"]), tuple(w["to"]))  # noqa: E731
        kept = {ends(w) for w in saved.get("wires", [])}
        added = [link for w in self.model["wires"] if ends(w) not in kept
                 and not profiles.routed(self.ws, w) for link in w["links"]]
        errors = [e for link in added if (e := await self.source.unlink(link))]
        # Only devices created since the save, in this session: never one that was already there
        # (taken back from the system, or from an older board) — a Revert once deleted two gains
        # the board had just taken back.
        for key in (set(self.ws.get("devices", {})) - set(saved.get("devices", {}))) & self.created:
            card = self.owned(key)
            if card and (e := await self.source.destroy(card["ids"])):
                errors.append(e)
        if errors:
            return f"Could not undo everything: {errors[0]}"
        return await self.load(active)

    async def op_delete(self, _page, op) -> str | None:
        name = str(op.get("name", ""))
        if not profiles.valid_name(name):
            return None
        self.store.delete(name)
        self._set(active=None if self.settings["active"] == name else self.settings["active"],
                  launch=None if self.settings["launch"] == name else self.settings["launch"])
        return None

    async def op_launch(self, _page, op) -> str | None:
        name = op.get("name")
        if name is not None and name not in self.store.names():
            return "That profile is gone."
        self._set(launch=name)
        return None

    async def op_retarget(self, page, op) -> str | None:
        card = next((c for c in self.model["cards"] if c["key"] == op.get("to")), None)
        if card is None or op.get("key") not in self.ws["cards"]:
            return "That device is gone; the graph changed. Try again."
        dropped = profiles.retarget(self.ws, op["key"], card)
        if dropped:
            await toast(page, "Could not keep: " + "; ".join(dropped), "info")
        return await self.apply(profiles.plan(self.ws, self.model))

    async def op_drop(self, _page, op) -> str | None:
        key = op.get("key")
        self.ws["cards"].pop(key, None)
        self.ws.get("devices", {}).pop(key, None)  # forgetting a missing device of our own
        self.ws["wires"] = [w for w in self.ws["wires"] if key not in (w["from"][0], w["to"][0])]
        return None

    async def op_hide(self, _page, op) -> str | None:
        """Off the board: the wires the user made to it are set aside on the card and unplugged
        (the system's stay). Back on the board: they are made again, where the other end exists."""
        key = op.get("key")
        if not key:
            return None
        card = self.ws["cards"].setdefault(str(key), {})
        made = set(self.ws.get("made", []))
        if op.get("hidden"):
            mine = [w for w in self.model["wires"] if w["id"] in made
                    and key in (w["from"][0], w["to"][0])]
            card["parked"] = card.get("parked", []) + [
                {"from": w["from"], "to": w["to"], "pairs": w["pairs"]} for w in mine]
            links = [link for w in mine for link in w["links"]]
            errors = [e for link in links if (e := await self.source.unlink(link))]
            card["hidden"] = True
            return f"PipeWire refused to disconnect: {errors[0]}" if errors else None
        parked = card.pop("parked", [])
        card["hidden"] = False
        plan = profiles.plan({**self.ws, "wires": parked}, self.model)
        for w in parked:
            self.mark(w["from"], w["to"])
        return await self.apply({"make": plan["make"], "remove": []})

    async def op_forget_wire(self, _page, op) -> str | None:
        """A wire that waits for a missing device: drop it from the board."""
        self.ws["wires"] = [w for w in self.ws["wires"]
                            if (w["from"], w["to"]) != (op.get("from"), op.get("to"))]
        return None

    # ---- live wires ---------------------------------------------------------------------------
    def scope_sources(self) -> list[tuple[str, str, list[int]]]:
        """Every output socket with a wire to a card on the board."""
        hidden = {c["key"] for c in self.model["cards"]
                  if self.ws["cards"].get(c["key"], {}).get("hidden", c["hidden"])}
        cards = {c["key"]: c for c in self.model["cards"]}
        found = {}
        for w in self.model["wires"]:
            (key, sock), to = w["from"], w["to"][0]
            if key in hidden or to in hidden or (key, sock) in found:
                continue
            ports = next((s["ports"] for s in cards[key]["outputs"] if s["id"] == sock), None)
            if ports:
                found[(key, sock)] = (key, sock, list(ports))
        return sorted(found.values())

    def recorded_serials(self, sources: list) -> dict[str, tuple]:
        cards = {c["key"]: c for c in self.model["cards"]}
        return {k: tuple(cards[k]["serials"]) for k, _, _ in sources if k in cards}

    def scope_sources_sent(self) -> list:
        return [src and [src[0], src[1]] for src in self.scope.sources] if self.scope else []

    async def sync_scope(self) -> None:
        """Run the capture for exactly the wires on the board, and only when someone looks.

        Never raises: live wires failing must leave the board working, with plain wires.
        """
        async with self.scope_lock:
            await self._sync_scope()

    async def _sync_scope(self) -> None:
        try:
            want = self.settings["live"] and bool(self.visible)
            sources = self.scope_sources() if want else []
            key = tuple((k, s, tuple(p)) for k, s, p in sources)
            dead = self.scope is not None and not self.scope.alive
            serials = self.recorded_serials(sources)
            # A card made again (WirePlumber's restart, an effect started fresh) can come back with
            # the very port ids it had: the same key, but the capture's links died with the old
            # node, and its wires went still until Animate was toggled (owner). A fresh capture.
            remade = any(self.scope_serials.get(k, v) != v for k, v in serials.items())
            if key == self.scope_key and not dead and not remade:
                return
            self.scope_serials = serials
            if not dead and not remade and await self.follow_scope(key, sources):
                return
            if dead and key == self.scope_key:
                if self.scope_tries >= 5 or time.monotonic() - self.scope.dead_since < 1:
                    return  # a second's rest between tries; after five, give up quietly
                self.scope_tries += 1
            else:
                self.scope_tries = 0
            await self.stop_scope()
            await self.start_scope(key, sources)
        except Exception:  # noqa: BLE001
            log.exception("live wires: could not set up the capture")

    async def start_scope(self, key: tuple, sources: list) -> None:
        self.scope_key = key
        kind = scopes.FakeScope if self.source.mode == "fixture" else scopes.Scope
        fresh = kind(sources) if sources else None
        # The page learns the new sources before the first frame that uses them.
        sent = [src and [src[0], src[1]] for src in fresh.sources] if fresh else []
        await self.broadcast(json.dumps({"type": "scope", "sources": sent, "fresh": True}))
        if fresh:
            self.scope, self.scope_seen = fresh, (0, time.monotonic())
            self.scope_started = time.monotonic()
            self.settle(SETTLE_CAPTURE_S)
            await fresh.start()

    async def follow_scope(self, key: tuple, sources: list) -> bool:
        """The running capture takes the new sources in: its count runs on, so the page keeps
        every wire's sound. False: it cannot (see Scope.update), a fresh one is needed."""
        if not (self.scope and sources and await self.scope.update(sources)):
            return False
        self.settle(SETTLE_CAPTURE_S)  # (its new links set the graph up anew too)
        self.scope_key = key
        await self.broadcast(json.dumps(
            {"type": "scope", "sources": self.scope_sources_sent(), "fresh": False}))
        return True

    async def stop_scope(self) -> None:
        """Called with scope_lock held (from _sync_scope) or at shutdown."""
        if self.scope:
            scope, self.scope = self.scope, None
            await scope.stop()

    async def send_frame(self) -> None:
        """One frame of live wires to the visible pages; notices clipping and a dead capture."""
        scope = self.scope
        if scope is None:
            return
        self.note_stall(scope)
        if not scope.alive:
            # When it died, noted once: noting it on every frame meant the retry never came.
            scope.dead_since = scope.dead_since or time.monotonic()
            await self.sync_scope()
            return
        frame, clipping = scope.frame()
        await self.note_clipping(scope, clipping)
        if frame:
            for page in list(self.visible):
                try:  # one page failing must not cost the others their frame
                    await page.send_bytes(frame)
                except Exception:  # noqa: BLE001
                    log.debug("live wires: a page did not take its frame", exc_info=True)

    def note_stall(self, scope: scopes.Scope) -> None:
        """A capture that delivers nothing for 2 s is treated as dead, whatever stopped it."""
        if scope.total != self.scope_seen[0]:
            self.scope_seen = (scope.total, time.monotonic())
            if time.monotonic() - self.scope_started > 10:
                self.scope_tries = 0  # healthy for a while: a later hiccup starts a fresh count
        elif time.monotonic() - self.scope_seen[1] > 2 and not scope.failed:
            log.warning("live wires: no data for 2 s; restarting the capture")
            scope.failed = True

    async def note_clipping(self, scope: scopes.Scope, clipping: set[int]) -> None:
        """Keep a clip on show for 2.5 s after the last one; republish when that set changes."""
        now, before = time.monotonic(), set(self.clipped)
        for idx in clipping:
            self.clipped[tuple(scope.sources[idx][:2])] = now
        for k in [k for k, t in self.clipped.items() if now - t > 2.5]:
            del self.clipped[k]
        if set(self.clipped) != before:
            text = self.state()
            if text != self.last:
                self.last = text
                await self.broadcast(text)

    async def forget(self, page: web.WebSocketResponse) -> None:
        self.pages.discard(page)
        self.drop_pages.discard(page)
        self.visible.discard(page)
        self.watching.pop(page, None)
        self.diag_keys.pop(page, None)
        await self.sync_scope()
        await self.sync_taps()

    async def op_live(self, page, op) -> str | None:
        """A page says whether it is visible: nothing is captured for a hidden window."""
        (self.visible.add if op.get("visible") else self.visible.discard)(page)
        await self.sync_scope()
        await self.sync_taps()
        return None

    async def op_autostart(self, _page, op) -> str | None:
        error = await autostart.set_enabled(bool(op.get("on")))
        self.autostart = await autostart.enabled()
        return error

    async def op_take_over(self, page, op) -> str | None:
        """Make a service's virtual devices PipeBoard's own (takeover.py), as confirmed."""
        unit = op.get("unit")
        cards = self.offer.get(unit)
        if not cards:
            return "There is nothing to take over from that service."
        names = {takeover.node_name(c) for c in cards}
        error = await self.source.stop_service(unit)
        if error:
            return f"Nothing was changed: {error}"
        for _ in range(50):  # until the service's devices are gone from the graph
            if not names & {n for c in graph.build(await self.source.snapshot())["cards"]
                            for n in c["names"]}:
                break
            await asyncio.sleep(0.1)
        keys = takeover.rewrite(self.ws, cards)
        self.persist()
        errors = [e for key in keys if (e := await self.make(key))]
        if errors:
            return f"{unit} is stopped, but {errors[0]}"
        await toast(page, f"PipeBoard now makes the {len(keys)} devices {unit} made.", "info")
        return None

    async def op_cstates(self, page, op) -> str | None:
        """Which CPU idle states are off (Audio engine › Advanced): as root through pkexec, in the
        background so a password prompt never holds the board up; the panel shows the outcome."""
        if self.source.mode != "live":
            return "CPU idle states can only be changed on the real system."
        try:
            off = engine.check_off(op.get("off"), [s["name"] for s in engine.cstates()["states"]])
        except ValueError as e:
            return str(e)

        async def change() -> None:
            error = await self.idle.set(off)
            # (each attempt counted: a closed password prompt changes nothing, and the page must
            # still learn it is over)
            tries = self.engine.get("cstates", {}).get("tries", 0) + 1
            self.engine["cstates"] = {**engine.cstates(), "tries": tries}
            await self.broadcast(self.state())
            if error:
                await toast(page, error)
        asyncio.create_task(change())
        return None

    async def op_drops(self, page, op) -> str | None:
        """A page opens or closes the path panel: its cards' dropouts are counted meanwhile."""
        (self.drop_pages.add if op.get("on") else self.drop_pages.discard)(page)
        if self.drop_pages and not self.drops_task:
            self.drops_task = asyncio.create_task(self.watch_drops())
        return None

    async def watch_errors(self) -> None:
        """Every second each card's dropout count, a minute of it kept: the Dropouts problem (owner:
        it must show as they happen; pw-top takes ~3 ms). A node made again counts from 0: its
        history starts over. Never raises."""
        seen: dict[str, list] = {}
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            if tick % 60 == 0:
                note_open_files()
            if self.source.mode != "live" or self.closing:
                continue
            try:
                errors, now = await engine.node_errors(), time.monotonic()
                if now < self.settling:  # an engine change's own burst: counted afresh after
                    seen.clear()
                recent = {} if now < self.settling else {
                    c["key"]: n for c in heard(self.model)
                    if (n := last_minute(seen, c, errors, now))}
                counted = self.count_second(errors, now < self.settling)
                if recent != self.dropped or counted:
                    self.dropped = recent
                    await self.broadcast(self.state())
                    self.tell_tray()
            except Exception:  # noqa: BLE001 - a count failing must never touch the board
                log.exception("dropouts: could not count")

    async def watch_drops(self) -> None:
        """Every 2 s, each card's dropouts since the panel opened (summed over its nodes), sent to
        the pages that show it (owner: a live drop counter on devices). Nothing runs while no
        panel is open. A node made again starts its count afresh: the start follows it down."""
        start: dict[str, int] = {}
        try:
            while self.drop_pages:
                errors = await engine.node_errors() if self.source.mode == "live" else {}
                if time.monotonic() < self.settling:  # (an engine change's own burst: not counted)
                    start.clear()
                counts = {}
                for c in heard(self.model):
                    mine = [errors[n] for n in c["names"] if n in errors]
                    if mine:
                        start[c["key"]] = min(start.get(c["key"], sum(mine)), sum(mine))
                        counts[c["key"]] = sum(mine) - start[c["key"]]
                text = json.dumps({"type": "drops", "counts": counts})
                for page in list(self.drop_pages):
                    with contextlib.suppress(ConnectionError, RuntimeError):
                        await page.send_str(text)
                await asyncio.sleep(2.0)
        finally:
            self.drops_task = None

    async def op_live_instant(self, _page, op) -> str | None:
        self._set(live_instant=bool(op.get("on")))
        return None

    async def op_show_latency(self, _page, op) -> str | None:
        self._set(show_latency=bool(op.get("on")))
        return None

    async def op_live_setting(self, _page, op) -> str | None:
        self._set(live=bool(op.get("on")))
        await self.sync_scope()
        return None

    # ---- wires ------------------------------------------------------------------------------
    async def op_connect(self, _page, op) -> str | None:
        return await self.connect(op.get("from"), op.get("to"))

    async def op_move_wire(self, _page, op) -> str | None:
        """Plug a wire's input end into another input, keeping which channel feeds which."""
        wire = next((w for w in self.model["wires"] if w["id"] == op.get("wire")), None)
        ins = self._socket(op.get("to"), "inputs")
        outs = self._socket(wire["from"], "outputs") if wire else None
        if not wire or not ins or not outs:
            return "That wire or socket is gone; the graph changed. Try again."
        pairs = {(outs[i], ins[min(j, len(ins) - 1)]) for i, j in wire["pairs"]}
        errors = [e for o, i in sorted(pairs) if (e := await self.source.link(o, i))
                  and "exists" not in e.lower()]
        if errors:
            return f"PipeWire refused the connection: {errors[0]}"
        self.mark(wire["from"], op["to"])
        return await self.disconnect(wire["id"])

    async def op_disconnect(self, _page, op) -> str | None:
        """One wire, or every wire of a card (its menu's Disconnect; waiting ghosts stay)."""
        if key := op.get("card"):
            ids = [w["id"] for w in self.model["wires"]
                   if key in (w["from"][0], w["to"][0]) and not w.get("ghost")]
            errors = [e for wid in ids if (e := await self.disconnect(wid))]
            return errors[0] if errors else None
        return await self.disconnect(op.get("wire"))

    def _socket(self, end, side: str) -> list[int] | None:
        if not (isinstance(end, list) and len(end) == 2):
            return None
        card = next((c for c in self.model["cards"] if c["key"] == end[0]), None)
        sock = next((s for s in card[side] if s["id"] == end[1]), None) if card else None
        return sock["ports"] if sock else None

    def mark(self, src, dst, made: bool = True) -> None:
        """Remember (or forget) that the user made this wire here: removing a card from the board
        sets aside only wires they made, never the system's (WirePlumber's, a script's)."""
        wid = f"{src[0]}:{src[1]}>{dst[0]}:{dst[1]}"
        mine = [i for i in self.ws.get("made", []) if i != wid]
        self.ws["made"] = mine + [wid] if made else mine
        # Unplugged by the user: a change even when the system made it (profiles.changes).
        cut = [i for i in self.ws.get("cut", []) if i != wid]
        self.ws["cut"] = cut if made else cut + [wid]

    async def connect(self, src, dst) -> str | None:
        outs, ins = self._socket(src, "outputs"), self._socket(dst, "inputs")
        if not outs or not ins:
            return "That socket is gone; the graph changed. Try again."
        pairs = graph.pair(outs, ins)
        if len(outs) == 2 and len(ins) == 1:  # stereo into mono: L, then R, one wire each (user)
            pairs = [(outs[graph.next_side(self.model["wires"], src)], ins[0])]
        errors = [e for o, i in pairs if (e := await self.source.link(o, i))
                  and "exists" not in e.lower()]
        if errors:
            return f"PipeWire refused the connection: {errors[0]}"
        self.mark(src, dst)
        return None

    # ---- the audio engine (§8c): block size, safety margin, priority; tried for a minute -------
    def trial_state(self) -> dict | None:
        """The setting being tried: its values, what it replaced, seconds left, and whether the
        graph now runs what was asked (PipeWire may not have taken a file's setting)."""
        trial = self.settings.get("engine_trial")
        if not trial:
            return None
        v, now = trial["values"], self.engine
        # (on the timer PipeWire pads the margin itself: only the block and the mode are asked)
        keys = ("block", "tsched") if v.get("tsched") else ("block", "margin", "tsched")
        took = self.source.mode != "live" or all(now.get(k) == v.get(k) for k in keys)
        return {"values": v, "was": trial["was"], "took": took,
                "left": max(0, round(trial["until"] - time.time())),
                "dropouts": self.trial_drops[1] - self.trial_drops[0]
                if None not in self.trial_drops else None}

    async def op_engine_try(self, _page, op) -> str | None:
        """New engine settings for a minute: written, and put into the running PipeWire."""
        if self.settings.get("engine_trial"):
            return "A setting is being tried: keep or revert it first."
        if not self.engine.get("interface"):
            return "No audio interface clocks the graph: there is nothing to set."
        try:
            values = engine.check(op)
        except ValueError as e:
            return str(e)
        live = self.source.mode == "live"
        was = {k: self.engine.get(k) for k in values}
        # the margin asked for, in blocks (on the timer PipeWire reports a padded one)
        was["margin"] = self.engine.get("margin_blocks", 1) * (was["block"] or 1)
        self._set(engine_trial={
            "values": values, "was": was,
            "before": engine.current() if live else {}, "until": time.time() + TRIAL_S})
        if not live:  # the fixture: the bar and its countdown, nothing written or restarted
            self.watch_trial()
            return None
        engine.write(engine.texts(values, self.engine["interface"]))
        self.watch_trial()
        return await self.engine_apply(values, self.settings["engine_trial"]["was"])

    async def engine_apply(self, values: dict, was: dict) -> str | None:
        """Apply engine values (engine.apply). When that restarts WirePlumber, the board's wires
        are made again: its start sets up every audio node's ports anew, which drops every link
        on them, not only the interface's (owner: Spotify reached Music Mix and went no further;
        16 wires between PipeBoard's own nodes were gone, their ports far newer than the nodes)."""
        before = copy.deepcopy(self.ws)
        restart = engine.reopens(values, was)
        self.settle(SETTLE_DROPS_S)
        # PipeBoard's effects are stopped first and started fresh after: WirePlumber's restart
        # sets their ports up anew while they run, they fail ("error changing node state"), and
        # PipeWire restarted the interface's stream over and over until they recovered, ~50 s
        # of silence (owner: "can we make the sound come back a lot sooner").
        present = self.model.get("present", [])
        effects = [k for k, d in self.ws.get("devices", {}).items()
                   if d["type"] in FILTERS and k in present] if restart else []
        if restart:
            await self.set_starting("reopen")
        self.closing = True  # (stopped on purpose: no "it stopped" warning)
        for key in effects:
            await self.eqs.stop(key)
        self.closing = False
        error = await engine.apply(values, was, await self.source.snapshot())
        if restart:
            await self.set_starting("wires")
        for key in effects:
            error = await self.make(key) or error
        if restart:
            error = await self.rewire(before, set(present)) or error
            self.listen = asyncio.create_task(self.wait_sound(self.engine.get("interface", [])))
        self.changed()  # read the engine again: the trial bar says if it did not take
        if error:
            log.warning("audio engine: %s", error)
            return f"Not all of it could be set: {error}"
        return None

    async def set_starting(self, step: str) -> None:
        """A step of a reopened interface coming back (reopen, wires, sound), shown in the bar
        (owner: no sound for a while after a change, and nothing said why). A new change
        stops the wait for the last one's sound."""
        if step == "reopen" and self.listen:
            self.listen.cancel()
            self.listen = None
        since = self.starting["since"] if self.starting and step != "reopen" else time.monotonic()
        self.starting = {"step": step, "since": since}
        await self.broadcast(self.state())

    def starting_state(self) -> dict | None:
        s = self.starting
        if not s:
            return None
        if "back" in s:
            return {"back": s["back"]}
        return {"step": s["step"], "elapsed": round(time.monotonic() - s["since"], 1)}

    async def wait_sound(self, interface: list[str]) -> None:
        """Until the interface's sound is back (engine.sound_back; how long differs per device:
        the owner's UR44C ~13 s). The trial's minute and its dropouts start then; for a moment
        the bar says how long it took."""
        await self.set_starting("sound")
        back = await engine.sound_back(interface)
        trial = self.settings.get("engine_trial")
        if trial:
            self._set(engine_trial={**trial, "until": time.time() + TRIAL_S})
            self.trial_drops = (None, None)
        self.starting = {"back": round(time.monotonic() - self.starting["since"])} if back else None
        await self.broadcast(self.state())
        if back:
            await asyncio.sleep(BACK_SHOWN_S)
            self.starting = None
            await self.broadcast(self.state())
        self.listen = None

    async def op_engine_keep(self, _page, _op) -> str | None:
        if self.trial_task:
            self.trial_task.cancel()
            self.trial_task = None
        self._set(engine_trial=None)
        return None

    async def op_engine_back(self, _page, _op) -> str | None:
        return await self.engine_back()

    async def rewire(self, board: dict, back: set | None = None) -> str | None:
        """Make a board's wires again (not the apps' own: WirePlumber links those), as soon as
        their ports are there: looked at every quarter second, done after two looks in a row
        found nothing to make (WirePlumber may still be setting up ports and drop a link just
        made; a fixed second's wait first made the sound come back that much later: owner).
        The marks of wires the user made, lost while they were gone, come back with them.
        `back`: the cards there before; not done until each is back with its sockets (the effects
        are new processes: their wires, skipped while they were not up yet, waited for an
        ordinary refresh behind the live-wires capture, and the sound came 14 s later: owner)."""
        mine = {**board, "wires": [w for w in board["wires"] if not profiles.routed(board, w)]}
        ends = [w for w in mine["wires"] if {w["from"][0], w["to"][0]} <= (back or set())]
        clean = 0
        for _ in range(40):  # 10 s at most
            await asyncio.sleep(0.25)
            # Its own look at the graph, not a refresh(): that waited behind the live-wires
            # capture, restarting every 3 s while its links were gone, and a quarter second
            # became 14 s (owner: "still taking long").
            dump = await self.source.snapshot()
            model = self.own(graph.build(dump), dump)
            make = profiles.plan(mine, model)["make"]
            waiting = [w for w in ends if profiles.ports(model, w["from"], "outputs") is None
                       or profiles.ports(model, w["to"], "inputs") is None]
            if not make and not waiting:
                clean += 1
                if clean == 2:
                    break
                continue
            clean = 0
            log.info("audio engine: making %d links of the board again", len(make))
            await self.apply({"make": make, "remove": []})
        else:
            return "Some of the board's wires could not be made again: Revert brings them back."
        self.ws["made"] = sorted(set(self.ws.get("made", [])) | set(board.get("made", [])))
        self.changed()
        return None

    async def engine_back(self) -> str | None:
        """Go back: the files as they were before the trial (none, or the last kept), and the
        values that ran then, at once."""
        trial = self.settings.get("engine_trial")
        if self.trial_task:
            self.trial_task.cancel()
            self.trial_task = None
        self._set(engine_trial=None)
        if trial and self.source.mode == "live":
            engine.write(trial["before"])
            return await self.engine_apply(trial["was"], trial["values"])
        return None

    def watch_trial(self) -> None:
        """Back when the minute is up, with or without a window open (the server keeps time);
        meanwhile the interface's dropouts counted, so the bar shows them (owner: a bad setting
        should be plain before Keep)."""
        trial = self.settings.get("engine_trial")

        async def wait() -> None:
            self.trial_drops = (None, None)
            # (the minute starts again when a reopened interface's sound is back: wait_sound)
            while time.time() < (self.settings.get("engine_trial") or trial)["until"]:
                await asyncio.sleep(2.0)
                if self.source.mode == "live" and not self.closing:
                    await self.count_drops()
            self.trial_task = None  # (engine_back must not cancel the task it runs in)
            log.info("audio engine: no Keep within the minute; going back")
            await self.engine_back()
        if trial:
            self.trial_task = asyncio.create_task(wait())

    async def count_drops(self) -> None:
        count = await engine.dropouts(self.engine.get("interface", []))
        if count is None:
            return
        first = self.trial_drops[0] if self.trial_drops[0] is not None else count
        if (first, count) != self.trial_drops:
            self.trial_drops = (first, count)
            await self.broadcast(self.state())

    async def stop_children(self, around: bool = True) -> None:
        """Stop what PipeBoard runs: the capture, the gains' pw-cli, plugins, EQs and effects
        (first, when quitting, the sound routed around them)."""
        await self.stop_scope()  # the capture never outlives the app
        if around:
            await self.route_around()  # before the EQs and plugins stop: the sound keeps going
        await self.gains.stop()
        await self.plugins.stop_all()
        await self.eqs.stop_all()
        await self.idle.stop()  # (the root helper ends with PipeBoard)
        if self.listen:  # (its capture ends with PipeBoard too)
            self.listen.cancel()
        if self.taps:
            await self.taps.stop()
        if self.spectrum:
            await self.spectrum.stop()

    # ---- quitting: sound goes around the EQ and plugins, which stop with PipeBoard (M12) --------
    async def route_around(self) -> None:
        """Before they stop: link what feeds each running EQ/plugin straight to where its sound
        went, and note it on the board for the next launch to undo. Never raises."""
        try:
            through = {k for k, d in self.ws.get("devices", {}).items()
                       if d["type"] in RUNS_WITH and k in self.model.get("present", [])}
            wires = graph.around(self.model, through) if through else []
            for w in wires:
                outs, ins = self._socket(w["from"], "outputs"), self._socket(w["to"], "inputs")
                for i, j in w["pairs"]:
                    await self.source.link(outs[i], ins[j])
            self.ws["around"] = [{**w, "through": sorted(through)} for w in wires]
            self.persist()
            log.info("quitting: routed around %s with %d wires", sorted(through), len(wires))
        except Exception:  # noqa: BLE001 - quitting must go on
            log.exception("could not route around the EQs and plugins")

    def undo_around(self) -> None:
        """At launch: the links made around the EQs and plugins go once each is back and its own
        wires are made again (as for a node inserted into a wire: no gap)."""
        around, self.around = self.around, []
        for w in around:
            live = next((m for m in self.model["wires"]
                         if m["from"] == w["from"] and m["to"] == w["to"]), None)
            links = [lid for lid, pair in zip(live["links"], live["pairs"], strict=True)
                     if list(pair) in w["pairs"]] if live else []
            if links:
                self.around_wait.append((set(w["through"]), links))

    async def disconnect(self, wire_id) -> str | None:
        wire = next((w for w in self.model["wires"] if w["id"] == wire_id), None)
        if not wire:
            return None  # already gone
        self.mark(wire["from"], wire["to"], made=False)
        errors = [e for lid in wire["links"] if (e := await self.source.unlink(lid))]
        return f"PipeWire refused to disconnect: {errors[0]}" if errors else None


FD_WARN = [200, 400, 800]  # open files at which the log says what they are (left to warn at)


def note_open_files() -> None:
    """PipeBoard's open files, looked at once a minute: past 200, 400 and 800 the log says what
    kinds they are and which processes it runs (2026-11-06: it ran out of files after ~20 min,
    "Too many open files", and could not quit; the leak was not found by reading the code)."""
    kinds = Counter()
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):  # (the listing's own, gone by now)
            kinds[re.sub(r"\d+", "N", os.readlink(f"/proc/self/fd/{fd}"))] += 1
    n = sum(kinds.values())
    if FD_WARN and n >= FD_WARN[0]:
        while FD_WARN and n >= FD_WARN[0]:
            FD_WARN.pop(0)
        me = os.getpid()
        kids = Counter(_comm(c) for c in Path(f"/proc/{me}/task/{me}/children").read_text().split())
        log.warning("open files: %d %s; processes %s", n, dict(kinds.most_common(6)), dict(kids))


def _comm(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "?"


def listed(card: dict, errors: dict[str, int]) -> int | None:
    """A card's error count now, or None when pw-top does not list any of its nodes: it lists only
    the active ones. Taken as 0, a node idle for a while and back with its old count looked like
    that many new dropouts at once (owner: a Counter jumped by 1495 when the window came back)."""
    mine = [errors[n] for n in card["names"] if n in errors]
    return sum(mine) if mine else None


def upstream(model: dict, key: str) -> list[dict]:
    """The cards whose sound reaches this one, through any number of wires (a Counter's path),
    apps left out (heard)."""
    feeds: dict[str, set] = {}
    for w in model["wires"]:
        feeds.setdefault(w["to"][0], set()).add(w["from"][0])
    found, todo = set(), [key]
    while todo:
        for k in feeds.get(todo.pop(), ()):
            if k not in found and k != key:
                found.add(k)
                todo.append(k)
    return [c for c in heard(model) if c["key"] in found]


def heard(model: dict) -> list[dict]:
    """The cards whose dropouts count: not apps. An app's stream logs one whenever the app itself
    is late (owner: Spotify each time it changes songs, nothing else did): its own gap, not the
    sound path's, and no block or margin would change it."""
    return [c for c in model["cards"] if c["kind"] != "app"]


def last_minute(seen: dict, card: dict, errors: dict, now: float) -> int:
    """A card's dropouts in the last minute, its count history in `seen` kept up to date (a node
    made again counts from 0: its history starts over)."""
    mine = [errors[n] for n in card["names"] if n in errors]
    if not mine:
        return 0
    hist = seen.setdefault(card["key"], [])
    if hist and sum(mine) < hist[-1][1]:
        hist.clear()
    hist.append((now, sum(mine)))
    hist[:] = [h for h in hist if now - h[0] <= 60]
    return sum(mine) - hist[0][1]


async def startup(board: Board) -> None:
    """Read the system, then load the profile chosen to load at launch, if there is one; an audio
    engine trial still running (PipeBoard was quit during it) keeps counting, or goes back."""
    board.autostart = await autostart.enabled()
    await board.refresh()
    board.first_profile()
    board.undo_around()
    for key, dev in list(board.ws.get("devices", {}).items()):  # these end with PipeBoard
        if dev["type"] in RUNS_WITH and key not in board.model.get("present", []):
            await board.make(key)
    await board.apply_names()
    if board.settings["launch"]:
        log.info("loading %s: %s", board.settings["launch"],
                 await board.load(board.settings["launch"]) or "ok")
        await board.refresh()
    board.watch_trial()  # (a trial whose minute ran out while PipeBoard was down: back at once)


async def frames(board: Board) -> None:
    """Live wires at 50 frames a second. A failing frame is logged and skipped, never fatal."""
    while True:
        await asyncio.sleep(0.02)
        try:
            await board.send_frame()
            await board.send_spectrum()
        except Exception:  # noqa: BLE001
            log.exception("live wires: a frame failed")


async def taps(board: Board) -> None:
    """The diagnostic cards' sound, sent every 8 ms on its own (owner: their picture lagged as
    the live wires do). The capture delivers a block every ~3 ms; waiting for the 20 ms frame
    tick added up to 20 ms. Nothing is sent while no card captures. A failing send is logged,
    never fatal."""
    while True:
        await asyncio.sleep(0.008)
        if board.taps:
            try:
                await board.send_taps()
            except Exception:  # noqa: BLE001
                log.exception("diagnostics: a frame failed")


async def toast(page: web.WebSocketResponse | None, text: str, level: str = "error") -> None:
    if page is None:  # an action from the tray: there is no page to tell
        log.warning("%s", text)
        return
    await page.send_str(json.dumps({"type": "toast", "text": text, "level": level}))


async def index(_request: web.Request) -> web.Response:
    """The page, each script and style asked for under its file's date: a copy Chrome cached
    before an update is never used with the new page (owner: Show latency was an unstyled box
    that did nothing: a new index.html with the old board.css and board.js)."""
    page = re.sub(r'(src|href)="(\w+\.(?:js|css))"',
                  lambda m: f'{m[1]}="{m[2]}?v={(WEB / m[2]).stat().st_mtime_ns}"',
                  (WEB / "index.html").read_text())
    return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def revalidate(_request: web.Request, response: web.StreamResponse) -> None:
    """Every file asked for again each time (a 304 when unchanged), never kept on a guess."""
    response.headers.setdefault("Cache-Control", "no-cache")


def app(board: Board, port: int) -> web.Application:
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def socket(request: web.Request) -> web.WebSocketResponse:
        # Only our own page: any website open in a browser could otherwise rewire the audio.
        if request.headers.get("Origin") not in allowed:
            raise web.HTTPForbidden(text="PipeBoard only talks to its own window")
        page = web.WebSocketResponse(heartbeat=20)
        await page.prepare(request)
        board.pages.add(page)
        await page.send_str(board.state())
        async for msg in page:
            if msg.type == WSMsgType.TEXT:
                try:
                    await board.act(page, json.loads(msg.data))
                except (ValueError, TypeError, KeyError) as e:
                    log.warning("bad message from the page: %s", e)
        await board.forget(page)
        return page

    application = web.Application()
    application.on_response_prepare.append(revalidate)
    application.router.add_get("/", index)
    # The window opens /pipeboard/: Chrome names an app window after its address, so this path
    # gives PipeBoard its own window id (KDE matches it to pipeboard's desktop entry and icon).
    application.router.add_get("/pipeboard/", index)
    application.router.add_get("/ws", socket)
    application.router.add_static("/pipeboard/", WEB)
    application.router.add_static("/", WEB)
    lifecycle(application, board)
    return application


async def close_pages(board: Board) -> None:
    """Quitting: open windows are told (they close themselves) and their connections closed.
    Otherwise the quit waited for every window to be closed by hand (tray Quit did nothing while
    the window was open, user)."""
    board.closing = True
    for page in list(board.pages):
        with contextlib.suppress(Exception):
            await page.send_str(json.dumps({"type": "bye"}))
            await page.close(code=WSCloseCode.GOING_AWAY, message=b"PipeBoard quit")


def lifecycle(application: web.Application, board: Board) -> None:
    async def start(_app: web.Application) -> None:
        await startup(board)
        _app["watch"] = asyncio.create_task(board.source.watch(board.changed))
        _app["frames"] = asyncio.create_task(frames(board))
        _app["taps"] = asyncio.create_task(taps(board))
        _app["listing"] = asyncio.create_task(board.plugins.installed())  # warms the picker
        _app["dropouts"] = asyncio.create_task(board.watch_errors())

    async def stop(_app: web.Application) -> None:
        board.closing = True
        tasks = [_app[k] for k in ("watch", "frames", "taps", "listing", "dropouts") if k in _app]
        for task in tasks:  # PipeBoard's own background work ends before the loop does
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await board.stop_children()

    application.on_shutdown.append(lambda _app: close_pages(board))
    application.on_startup.append(start)
    application.on_cleanup.append(stop)
