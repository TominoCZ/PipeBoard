"""pipeboard: serve the board and open it in a Chrome app window.

    pipeboard                    the live PipeWire graph
    pipeboard --fixture [name]   a saved graph (machine, pedal, problems); never touches PipeWire
    pipeboard --no-window        serve only, at http://127.0.0.1:7870 (no window, no tray)
    pipeboard --background       minimized to the tray, as at login
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import functools
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from aiohttp import web

from pipeboard import pipewire, workspace
from pipeboard.server import Board, app

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


@functools.cache
def chrome() -> str | None:
    """A Chrome or Chromium that can start: --version goes through the packages' wrapper scripts to
    the real binary, which the loader runs only with all its libraries there. GPU drivers are
    loaded later and not looked at. Once per run."""
    for name in BROWSERS:
        if path := shutil.which(name):
            try:
                if subprocess.run([path, "--version"], capture_output=True,
                                  timeout=10).returncode == 0:
                    return path
            except (OSError, subprocess.TimeoutExpired):
                pass
            print(f"pipeboard: {path} cannot start", file=sys.stderr)
    return None


def open_window(url: str, profile: Path) -> None:
    """The page in a bare app window with its own profile, so it closes like an app; in the
    default browser when no Chrome or Chromium can start."""
    if not (browser := chrome()):
        if not webbrowser.open(url):
            print(f"pipeboard: no browser found; open {url} yourself", file=sys.stderr)
        return
    subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                      "--no-default-browser-check", "--window-size=1500,920"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# Chrome names an --app window "chrome-<host>_<path, / as _>-<profile>"; KDE looks for a desktop
# entry of that name for the taskbar icon. The /pipeboard/ path keeps it apart from other local
# app windows (Jarvis is also a 127.0.0.1 Chrome app).
APP_ID = "chrome-127.0.0.1__pipeboard_-Default"
ENTRY = f"""[Desktop Entry]
Type=Application
Name=PipeBoard
GenericName=Audio routing
Comment=Route your PipeWire audio on a node board
Exec={ROOT / "run.sh"}
Icon={{icon}}
Terminal=false
Categories=AudioVideo;Audio;Mixer;
StartupWMClass={APP_ID}
"""


def install_entry() -> None:
    """The taskbar icon and the app-menu entry, kept in step with this checkout. Never fatal."""
    share = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    # The icon by its file, not its name: a panel that started before the icon existed keeps a
    # stale name lookup (seen: a blank document icon while KDE's own lookup found it).
    icon = share / "icons/hicolor/scalable/apps/pipeboard.svg"
    files = {share / "applications" / f"{APP_ID}.desktop": ENTRY.replace("{icon}", str(icon)),
             **{share / f"icons/hicolor/scalable/apps/{name}.svg":
                (Path(__file__).parent / "web" / f"{svg}.svg").read_text()
                for name, svg in (("pipeboard", "icon"), ("pipeboard-tray", "tray"),
                                  ("pipeboard-tray-alert", "tray-alert"))}}
    try:
        for path, text in files.items():
            if not path.exists() or path.read_text() != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
    except OSError as e:
        print(f"pipeboard: could not install the desktop entry: {e}", file=sys.stderr)


def single(folder: Path):
    """Hold this board's lock for as long as PipeBoard runs; None if another copy holds it.

    flock is released by the kernel when the process ends, however it ends: never stale. Keyed by
    the config folder, so a fixture or test board with its own folder is a different instance.
    """
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    key = hashlib.sha1(str(folder.resolve()).encode()).hexdigest()[:10]
    held = open(runtime / f"pipeboard-{key}.lock", "a+")  # noqa: SIM115 - held until exit
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held.close()
        return None
    held.truncate(0)
    held.write(f"{os.getpid()}\n")
    held.flush()
    return held


def tell(text: str) -> None:
    """A desktop dialog that stays up after PipeBoard has gone; the terminal gets it too."""
    print(f"pipeboard: {text}", file=sys.stderr)
    icon = str(Path(__file__).parent / "web" / "icon.svg")
    for cmd in (["kdialog", "--title", "PipeBoard", "--icon", icon, "--sorry", text],
                ["zenity", "--warning", "--title", "PipeBoard", f"--text={text}"],
                ["notify-send", "--icon", icon, "PipeBoard", text]):
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return


def source(fixture: str | None):
    if not fixture:
        return pipewire.Live()
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    import scenarios  # a development checkout only

    return pipewire.Fixture(scenarios.load(fixture))


def main() -> None:
    ap = argparse.ArgumentParser(prog="pipeboard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", nargs="?", const="machine", metavar="NAME")
    ap.add_argument("--no-window", action="store_true", help="serve only: no window, no tray")
    ap.add_argument("--background", action="store_true",
                    help="start minimized to the tray (as at login)")
    ap.add_argument("--port", type=int, default=7870)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if args.fixture and args.port == 7870:
        args.port = 7871  # so a fixture board can run beside the real one
    url = f"http://127.0.0.1:{args.port}/pipeboard/"
    data = workspace.config_dir()
    # A fixture keeps its own board and profiles, so trying things never touches the real ones.
    folder = data / f"fixture-{args.fixture}" if args.fixture else data
    folder.mkdir(parents=True, exist_ok=True)
    lock = single(folder)
    if lock is None:
        tell("PipeBoard is already running: click its icon in the system tray. "
             "This second copy closes.")
        sys.exit(1)
    install_entry()
    serve(args, url, data, folder)


async def raise_window() -> bool:
    """Bring PipeBoard's open window to the front through KWin (KDE); False if that failed."""
    script = Path(tempfile.gettempdir()) / f"pipeboard-raise-{os.getpid()}.js"
    script.write_text(f"""for (const w of workspace.windowList())
  if (w.resourceClass === "{APP_ID}") {{ w.minimized = false; workspace.activeWindow = w; }}""")
    try:
        from dbus_fast.aio import MessageBus

        bus = await MessageBus().connect()
        scripting = bus.get_proxy_object("org.kde.KWin", "/Scripting", await bus.introspect(
            "org.kde.KWin", "/Scripting")).get_interface("org.kde.kwin.Scripting")
        sid = await scripting.call_load_script(str(script), "pipeboard-raise")
        path = f"/Scripting/Script{sid}"
        run = bus.get_proxy_object("org.kde.KWin", path, await bus.introspect("org.kde.KWin", path))
        await run.get_interface("org.kde.kwin.Script").call_run()
        await scripting.call_unload_script("pipeboard-raise")
        bus.disconnect()
        return True
    except Exception:  # noqa: BLE001 - not on KDE, or KWin said no: the caller opens a window
        logging.getLogger(__name__).info("could not raise the window through KWin", exc_info=True)
        return False
    finally:
        script.unlink(missing_ok=True)


def with_tray(application, board: Board, show) -> None:
    """The tray icon and its menu, started with the app and gone with it."""
    from pipeboard.tray import Tray

    def act(op: dict) -> None:
        asyncio.ensure_future(board.act(None, op))

    def items() -> list[dict]:
        return [{"label": "Open PipeBoard", "action": show},
                {"sep": True},
                {"label": "Launch at login", "checked": board.autostart,
                 "action": lambda: act({"op": "autostart", "on": not board.autostart})},
                {"sep": True},
                {"label": "Quit PipeBoard", "action": quit_once}]

    def quit_once() -> None:
        """One quit: a second one in the middle of quitting aborted it (no route-around, the
        window left behind, user)."""
        if not quitting:
            quitting.append(1)
            os.kill(os.getpid(), signal.SIGINT)

    quitting: list[int] = []

    tray = Tray(show, items)
    board.on_state = tray.update

    async def start(_app):
        await tray.start()

    async def stop(_app):
        tray.stop()

    application.on_startup.append(start)
    application.on_cleanup.append(stop)


def serve(args, url: str, data: Path, folder: Path) -> None:
    board = Board(source(args.fixture), folder)
    application = app(board, args.port)
    profile = data / ("chrome-fixture" if args.fixture else "chrome")

    def show() -> None:
        """The tray's click: raise the open window, or open one."""
        async def go():
            # KWin raises only the Chrome app window; a browser tab is opened again instead
            if not (board.pages and chrome() and await raise_window()):
                open_window(url, profile)
        asyncio.ensure_future(go())

    if not args.no_window:  # tests run with --no-window: no tray, no window
        with_tray(application, board, show)
    if not (args.no_window or args.background):  # at login (--background): only the tray
        async def window(_app):
            open_window(url, profile)
        application.on_startup.append(window)
    print(f"pipeboard: {url} ({board.source.mode})", flush=True)
    try:
        web.run_app(application, host="127.0.0.1", port=args.port, print=None)
    except OSError as e:  # the port is taken by some other program
        tell(f"PipeBoard could not start: port {args.port} is in use by another program "
             f"({e.strerror}).")
        sys.exit(1)


if __name__ == "__main__":
    main()
