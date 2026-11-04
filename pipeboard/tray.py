"""The tray icon: a StatusNotifierItem with a dbusmenu, over the session D-Bus (PLAN §8).

The quoted annotations below are D-Bus signatures that dbus-fast reads, not Python types: keep
the quotes, and no `from __future__ import annotations` here (it would quote them again).

Click: open or raise the window. Menu: Open PipeBoard, Launch at login, Quit (the wires'
animation is the window's own: its Animate button; owner).
While an error-level problem stands, the icon shows a dot (NeedsAttention); the tooltip names the
active profile and the problems. Without a session bus or a tray host this logs and does nothing:
PipeBoard runs on, without a tray.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from dbus_fast import PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

log = logging.getLogger(__name__)
ITEM, MENU = "/StatusNotifierItem", "/MenuBar"


class Item(ServiceInterface):
    """What the panel shows: icon, tooltip, status; a click calls `open`."""

    def __init__(self, open_window: Callable[[], None]):
        super().__init__("org.kde.StatusNotifierItem")
        self.open_window = open_window
        self.status, self.tip = "Active", "Routing your audio"

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":  # noqa: F821, N802
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":  # noqa: F821, N802
        return "pipeboard"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":  # noqa: F821, N802
        return "PipeBoard"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":  # noqa: F821, N802
        return self.status

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":  # noqa: F821, N802
        return "pipeboard-tray"

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":  # noqa: F821, N802
        return "pipeboard-tray-alert"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        """Where the panel finds our icons, whatever its icon cache believes."""
        return str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "icons")

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":  # noqa: F821, N802
        return ["pipeboard-tray", [], "PipeBoard", self.tip]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":  # noqa: F821, N802
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":  # noqa: F821, N802
        return MENU

    @method()
    def Activate(self, x: "i", y: "i"):  # noqa: F821, N802
        self.open_window()

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):  # noqa: F821, N802
        self.open_window()

    @method()
    def ContextMenu(self, x: "i", y: "i"):  # noqa: F821, N802
        pass  # the panel draws the dbusmenu itself

    @method()
    def Scroll(self, delta: "i", orientation: "s"):  # noqa: F821, N802
        pass

    @signal()
    def NewStatus(self, status) -> "s":  # noqa: F821, N802
        return status

    @signal()
    def NewToolTip(self):  # noqa: N802
        pass


class Menu(ServiceInterface):
    """The right-click menu, in the dbusmenu protocol the panel speaks."""

    def __init__(self, items: Callable[[], list[dict]]):
        super().__init__("com.canonical.dbusmenu")
        self.items, self.revision = items, 1

    def _layout(self) -> list:
        children = []
        for i, it in enumerate(self.items(), start=1):
            props = {"type": Variant("s", "separator")} if it.get("sep") else {
                "label": Variant("s", it["label"]), "enabled": Variant("b", True)}
            if "checked" in it:
                props["toggle-type"] = Variant("s", "checkmark")
                props["toggle-state"] = Variant("i", 1 if it["checked"] else 0)
            children.append(Variant("(ia{sv}av)", [i, props, []]))
        return [0, {"children-display": Variant("s", "submenu")}, children]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":  # noqa: F821, N802
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":  # noqa: F821, N802
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":  # noqa: F821, N802
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":  # noqa: F821, N802
        return []

    @method()
    def GetLayout(self, parent: "i", depth: "i", names: "as") -> "u(ia{sv}av)":  # noqa: F821, N802
        return [self.revision, self._layout()]

    @method()
    def GetGroupProperties(self, ids: "ai", names: "as") -> "a(ia{sv})":  # noqa: F821, N802
        return [[c.value[0], c.value[1]] for c in self._layout()[2] if not ids or c.value[0] in ids]

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":  # noqa: A002, F821, N802
        found = next((c.value[1] for c in self._layout()[2] if c.value[0] == id), {})
        return found.get(name, Variant("s", ""))

    @method()
    def Event(self, id: "i", event: "s", data: "v", stamp: "u"):  # noqa: A002, F821, N802
        if event == "clicked":
            items = self.items()
            if 0 < id <= len(items) and items[id - 1].get("action"):
                items[id - 1]["action"]()

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":  # noqa: F821, N802
        for id_, event, data, stamp in events:
            self.Event(id_, event, data, stamp)
        return []

    @method()
    def AboutToShow(self, id: "i") -> "b":  # noqa: A002, F821, N802
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":  # noqa: F821, N802
        return [[], []]

    @signal()
    def LayoutUpdated(self) -> "ui":  # noqa: F821, N802
        return [self.revision, 0]

    def changed(self) -> None:
        self.revision += 1
        self.LayoutUpdated()


class Tray:
    """Puts PipeBoard in the panel. start() never raises; a tray that cannot appear is logged."""

    def __init__(self, open_window: Callable[[], None], items: Callable[[], list[dict]]):
        self.item, self.menu = Item(open_window), Menu(items)
        self.bus: Optional[MessageBus] = None  # noqa: UP045 - no __future__ annotations here
        self.task: Optional[asyncio.Task] = None  # noqa: UP045
        self.name = ""

    async def start(self) -> bool:
        """Export the icon and its menu, then keep it in the panel (_stay). Never raises."""
        try:
            self.bus = await MessageBus().connect()
            self.bus.export(ITEM, self.item)
            self.bus.export(MENU, self.menu)
            self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            await self.bus.request_name(self.name)
        except Exception:  # noqa: BLE001 - no tray is no reason to stop routing audio
            log.exception("tray: could not put PipeBoard in the panel")
            return False
        self.task = asyncio.create_task(self._stay())
        return True

    async def _stay(self) -> None:
        """At login PipeBoard may start before the panel's tray is up, and a restarted panel
        forgets its icons: started in the tray only (user), it would then run unseen. So every
        3 s: is the icon registered with the panel's watcher? If not, register it."""
        where, warned = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher"), False
        while True:
            try:
                proxy = self.bus.get_proxy_object(*where, await self.bus.introspect(*where))
                watcher = proxy.get_interface("org.kde.StatusNotifierWatcher")
                if not any(i.startswith(self.name)
                           for i in await watcher.get_registered_status_notifier_items()):
                    await watcher.call_register_status_notifier_item(self.name)
                    log.info("tray: in the panel")
                warned = False
            except Exception:  # noqa: BLE001 - no panel yet: try again
                if not warned:
                    log.warning("tray: no panel to show in yet; trying every 3 s")
                    warned = True
            await asyncio.sleep(3)

    def update(self, alert: bool, tip: str) -> None:
        """Show the problem dot or not, and the tooltip; the menu is read again when next opened."""
        status = "NeedsAttention" if alert else "Active"
        if status != self.item.status:
            self.item.status = status
            self.item.NewStatus(status)
        if tip != self.item.tip:
            self.item.tip = tip
            self.item.NewToolTip()
        self.menu.changed()

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.bus:
            self.bus.disconnect()
