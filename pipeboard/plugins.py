"""Plugin nodes: which plugins there are, and one host process per plugin on the board (§5.2).

Each runs `python -m pipeboard.plughost` under `pw-jack`: Carla never loads into PipeBoard, and a
plugin that crashes ends only its own card (it turns "missing"; Create this device starts it
again). The kernel ends a host when PipeBoard ends (PR_SET_PDEATHSIG, SIGTERM: the host closes
its bridge cleanly). Unlike devices and gains, plugins do not outlive PipeBoard: the board
starts them again at launch. Nothing here raises: a failure reaches `heard` as {"error": text}.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

from pipeboard import devices, plughost
from pipeboard.scope import _die_with_parent

log = logging.getLogger(__name__)
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pipeboard"


def ref(p: dict) -> str:
    """One installed plugin's identity, as the page names it when adding one."""
    return f"{p['format']}:{p.get('path') or ''}:{p['label']}"


def new(entry: dict) -> dict:
    """A plugin on the board, from an installed one (never from what the page sends: the page
    names a plugin by ref, and only listed ones load). Its controls arrive when it first starts."""
    dev = {"id": "d" + secrets.token_hex(5), "type": "plugin", "name": entry["name"][:40],
           "plugin": entry["name"],
           "channels": min(2, entry["ins"]), "ins": entry["ins"], "outs": entry["outs"]}
    dev["node"] = f"pb-{dev['id']}"
    dev.update({k: entry[k] for k in ("format", "path", "label", "uid") if k in entry})
    return dev


def listed(out: bytes, err: bytes, code: int | None) -> list[dict]:
    """The installed plugins from `plughost --list`'s output; [] with one line logged (not a
    traceback) when it said nothing: Carla failed to load, and its own words say why."""
    lines = out.decode(errors="replace").splitlines()
    if not lines:
        why = (err.decode(errors="replace").strip().splitlines() or [f"exit {code}"])[-1]
        log.warning("plugins: could not list them: %s", why)
        return []
    return json.loads(lines[-1])["plugins"]


class Plugins:
    """Starts, talks to and stops plugin hosts. `heard(key, message)` gets what each host says."""

    def __init__(self, heard: Callable[[str, dict], None]):
        self.heard = heard
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.stopping: set[str] = set()
        self.found: list[dict] | None = None  # installed plugins, once listed
        self.listing: asyncio.Task | None = None

    async def installed(self, rescan: bool = False) -> list[dict]:
        """The installed Linux effects; the first call lists them (VST3 bundles new to the cache
        are discovered, LSP's takes ~50 s once), later ones reuse that. `rescan` lists them again:
        LV2s ~1 s, unchanged VST3 bundles from the cache, so only what is new is slow."""
        if rescan and not self.listing:
            self.found = None
        if self.found is None:
            if not self.listing:
                self.listing = asyncio.create_task(self._list())
            await asyncio.shield(self.listing)
        return self.found or []

    async def _list(self) -> None:
        if not (Path(plughost.CARLA) / "carla_backend.py").is_file():
            # (a friend's launch: a traceback at every start, for a choice the installer offered)
            log.info("plugins: Carla is not installed, so there are none to add (optional)")
            self.found, self.listing = [], None
            return
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pipeboard.plughost", "--list", str(CACHE / "vst3.json"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=_die_with_parent)
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:  # quitting mid-listing: no child or pipe left behind
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                proc._transport.close()
                raise
            self.found = listed(out, err, proc.returncode)
        except Exception:  # noqa: BLE001 - no list is an empty picker, not a crash
            log.exception("plugins: could not list them")
            self.found = []
        finally:
            self.listing = None

    async def start(self, key: str, dev: dict) -> str | None:
        if key in self.procs:
            return None  # running already (a profile load right after launch asks again)
        spec = {k: dev.get(k) for k in ("id", "format", "path", "label", "uid", "bypass")}
        spec["values"] = {str(p["i"]): p["value"] for p in dev.get("params", []) if "value" in p}
        env = {**os.environ, "PIPEWIRE_PROPS": "{ pipeboard.owned=true "
               f"pipeboard.id={dev['id']} pipeboard.kind=plugin "
               f"pipeboard.board={devices.BOARD or 'none'} }}"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "pw-jack", sys.executable, "-m", "pipeboard.plughost", json.dumps(spec),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, preexec_fn=_die_with_parent,
                start_new_session=True)  # Ctrl+C reaches PipeBoard only (see eq.Eqs.start)
        except OSError as e:
            return f"could not start its host: {e}"
        self.procs[key] = proc
        asyncio.create_task(self._read(key, proc))
        return None

    async def _read(self, key: str, proc: asyncio.subprocess.Process) -> None:
        try:
            async for line in proc.stdout:
                with contextlib.suppress(ValueError):
                    self.heard(key, json.loads(line))
        except Exception:  # noqa: BLE001
            log.exception("plugins: lost the host of %s", key)
        await proc.wait()
        if self.procs.get(key) is proc:
            del self.procs[key]
        if key not in self.stopping:
            self.heard(key, {"ended": proc.returncode})
        self.stopping.discard(key)

    def send(self, key: str, msg: dict) -> None:
        proc = self.procs.get(key)
        if proc and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def stop(self, key: str) -> None:
        """Close its stdin: the host closes its bridge and ends (the cleanest way, measured).
        SIGTERM after 3 s, SIGKILL after 3 more."""
        proc = self.procs.pop(key, None)
        if not proc:
            return
        self.stopping.add(key)
        with contextlib.suppress(OSError, AttributeError):
            proc.stdin.close()
        for end in (proc.terminate, proc.kill):
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 3)
                return
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    end()

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(k) for k in list(self.procs)))


def _knobs(rows: list[tuple]) -> list[dict]:
    return [{"i": i, "name": n, "unit": u, "min": lo, "max": hi, "def": d, "kind": k,
             "log": False} for i, (n, u, lo, hi, d, k) in enumerate(rows)]


# What the fixture's plugins say when they start (the real ones' first controls).
FAKE_PARAMS = {
    "Hum Remover": _knobs([
        ("Strength", "", 0, 1, 0.6, "number"), ("Hum canceller", "", 0, 1, 0.5, "number"),
        ("Cut", "", 0, 1, 0, "number"), ("Gate", "", 0, 1, 1, "number"),
        ("Bypass", "", 0, 1, 0, "toggle")]),
    "": _knobs([
        ("Attack threshold", "G", 0, 1, 0.25, "number"), ("Ratio", "", 1, 100, 4, "number"),
        ("Attack time", "ms", 0, 2000, 20, "number"),
        ("Release time", "ms", 0, 5000, 100, "number"),
        ("Makeup gain", "G", 0, 251, 1, "number"), ("Knee", "G", 0.06, 1, 0.5, "number"),
        ("Dry gain", "G", 0, 10, 0, "number"), ("Wet gain", "G", 0, 10, 1, "number")]),
}
FAKE_INSTALLED = [
    {"format": "lv2", "name": "LSP Compressor Stereo", "maker": "LSP LV2", "ins": 2, "outs": 2,
     "label": "http://lsp-plug.in/plugins/lv2/compressor_stereo", "params": 48},
    {"format": "lv2", "name": "LSP Parametric Equalizer x8 Stereo", "maker": "LSP LV2", "ins": 2,
     "outs": 2, "label": "http://lsp-plug.in/plugins/lv2/para_equalizer_x8_stereo", "params": 140},
    {"format": "lv2", "name": "a-Reverb", "maker": "Ardour Community", "ins": 1, "outs": 2,
     "label": "urn:ardour:a-reverb", "params": 3},
    {"format": "vst3", "name": "Hum Remover", "maker": "Hum Remover", "ins": 2, "outs": 2,
     "path": "/usr/lib/vst3/HumRemover.vst3", "label": "Hum Remover", "uid": 460611248,
     "params": 5},
]


class FakePlugins(Plugins):
    """The fixture's plugins: the nodes a host would make, in the fixture's graph; no process."""

    def __init__(self, heard: Callable[[str, dict], None], source):
        super().__init__(heard)
        self.source, self.found, self.running = source, FAKE_INSTALLED, set()

    async def installed(self, rescan: bool = False) -> list[dict]:
        return self.found  # (Rescan never lists the real ones)

    async def start(self, key: str, dev: dict) -> str | None:
        if key in self.running:
            return None
        self.running.add(key)
        await self.source.create(dev)
        if not dev.get("params"):
            params = [dict(p) for p in FAKE_PARAMS.get(dev["name"], FAKE_PARAMS[""])]
            for p in params:
                p["value"] = p["def"]
            switch = next(({"i": p["i"], "on": p["max"], "off": p["min"]} for p in params
                           if p["name"] == "Bypass"), None)
            self.heard(key, {"ready": {"params": params, "editor": True, "bypassable": True,
                                       "switch": switch}})
        return None

    def send(self, key: str, msg: dict) -> None:
        if "editor" in msg:  # the fixture's plugin window opens and closes at once
            self.heard(key, {"editor": bool(msg["editor"])})

    async def stop(self, key: str) -> None:
        self.running.discard(key)
        await self.source.destroy([o["id"] for o in self.source.dump if o["type"].endswith("Node")
                                   and o["info"]["props"].get("pipeboard.id") == key[3:]])
