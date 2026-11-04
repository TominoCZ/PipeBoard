"""The page and its WebSocket: the board model out, the user's actions in (PLAN §3)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from pipeboard import (
    autostart,
    devices,
    dynamics,
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
        self.scope_tries = 0
        self.scope_seen = (0, 0.0)
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
                    "live-setting": self.op_live_setting}

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
        found.sort(key=lambda p: problems.ORDER[p["severity"]])
        return json.dumps({"type": "state", "model": self.model, "workspace": self.ws,
                           "profile": prof, "problems": found, "mode": self.source.mode,
                           "autostart": self.autostart, "editors": sorted(self.editors),
                           "takeover": {u: [c["title"] for c in cards]
                                        for u, cards in self.offer.items()},
                           "live": {"on": self.settings["live"],
                                    "sources": self.scope_sources_sent()}})

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
            for card in self.model["cards"]:  # PipeBoard's own: the name it was given (a
                if dev := self.ws.get("devices", {}).get(card["key"]):  # plugin's node is pb-<id>)
                    card["title"] = dev["name"]
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
        await self.sync_spectrum()
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
        """The graph moved: take one snapshot once it settles, however many events arrive."""
        if not self.pending:
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

    async def make(self, key: str) -> str | None:
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
        self.ws = {**saved, "cards": saved.get("cards", {}), "wires": saved.get("wires", [])}
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
            if key == self.scope_key and not dead:
                return
            if not dead and await self.follow_scope(key, sources):
                return
            if dead and key == self.scope_key:
                if self.scope_tries >= 5 or time.monotonic() - self.scope.dead_since < 1:
                    return  # a second's rest between tries; after five, give up quietly
                self.scope_tries += 1
            else:
                self.scope_tries = 0
            await self.stop_scope()
            self.scope_key = key
            kind = scopes.FakeScope if self.source.mode == "fixture" else scopes.Scope
            fresh = kind(sources) if sources else None
            # The page learns the new sources before the first frame that uses them.
            sent = [src and [src[0], src[1]] for src in fresh.sources] if fresh else []
            await self.broadcast(json.dumps({"type": "scope", "sources": sent, "fresh": True}))
            if fresh:
                self.scope, self.scope_seen = fresh, (0, time.monotonic())
                self.scope_started = time.monotonic()
                await fresh.start()
        except Exception:  # noqa: BLE001
            log.exception("live wires: could not set up the capture")

    async def follow_scope(self, key: tuple, sources: list) -> bool:
        """The running capture takes the new sources in: its count runs on, so the page keeps
        every wire's sound. False: it cannot (see Scope.update), a fresh one is needed."""
        if not (self.scope and sources and await self.scope.update(sources)):
            return False
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
        self.visible.discard(page)
        self.watching.pop(page, None)
        await self.sync_scope()
        await self.sync_spectrum()

    async def op_live(self, page, op) -> str | None:
        """A page says whether it is visible: nothing is captured for a hidden window."""
        (self.visible.add if op.get("visible") else self.visible.discard)(page)
        await self.sync_scope()
        await self.sync_spectrum()
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


async def startup(board: Board) -> None:
    """Read the system, then load the profile chosen to load at launch, if there is one."""
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


async def frames(board: Board) -> None:
    """Live wires at 50 frames a second. A failing frame is logged and skipped, never fatal."""
    while True:
        await asyncio.sleep(0.02)
        try:
            await board.send_frame()
            await board.send_spectrum()
        except Exception:  # noqa: BLE001
            log.exception("live wires: a frame failed")


async def toast(page: web.WebSocketResponse | None, text: str, level: str = "error") -> None:
    if page is None:  # an action from the tray: there is no page to tell
        log.warning("%s", text)
        return
    await page.send_str(json.dumps({"type": "toast", "text": text, "level": level}))


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

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    application = web.Application()
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
        _app["listing"] = asyncio.create_task(board.plugins.installed())  # warms the picker

    async def stop(_app: web.Application) -> None:
        board.closing = True
        tasks = [_app[k] for k in ("watch", "frames", "listing") if k in _app]
        for task in tasks:  # PipeBoard's own background work ends before the loop does
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await board.stop_scope()  # the capture never outlives the app
        await board.route_around()  # before the EQs and plugins stop: the sound keeps going
        await board.gains.stop()
        await board.plugins.stop_all()
        await board.eqs.stop_all()
        if board.spectrum:
            await board.spectrum.stop()

    application.on_shutdown.append(lambda _app: close_pages(board))
    application.on_startup.append(start)
    application.on_cleanup.append(stop)
