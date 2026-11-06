"""The README's pictures, rendered from the real page (the shots' fixture boards, and the owner's
own board read-only: a snapshot of the live graph and a copy of the board file).

    uv run --with playwright --with pillow python scripts/readme.py [name ...]

Playwright only for this script (animated pictures need frames over time); it drives the
installed Chrome. Writes docs/images/; the diagnostics video (docs/images/diag.mp4) is the owner's
own recording, turned into a GIF preview here.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts")]
import shots  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

IMG = ROOT / "docs" / "images"
# Real music for the diagnostic cards (owner: as detailed as on the real board): a few seconds
# recorded passively from the owner's Waveform card (pw-record, 48 kHz stereo f32), not kept in
# the repository. Without it the cards stay silent in the pictures.
SOUND = ROOT / ".dev" / "readme-sound.raw"
DIAG_KEYS = ["pb:dwave01", "pb:dster01", "pb:dcount01", "pb:dmeter01", "pb:dspec01"]
PLAY = """async ([uri, keys]) => {
  const f = new Float32Array(await (await fetch(uri)).arrayBuffer()), n = 960, frames = f.length / 2;
  let at = 48000;  // (a second in)
  DIAG.keys(keys);
  const step = () => {
    const bytes = new Uint8Array(4 + n * keys.length * 8); bytes.fill(255, 0, 4);
    const out = new Float32Array(bytes.buffer, 4);
    for (let j = 0; j < n; j++, at = (at + 1) % frames)
      for (let i = 0; i < keys.length; i++) { out[(j * keys.length + i) * 2] = f[at * 2]; out[(j * keys.length + i) * 2 + 1] = f[at * 2 + 1]; }
    DIAG.frame(bytes.buffer);
  };
  for (let i = 0; i < 12; i++) step();
  setInterval(step, 20);
}"""
SCALE = 1.5  # the stills' pixel density (as before: 2475 px wide for a 1650 px window)
WIDE, GIF = (1650, 958), (1200, 750)


def mine(folder: Path, _dump: list) -> list:
    """The owner's board: the live graph now (only read), and a copy of the board file."""
    snap = folder / "pw-dump.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "capture_fixture.py"), str(snap)],
                   check=True, capture_output=True)
    board = json.loads((Path.home() / ".config" / "pipeboard" / "workspace.json").read_text())
    for key, dev in board.get("devices", {}).items():  # (the copy only: the diagnostic cards have
        if dev.get("type") in ("waveform", "stereo", "counter", "meter", "spectrum"):  # a section of their own)
            board.setdefault("cards", {}).setdefault(key, {})["hidden"] = True
    board["tidy_portals"] = True  # (Tidy folds it into rows, as a long board reads best)
    (folder / "workspace.json").write_text(json.dumps(board))
    return json.loads(snap.read_text())


# The owner's own names, swapped for plain ones in the pictures that show them up close.
PLAIN = {"Steinberg": "Audio Interface", "Nucleo": "USB Pedal", "CutWire": "Game", "spotify": "Music Player",
         "WEBRTC VoiceEngine": "Voice Chat"}


def mine_plain(folder: Path, dump: list) -> list:
    """The owner's board (as mine), its devices and apps given plain names, Show latency on."""
    snap = mine(folder, dump)
    board = json.loads((folder / "workspace.json").read_text())
    for key, card in board.get("cards", {}).items():
        for part, nick in PLAIN.items():
            if part.lower() in key.lower():
                card["nick"] = nick
    (folder / "workspace.json").write_text(json.dumps(board))
    (folder / "settings.json").write_text(json.dumps({"show_latency": True}))
    return snap


# name: (scenario, setup, the page's own steps, what to take)
# take: ("still", size) the window · ("panel", size, selector) one element · ("gif", size,
# seconds, fps) the window over time · ("crop", size, (x0, y0, x1, y1)) part of the window
SHOTS = {
    "board1": ("machine", None, "PB.debug.fakeLive()", ("gif", GIF, 1.8, 30)),
    "board2": ("machine", mine, "PB.debug.tidy() || PB.debug.fakeLive(99, null, false)", ("still", WIDE)),
    "portals": ("machine", shots.chained, "PB.debug.tidy()", ("still", shots.BIG)),
    "portals-hover": ("machine", shots.chained, "PB.debug.tidy() || PB.debug.portalHover(1) || PB.debug.fakeLive()",
                      ("gif", GIF, 1.8, 30)),  # (a board's shape: Tidy folds it into rows)
    "effects/eq-dynamic": ("machine", shots.equalized, shots.SHOTS["eq-dynamic"][3], ("panel", WIDE, ".eq-panel")),
    "effects/limiter": ("machine", shots.equalized, shots.SHOTS["limiter"][3], ("panel", WIDE, ".eq-panel")),
    "effects/compressor": ("machine", shots.equalized, shots.SHOTS["compressor"][3], ("panel", WIDE, ".eq-panel")),
    "effects/gate": ("machine", shots.equalized, shots.SHOTS["gate"][3], ("panel", WIDE, ".eq-panel")),
    "effects/deesser": ("machine", shots.equalized, shots.SHOTS["deesser"][3], ("panel", WIDE, ".eq-panel")),
    "effects/denoise": ("machine", shots.equalized, shots.SHOTS["denoise"][3], ("panel", WIDE, ".eq-panel")),
    "engine": ("machine", None, "PB.debug.engineOpen({block: 256, blocks: 2, prio: 90, tsched: true}, true)",
               ("panel", WIDE, ".eng")),
    "latency-path": ("machine", mine_plain, "PB.debug.pathView('Voice Mix')", ("still", WIDE)),
    "diagnostics": ("machine", shots.diagnosed, "PB.debug.tidy() || PB.debug.fakeLive(99, null, false) || "
                    "PB.debug.fit(['music_player', 'pb:dwave01', 'pb:dster01', 'pb:dcount01', 'pb:dmeter01', 'pb:dspec01'])",
                    ("still", (1650, 1100))),
    "diag/waveform": ("machine", shots.diagnosed, "PB.debug.tidy() || PB.debug.openDiag('pb:dwave01')",
                      ("panel", WIDE, ".diag-panel")),
    "diag/stereo": ("machine", shots.diagnosed, "PB.debug.tidy() || PB.debug.openDiag('pb:dster01')",
                    ("panel", WIDE, ".diag-panel")),
    "diag/meter": ("machine", shots.diagnosed, "PB.debug.tidy() || PB.debug.openDiag('pb:dmeter01')",
                   ("panel", WIDE, ".diag-panel")),
    "diag/spectrum": ("machine", shots.diagnosed, "PB.debug.tidy() || PB.debug.openDiag('pb:dspec01')",
                      ("panel", WIDE, ".diag-panel")),
}


def page_for(name: str, scenario: str, setup, steps: str) -> Path:
    """The page, its steps run by the page itself once it has drawn (as the shots' are)."""
    # (the debug hooks kept within reach afterwards: the animations step through moments with them)
    shots.SHOTS[f"readme-{name.replace('/', '-')}"] = (scenario, WIDE, setup, f"{steps}; window.__PB = PB")
    return shots._page(f"readme-{name.replace('/', '-')}")


def gif(frames: list, out: Path) -> None:
    """Frames and their moments (seconds) in one looping GIF, one palette."""
    images = [Image.open(f).convert("RGB") for f, _ in frames]
    gaps = [max(20, round((b - a) * 1000)) for (_, a), (_, b) in zip(frames, frames[1:], strict=False)] + [30]
    pal = images[len(images) // 2].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    images = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in images]
    images[0].save(out, save_all=True, append_images=images[1:], duration=gaps, loop=0, optimize=True)


def take(browser, name: str, page: Path, how: tuple) -> None:
    out = IMG / (name + (".gif" if how[0] == "gif" else ".png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    size = how[1]
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]},
                          device_scale_factor=1 if how[0] == "gif" else SCALE)
    pg.goto(page.as_uri())
    if name.startswith("diag") and SOUND.exists():
        pg.wait_for_timeout(300)
        pg.evaluate(PLAY, [SOUND.as_uri(), DIAG_KEYS])
    pg.wait_for_timeout(2200)  # (the page's steps run, fits and live pictures settle)
    # (no tooltip, no "unsaved changes" box: a preview of a copy; behind a panel, no board)
    pg.add_style_tag(content="#tip, #changes, #profile-note { display: none !important; }"
                             + ("#world, #grid, #bar, #problems-btn { visibility: hidden; }" if how[0] == "panel" else ""))
    if how[0] == "still":
        pg.screenshot(path=str(out))
    elif how[0] == "panel":
        pg.locator(how[2]).first.screenshot(path=str(out))
    else:  # each frame the wires' sound at an exact moment (PB.debug.liveAt), so the motion is even
        tmp, frames, step = Path("/tmp/pb-readme-frames"), [], 1 / how[3]
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        for i in range(round(how[2] * how[3])):
            pg.evaluate(f"() => window.__PB.debug.liveAt({2000 + i * step * 1000})")
            f = tmp / f"{i:03}.png"
            pg.screenshot(path=str(f))
            frames.append((f, i * step))
        gif(frames, out)
    pg.close()
    print(f"{out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB")


def video_gif() -> None:
    """The owner's recording as a GIF preview the README can show (GitHub does not play a video
    file linked from the repository): 960 px, 15 frames a second, one palette."""
    src, out = IMG / "diag.mp4", IMG / "diag.gif"
    if not src.exists():
        return
    vf = "fps=15,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, str(out)], check=True)
    print(f"{out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB")


def main() -> None:
    names = sys.argv[1:] or [*SHOTS, "video"]
    # (the pages first: building one runs the server's own loop, which Playwright's would block)
    pages = {n: page_for(n, *SHOTS[n][:3]) for n in names if n in SHOTS}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots._chrome(), args=["--allow-file-access-from-files"])
        for name in names:
            if name in SHOTS:
                take(browser, name, pages[name], SHOTS[name][3])
        browser.close()
    if "video" in names:
        video_gif()


main()
