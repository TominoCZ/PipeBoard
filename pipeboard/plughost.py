"""One plugin's host: a separate process PipeBoard starts under `pw-jack` (PLAN §5.2).

Carla (its Python backend, stdlib only) runs a JACK engine with one client per plugin, and the
plugin itself in Carla's bridge process: a plugin that crashes takes neither this host nor
PipeBoard with it (Hum Remover even crashes when loaded in-process). PipeWire sees one node
with the plugin's ports; PIPEWIRE_PROPS (set by PipeBoard) tags it as PipeBoard's own.

    python -m pipeboard.plughost '{"id": …, "format": "lv2"|"vst3", "path": …, "label": …,
                                    "values": {"<index>": value} | null, "bypass": bool}'

Talk is JSON lines. Out: {"ready": {"params": [...], "editor": bool}} once loaded, or
{"error": text}; {"param": [index, value]} when the plugin's own window moves a control;
{"editor": bool} when that window opens or closes. In: {"values": {"<index>": value}},
{"editor": bool}, {"bypass": bool}. Ends cleanly (closing the bridge) when stdin closes, which
PipeBoard does to stop it, or on SIGTERM, which the kernel sends when PipeBoard ends.

Bypass is the plugin's own switch when it has one (a control named Bypass, or LSP's Enabled):
it lets the sound through untouched, often with a fade (measured for LSP). Otherwise, for an LV2
with as many outputs as inputs, Carla's dry/wet at fully dry (measured). Never dry/wet on a
VST3: on a bridged VST3 it crashes Carla 2.5.10 (Hum Remover, every time). Carla's "active"
switch is no bypass: it outputs silence.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import sys
from collections.abc import Callable

CARLA = "/usr/share/carla"
LIB = "/usr/lib/carla"


def _out() -> Callable[[dict], object]:
    """Carla prints to stdout: keep the real stdout for our lines, send Carla's to stderr."""
    fd = os.dup(1)
    os.dup2(2, 1)
    stream = os.fdopen(fd, "w", buffering=1)
    return lambda msg: stream.write(json.dumps(msg) + "\n")


def _params(host, cb) -> list[dict]:
    """The controls a person can set: name, unit, range, default, and what kind of control."""
    found = []
    for i in range(host.get_parameter_count(0)):
        data = host.get_parameter_data(0, i)
        if data["type"] != cb.PARAMETER_INPUT or not data["hints"] & cb.PARAMETER_IS_ENABLED:
            continue
        info, r = host.get_parameter_info(0, i), host.get_parameter_ranges(0, i)
        kind = "toggle" if data["hints"] & cb.PARAMETER_IS_BOOLEAN else \
            "int" if data["hints"] & cb.PARAMETER_IS_INTEGER else "number"
        found.append({"i": i, "name": info["name"], "unit": info["unit"],
                      "min": round(r["min"], 6), "max": round(r["max"], 6),
                      "def": round(r["def"], 6), "kind": kind,
                      "log": bool(data["hints"] & cb.PARAMETER_IS_LOGARITHMIC)})
    return found


def _engine(spec: dict, cb):
    host = cb.CarlaHostDLL(f"{LIB}/libcarla_standalone2.so", False)
    for option, value, text in (
            (cb.ENGINE_OPTION_PATH_BINARIES, 0, LIB),
            (cb.ENGINE_OPTION_PATH_RESOURCES, 0, f"{LIB}/resources"),
            (cb.ENGINE_OPTION_PROCESS_MODE, cb.ENGINE_PROCESS_MODE_MULTIPLE_CLIENTS, ""),
            (cb.ENGINE_OPTION_CLIENT_NAME_PREFIX, 0, f"pb-{spec['id']}"),
            (cb.ENGINE_OPTION_PREFER_PLUGIN_BRIDGES, 1, ""),
            # Where LV2 plugins are, as the picker lists them: Carla's own default left out
            # Ubuntu's /usr/lib/x86_64-linux-gnu/lv2, so a plugin picked there never loaded
            # ("Failed to find the requested plugin": the owner's Calf Vinyl since 2026-11-06).
            (cb.ENGINE_OPTION_PLUGIN_PATH, cb.PLUGIN_LV2, lv2_path())):
        host.set_engine_option(option, value, text)
    if not host.engine_init("JACK", f"pb-{spec['id']}"):
        raise RuntimeError(f"Carla could not start: {host.get_last_error()}")
    kind = cb.PLUGIN_LV2 if spec["format"] == "lv2" else cb.PLUGIN_VST3
    if not host.add_plugin(cb.BINARY_NATIVE, kind, spec.get("path") or "", "",
                           spec.get("label") or "", int(spec.get("uid") or 0), None, 0):
        raise RuntimeError(f"The plugin did not load: {host.get_last_error()}")
    return host


def _switch(params: list[dict]) -> dict | None:
    """The plugin's own bypass: {"i", "on" (value when bypassed), "off"}, or None."""
    for p in params:
        name = p["name"].strip().lower()
        if name == "bypass":
            return {"i": p["i"], "on": p["max"], "off": p["min"]}
        if name in ("enabled", "enable"):
            return {"i": p["i"], "on": p["min"], "off": p["max"]}
    return None


def _bypass(host, cb, spec: dict, params: list[dict]):
    """How this plugin is bypassed: set(on), or None when it cannot be."""
    switch = _switch(params)
    if switch:
        return lambda on: host.set_parameter_value(0, switch["i"], switch["on" if on else "off"])
    hints = host.get_plugin_info(0)["hints"]
    if spec["format"] == "lv2" and hints & cb.PLUGIN_CAN_DRYWET:
        return lambda on: host.set_drywet(0, 0.0 if on else 1.0)
    return None


def _apply(host, params: list[dict], values: dict | None) -> None:
    """Saved values, or the plugin's defaults: Carla reports a fresh VST3's values as 0, so a new
    plugin gets its defaults set, and the card never shows a value the plugin does not have."""
    for p in params:
        value = (values or {}).get(str(p["i"]), p["def"])
        p["value"] = min(p["max"], max(p["min"], float(value)))
        host.set_parameter_value(0, p["i"], p["value"])


def _command(host, line: str, bypass) -> None:
    msg = json.loads(line)
    if "bypass" in msg and bypass:
        bypass(bool(msg["bypass"]))
    for i, value in (msg.get("values") or {}).items():
        host.set_parameter_value(0, int(i), float(value))
    if "editor" in msg:
        host.show_custom_ui(0, bool(msg["editor"]))


def main() -> int:
    say = _out()
    stop = []
    sys.path.insert(0, CARLA)
    try:
        import carla_backend as cb  # noqa: PLC0415 - Carla's own module, from its own folder

        spec = json.loads(sys.argv[1])
        host = _engine(spec, cb)
    except Exception as e:  # noqa: BLE001 - tell PipeBoard why, in words
        say({"error": str(e)})
        return 1

    def heard(_h, action, plugin, v1, _v2, _v3, value, _text):
        if action == cb.ENGINE_CALLBACK_PARAMETER_VALUE_CHANGED and plugin == 0 and v1 >= 0:
            say({"param": [v1, value]})
        elif action == cb.ENGINE_CALLBACK_UI_STATE_CHANGED:
            say({"editor": v1 == 1})

    # After Carla starts: its engine puts in a SIGTERM handler of its own, which left the bridge
    # running (an orphan for ~30 s) instead of closing it.
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    params, hints = _params(host, cb), host.get_plugin_info(0)["hints"]
    _apply(host, params, spec.get("values"))
    bypass = _bypass(host, cb, spec, params)
    if bypass and spec.get("bypass"):
        bypass(True)
    host.set_engine_callback(heard)
    say({"ready": {"params": params, "editor": bool(hints & cb.PLUGIN_HAS_CUSTOM_UI),
                   "bypassable": bool(bypass), "switch": _switch(params)}})
    _loop(host, stop, bypass)
    host.engine_close()
    return 0


def _loop(host, stop: list, bypass) -> None:
    """Carla's idle work (the editor window, bridge messages) on this thread, 30 times a second;
    commands as they come. Ends on SIGTERM or when PipeBoard's end of stdin closes."""
    while not stop:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 1 / 30)
        except InterruptedError:
            continue
        if ready:
            line = sys.stdin.readline()
            if not line:
                return
            # a bad line from PipeBoard is ignored, never fatal
            with contextlib.suppress(ValueError, KeyError, TypeError, IndexError):
                _command(host, line, bypass)
        host.engine_idle()


LV2_PATH = ("~/.lv2", "/usr/local/lib/lv2", "/usr/lib/lv2", "/usr/lib/x86_64-linux-gnu/lv2")
VST3_PATH = ("~/.vst3", "/usr/local/lib/vst3", "/usr/lib/vst3")


def lv2_path() -> str:
    """Where LV2 plugins are looked for: listing them and loading one alike."""
    return os.environ.get("LV2_PATH") or ":".join(os.path.expanduser(p) for p in LV2_PATH)


def _lv2() -> list[dict]:
    """LV2 effects (audio in and out), from Carla's own LV2 cache: ~1 s for 335 plugins."""
    from carla_backend import PLUGIN_LV2  # noqa: PLC0415
    from carla_utils import CarlaUtils  # noqa: PLC0415

    utils = CarlaUtils(f"{LIB}/libcarla_utils.so")
    path = lv2_path()
    found = []
    for i in range(utils.get_cached_plugin_count(PLUGIN_LV2, path)):
        p = utils.get_cached_plugin_info(PLUGIN_LV2, i)
        if p["valid"] and p["audioIns"] and p["audioOuts"]:
            found.append({"format": "lv2", "name": p["name"], "maker": p["maker"],
                          "label": p["label"].split("/", 1)[-1], "ins": p["audioIns"],
                          "outs": p["audioOuts"], "params": p["parameterIns"]})
    return found


def _discover(bundle: str) -> list[dict]:
    """The plugins in one VST3 bundle, by Carla's discovery tool (LSP's bundle of 194: ~50 s)."""
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run([f"{LIB}/carla-discovery-native", "vst3", bundle], timeout=180,
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found, cur = [], {}
    for line in out.splitlines():
        _, _, rest = line.partition("carla-discovery::")
        key, _, value = rest.partition("::")
        if key == "init":
            cur = {}
        elif key == "end" and cur.get("audio.ins", "0") != "0" != cur.get("audio.outs", "0"):
            found.append({"format": "vst3", "name": cur.get("name", ""),
                          "maker": cur.get("maker", ""), "path": bundle,
                          "label": cur.get("label", ""), "uid": int(cur.get("uniqueId", 0)),
                          "ins": int(cur["audio.ins"]), "outs": int(cur["audio.outs"]),
                          "params": int(cur.get("parameters.ins", 0))})
        elif key:
            cur[key] = value
    return found


def _vst3(cache: dict) -> list[dict]:
    """Every VST3 bundle's plugins: discovered once, then read from the cache until it changes."""
    found = []
    for folder in VST3_PATH:
        folder = os.path.expanduser(folder)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            bundle = os.path.join(folder, name)
            if not name.endswith(".vst3") or not os.path.isdir(f"{bundle}/Contents/x86_64-linux"):
                continue  # a Windows plugin, or not a bundle
            stamp = os.stat(bundle).st_mtime
            if cache.get(bundle, {}).get("stamp") != stamp:
                cache[bundle] = {"stamp": stamp, "plugins": _discover(bundle)}
            found += cache[bundle]["plugins"]
    return found


def _same(p: dict) -> str:
    """A name to spot one plugin in two formats: LSP's LV2 "LSP Compressor Stereo" (maker "LSP
    LV2") is its VST3 "Compressor Stereo" (maker "LSP VST3")."""
    name, brand = p["name"].lower(), (p["maker"].split() or [""])[0].lower()
    return name.removeprefix(brand + " ") if brand else name


def listing(cache_file: str) -> list[dict]:
    """Installed Linux effects: LV2, then VST3s that are not the same plugin as an LV2 one."""
    sys.path.insert(0, CARLA)
    try:
        with open(cache_file) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    lv2 = _lv2()
    names = {_same(p) for p in lv2}
    vst3 = [p for p in _vst3(cache) if _same(p) not in names]
    with open(cache_file + ".tmp", "w") as f:
        json.dump(cache, f)
    os.replace(cache_file + ".tmp", cache_file)
    return sorted(lv2 + vst3, key=lambda p: (p["name"].lower(), p["format"]))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--list"]:
        say = _out()
        say({"plugins": listing(sys.argv[2])})
        sys.exit(0)
    sys.exit(main())
