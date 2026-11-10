"""What the board costs to draw: headless Chrome on the real GPU, each scene animating with made-up
sound for a few seconds, measured through the DevTools protocol.

    ./run.sh perf                  every scene, on the GPU
    ./run.sh perf wires scopes     only these
    ./run.sh perf --cpu            software rendering (SwiftShader)
    ./run.sh perf --json out.json  also write the numbers
    ./run.sh perf --uncapped       frames as fast as they come (no vsync): the fps it could reach
                                   (owner's screen: 240 Hz, a frame every 4.2 ms)

Per scene: CPU time of every Chrome process (the page, the GPU process, raster) per second of
wall time, the page's main thread (all tasks, and script / style and layout within it), and the
screen frames' intervals. The pages are the shots' (scripts/shots.py), so the same boards.
"""

import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shots  # noqa: E402

# Made-up sound that keeps coming: the EQ's in and out (its live spectrum), a dynamics panel's peaks.
EQ_FEED = """(() => { let t = 0; setInterval(() => {
  const f = new Float32Array(1920);
  for (let i = 0; i < 960; i++, t++) { const s = t / 48000, x = 0.3 * Math.sin(2 * Math.PI * 110 * s) + 0.1 * Math.sin(2 * Math.PI * 1300 * s) + 0.02 * (Math.random() - 0.5);
    f[2 * i] = x; f[2 * i + 1] = 0.8 * x; }
  let b = ''; const u = new Uint8Array(f.buffer); for (let i = 0; i < u.length; i++) b += String.fromCharCode(u[i]);
  EQ.pcm('pb:deq01', btoa(b)); }, 20); })()"""
DYN_FEED = """(() => { let t = 0; setInterval(() => { const list = [];
  for (let i = 0; i < 2; i++, t++) { const v = 0.2 + 0.6 * Math.abs(Math.sin(t / 37)) * Math.random(); list.push([v, Math.min(v, 0.5)]); }
  DYN.peaks('pb:dlim01', list); }, 20); })()"""

# name: (scenario, size, setup, js) as the shots'
SCENES = {
    "idle": ("machine", shots.BIG, shots.studio, "PB.debug.tidy()"),
    "wires": ("machine", shots.BIG, shots.studio, "PB.debug.tidy() || PB.debug.liveLoop(99)"),
    "wires-zoomed": ("machine", shots.BIG, shots.studio, "PB.debug.tidy() || PB.debug.zoom(2.2) || PB.debug.liveLoop(99)"),
    # Zooming in and out all the time over the live board (owner: zooming felt choppy).
    "zoom-live": ("machine", shots.BIG, shots.studio, "PB.debug.tidy() || PB.debug.liveLoop(99) || "
                  "(function f(t) { PB.debug.zoom(1.1 + 0.6 * Math.sin(t / 500)); requestAnimationFrame(f); })(0)"),
    "scopes": ("machine", shots.BIG, shots.diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag(1, true)"),
    "scope-panel": ("machine", shots.BIG, shots.diagnosed, "PB.debug.tidy() || PB.debug.fakeDiag(1, true) || PB.debug.openDiag('pb:dster01')"),
    "eq-panel": ("machine", shots.BIG, shots.equalized, f"PB.debug.eqOpen('pb:deq01', 2) || {EQ_FEED}"),
    "dyn-panel": ("machine", shots.BIG, shots.equalized, f"PB.debug.dynOpen('pb:dlim01') || {DYN_FEED}"),
}
GPU = ["--use-gl=angle", "--use-angle=gl", "--enable-gpu", "--ignore-gpu-blocklist"]
UNCAPPED = ["--disable-gpu-vsync", "--disable-frame-rate-limit"]
CPU = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
PROBE = """(() => { const g = document.createElement('canvas').getContext('webgl2'), x = g && g.getExtension('WEBGL_debug_renderer_info');
  return g ? (x ? g.getParameter(x.UNMASKED_RENDERER_WEBGL) : 'webgl2') : 'none'; })()"""
FRAMES = "window.__ft = []; (function f(t) { __ft.push(t); requestAnimationFrame(f); })(performance.now())"


def cpu_ticks(root: int) -> int:
    """utime + stime of a process and all its descendants, in clock ticks."""
    kids, stat = {}, {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            f = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        kids.setdefault(int(f[1]), []).append(int(d))
        stat[int(d)] = int(f[11]) + int(f[12])
    total, todo = 0, [root]
    while todo:
        p = todo.pop()
        total += stat.get(p, 0)
        todo += kids.get(p, [])
    return total


async def measure(name: str, page: Path, gpu: bool, seconds: float, port: int, uncapped: bool = False) -> dict:
    w, h = SCENES[name][1]
    prof = tempfile.mkdtemp()
    chrome = subprocess.Popen([shots._chrome(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                               f"--window-size={w},{h}", "--force-device-scale-factor=1", "--no-first-run", "--hide-scrollbars",
                               *(GPU if gpu else CPU), *(UNCAPPED if uncapped else []), "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(100):
                try:
                    tabs = await (await s.get(f"http://127.0.0.1:{port}/json")).json()
                    break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
            tab = next(t for t in tabs if t["type"] == "page")
            async with s.ws_connect(tab["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                n = 0

                async def cmd(method, **params):
                    nonlocal n
                    n += 1
                    await ws.send_json({"id": n, "method": method, "params": params})
                    while True:
                        m = json.loads((await ws.receive()).data)
                        if m.get("id") == n:
                            return m.get("result", {})

                async def js(expr):
                    r = await cmd("Runtime.evaluate", expression=expr, returnByValue=True)
                    return r.get("result", {}).get("value")

                async def metrics():
                    got = (await cmd("Performance.getMetrics"))["metrics"]
                    return {m["name"]: m["value"] for m in got}

                await cmd("Performance.enable")
                await cmd("Page.navigate", url=page.as_uri())
                await asyncio.sleep(3)  # loaded, the scene's own JS run, settled
                renderer = await js(PROBE)
                await js(FRAMES)
                m0, c0 = await metrics(), cpu_ticks(chrome.pid)
                await asyncio.sleep(seconds)
                m1, c1 = await metrics(), cpu_ticks(chrome.pid)
                ft = await js("__ft")
                errors = await js("PB.debug.errors()") or []
    finally:
        chrome.kill()
        chrome.wait()
    gaps = [b - a for a, b in zip(ft, ft[1:])] or [0]
    per_s = lambda k: (m1.get(k, 0) - m0.get(k, 0)) * 1000 / seconds  # noqa: E731  (s per s → ms per s)
    return {"scene": name, "renderer": renderer,
            "cpu": (c1 - c0) * 1000 / os.sysconf("SC_CLK_TCK") / seconds,
            "main": per_s("TaskDuration"), "script": per_s("ScriptDuration"),
            "style_layout": per_s("RecalcStyleDuration") + per_s("LayoutDuration"),
            "fps": len(gaps) / (sum(gaps) / 1000) if sum(gaps) else 0,
            "p50": statistics.median(gaps), "p95": sorted(gaps)[int(len(gaps) * 0.95)], "worst": max(gaps),
            "errors": errors}


def main() -> int:
    args = sys.argv[1:]
    gpu, out, uncapped = "--cpu" not in args, None, "--uncapped" in args
    if "--json" in args:
        out = Path(args[args.index("--json") + 1])
        args.remove(str(out))
    names = [a for a in args if not a.startswith("--")] or list(SCENES)
    if unknown := [a for a in names if a not in SCENES]:
        sys.exit(f"perf: unknown {unknown}; scenes: {', '.join(SCENES)}")
    (shots.OUT / "pages").mkdir(parents=True, exist_ok=True)
    shots.SHOTS.update(SCENES)
    pages = {n: shots._page(n) for n in names}  # (before any event loop: the setups run their own)
    rows = [asyncio.run(measure(n, pages[n], gpu, 8.0, 9340 + i, uncapped)) for i, n in enumerate(names)]
    print(f"renderer: {rows[0]['renderer']}")
    print(f"{'scene':<13}{'CPU ms/s':>9}{'main':>8}{'script':>8}{'st+lay':>8}{'fps':>6}{'p50':>7}{'p95':>7}{'worst':>7}")
    for r in rows:
        print(f"{r['scene']:<13}{r['cpu']:>9.0f}{r['main']:>8.0f}{r['script']:>8.0f}{r['style_layout']:>8.0f}"
              f"{r['fps']:>6.0f}{r['p50']:>7.1f}{r['p95']:>7.1f}{r['worst']:>7.1f}")
        for e in r["errors"]:
            print(f"    - {e}")
    if out:
        out.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
