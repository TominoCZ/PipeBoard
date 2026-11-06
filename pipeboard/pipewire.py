"""The only module that runs PipeWire's tools: snapshot, watch, link, unlink (PLAN §3).

Also a stand-in, Fixture, that keeps a saved graph in memory and applies links to it, so the
whole app can be clicked through without touching real audio (`pipeboard --fixture`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from pipeboard import devices

log = logging.getLogger(__name__)
NODE, PORT, LINK = (f"PipeWire:Interface:{t}" for t in ("Node", "Port", "Link"))
# What the board is made of: a change to anything else (a client connecting, as PipeBoard's own
# pw-dump, pw-top and pw-link do every time) is no reason to read the graph again. Reacting to
# those, each read made the next one: ~13 full reads a second, for nothing (owner: the load).
BOARD_TYPES = {"Node", "Port", "Link", "Device", "Metadata"}
_TYPED = re.compile(r'"id": (\d+),\s*"type": "PipeWire:Interface:(\w+)"')
_GONE = re.compile(r'"id": (\d+),\s*"info": null')


def matters(text: str, kinds: dict[str, str]) -> bool:
    """Whether a piece of pw-dump --monitor output changes the board. `kinds` remembers each
    object's type (a removal names only its id); an id never seen counts, to be safe."""
    hit = False
    for oid, kind in _TYPED.findall(text):
        kinds[oid] = kind
        hit = hit or kind in BOARD_TYPES
    for oid in _GONE.findall(text):
        hit = hit or kinds.pop(oid, "Node") in BOARD_TYPES
    return hit


async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode().strip()


class Live:
    """The real graph."""

    mode = "live"

    async def snapshot(self) -> list[dict]:
        code, out, err = await _run("pw-dump")
        if code:
            raise RuntimeError(f"pw-dump failed: {err}")
        return json.loads(out)

    async def watch(self, changed) -> None:
        """Call changed() whenever the graph changes; pw-dump --monitor is only the trigger."""
        while True:
            proc = await asyncio.create_subprocess_exec(
                "pw-dump", "--monitor", "--no-colors",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)  # Ctrl+C: not killed under PipeBoard while it quits
            kinds: dict[str, str] = {}
            tail = ""
            try:
                while chunk := await proc.stdout.read(65536):
                    text = tail + chunk.decode(errors="replace")
                    # (whole objects, each starting "  {" on its own line: an id and its type are
                    # never split between two pieces)
                    head, mark, rest = text.rpartition("\n  {")
                    text, tail = (head, mark + rest) if mark else (text, "")
                    if matters(text, kinds):
                        changed()
                await proc.wait()
            finally:  # the app is stopping: take the watcher with it, quietly
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                proc._transport.close()
            log.warning("pw-dump --monitor ended (%s); restarting in 2 s", proc.returncode)
            await asyncio.sleep(2)

    async def link(self, out_port: int, in_port: int) -> str | None:
        """Make a link; return PipeWire's complaint, or None."""
        code, _, err = await _run("pw-link", str(out_port), str(in_port))
        return (err or "pw-link failed") if code else None

    async def unlink(self, link_id: int) -> str | None:
        code, _, err = await _run("pw-link", "-d", str(link_id))
        return (err or "pw-link -d failed") if code else None


    async def create(self, dev: dict) -> str | None:
        """Make one of PipeBoard's virtual devices: a lingering node inside PipeWire."""
        code, _, err = await _run("pw-cli", "create-node", "adapter", devices.props(dev))
        # pw-cli also reports objects that vanished while it ran ("Stale file handle", "no global",
        # "unknown resource": a busy graph at login): not this node's failure. Treating them as
        # one stopped the profile load at login, though every device had been made.
        churn = ("Stale file handle", "no global", "unknown resource")
        real = [line for line in err.splitlines()
                if "error" in line.lower() and not any(n in line for n in churn)]
        return (real[0] if real else err or "pw-cli create-node failed") if code or real else None

    async def stop_service(self, unit: str) -> str | None:
        return await stop_service(unit)

    async def destroy(self, node_ids: list[int]) -> str | None:
        """Remove nodes. Only ever called for PipeBoard's own devices (server.py checks)."""
        for nid in node_ids:
            code, _, err = await _run("pw-cli", "destroy", str(nid))
            # pw-cli can report failure on stderr alone; but it also prints "no global N" when an
            # object it was looking at went away meanwhile (another one, or this one already
            # gone): taken as a failure, the device stayed on the board while its node was gone,
            # a ghost card (owner saw it; 2 in 30 in a test).
            bad = [line for line in err.splitlines() if "error" in line.lower()
                   and "no global" not in line.lower()]
            if code or bad:
                return (bad[0] if bad else err) or "pw-cli destroy failed"
        return None


async def stop_service(unit: str) -> str | None:
    """Stop and disable a systemd user service (its processes end with it); a complaint or None."""
    for verb in ("stop", "disable"):
        code, out, err = await _run("systemctl", "--user", verb, unit)
        if code:
            return f"systemctl {verb} {unit}: {err or out}"
    return None


class Fixture:
    """A saved graph in memory: links change it, nothing reaches PipeWire."""

    mode = "fixture"

    def __init__(self, dump: list[dict]):
        self.dump = dump
        self.units: dict[int, str] = {}  # process id → service, for take-over checks
        self.changed = lambda: None
        self.next_id = max((o["id"] for o in dump), default=0) + 1000

    async def snapshot(self) -> list[dict]:
        return self.dump

    async def watch(self, changed) -> None:
        self.changed = changed
        await asyncio.Event().wait()

    async def link(self, out_port: int, in_port: int) -> str | None:
        self.next_id += 1
        self.dump.append({"id": self.next_id, "type": LINK, "info": {
            "output-port-id": out_port, "input-port-id": in_port, "state": "active", "props": {}}})
        self.changed()
        return None

    async def unlink(self, link_id: int) -> str | None:
        self.dump[:] = [o for o in self.dump if o["id"] != link_id]
        self.changed()
        return None


    async def stop_service(self, unit: str) -> str | None:
        """What stopping a service does to the graph: its processes' nodes are gone."""
        clients = {o["id"] for o in self.dump if o["type"].endswith("Client")
                   and self.units.get(o["info"]["props"].get("application.process.id")) == unit}
        doomed = [o["id"] for o in self.dump if o["type"] == NODE
                  and o["info"]["props"].get("client.id") in clients]
        return await self.destroy(doomed)

    async def create(self, dev: dict) -> str | None:
        """The node and ports PipeWire would make for this device."""
        if dev["type"] == "plugin":
            return self._plugin(dev)
        if dev["type"] in ("eq", "limiter", "compressor", "gate", "denoise", "deesser"):
            return self._eq(dev)
        nid = self.next_id = self.next_id + 10
        chans = ["FL", "FR"] if dev["channels"] == 2 else ["MONO"]
        props = {"node.name": dev["node"], "node.description": dev["name"],
                 "media.class": devices.CLASS[dev["type"]], "object.serial": nid,
                 "node.virtual": True, "pipeboard.owned": True, "pipeboard.id": dev["id"],
                 "pipeboard.board": devices.BOARD,
                 "pipeboard.kind": devices.KIND.get(dev["type"], "virtual"),
                 "pipeboard.type": dev["type"]}
        self.dump.append({"id": nid, "type": NODE, "info": {"props": props}})
        names = ("input", "capture") if dev["type"] == "mic" else ("playback", "monitor")
        for i, ch in enumerate(chans):
            for j, (side, name) in enumerate(zip(("input", "output"), names, strict=True)):
                self.dump.append({"id": nid + 1 + i * 2 + j, "type": PORT, "info": {
                    "direction": side, "props": {
                        "node.id": nid, "port.name": f"{name}_{ch}", "audio.channel": ch,
                        "port.id": i, "format.dsp": "32 bit float mono audio",
                        "port.monitor": side == "output" or None}}})
        self.changed()
        return None

    def _plugin(self, dev: dict) -> None:
        """What a plugin host makes: Carla's empty engine node and the plugin's JACK node."""
        tags = {"pipeboard.owned": True, "pipeboard.id": dev["id"], "pipeboard.kind": "plugin",
                "client.api": "jack", "media.type": "Audio"}
        for name in (f"pb-{dev['id']}", f"pb-{dev['id']}.0/{dev['name']}"):
            nid = self.next_id = self.next_id + 10
            self.dump.append({"id": nid, "type": NODE, "info": {"props": {
                **tags, "node.name": name, "node.description": name, "object.serial": nid}}})
        ports = [("input", f"in_{i + 1}") for i in range(dev.get("ins", 2))]
        ports += [("output", f"out_{i + 1}") for i in range(dev.get("outs", 2))]
        for k, (side, name) in enumerate(ports):
            props = {"node.id": nid, "port.name": name, "port.id": k,
                     "format.dsp": "32 bit float mono audio"}
            self.dump.append({"id": nid + 1 + k, "type": PORT,
                              "info": {"direction": side, "props": props}})
        self.changed()

    def _eq(self, dev: dict) -> None:
        """What an EQ's filter-chain makes: an input stream and an output stream, FL and FR."""
        tags = {"pipeboard.owned": True, "pipeboard.id": dev["id"], "pipeboard.kind": "effect"}
        for name, cls, side in ((f"{dev['node']}_in", "Stream/Input/Audio/Internal", "input"),
                                (dev["node"], "Stream/Output/Audio/Internal", "output")):
            nid = self.next_id = self.next_id + 10
            self.dump.append({"id": nid, "type": NODE, "info": {"props": {
                **tags, "node.name": name, "media.class": cls, "object.serial": nid}}})
            for k, ch in enumerate(("FL", "FR")):
                props = {"node.id": nid, "port.name": f"{side}_{ch}", "audio.channel": ch,
                         "port.id": k, "format.dsp": "32 bit float mono audio"}
                self.dump.append({"id": nid + 1 + k, "type": PORT,
                                  "info": {"direction": side, "props": props}})
        self.changed()

    async def destroy(self, node_ids: list[int]) -> str | None:
        gone = set(node_ids)
        ports = {o["id"] for o in self.dump if o["type"] == PORT
                 and o["info"]["props"]["node.id"] in gone}
        ends = lambda o: {o["info"]["output-port-id"], o["info"]["input-port-id"]}  # noqa: E731
        self.dump[:] = [o for o in self.dump if o["id"] not in gone | ports
                        and not (o["type"] == LINK and ends(o) & ports)]
        self.changed()
        return None
