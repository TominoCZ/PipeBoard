"""The user's board: positions, nicknames and their history, hidden cards, the view.

Saved as JSON in ~/.config/pipeboard/ (PIPEBOARD_CONFIG_DIR overrides). Cards are keyed by
graph.py's stable keys, never by PipeWire ids, so the board survives restarts (PLAN §7).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HISTORY = 12  # earlier names kept per card


def config_dir() -> Path:
    base = os.environ.get("PIPEBOARD_CONFIG_DIR") or Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pipeboard"
    return Path(base)


def load(path: Path) -> dict:
    try:
        ws = json.loads(path.read_text())
    except FileNotFoundError:
        ws = {}
    except json.JSONDecodeError:
        # Keep the broken file for the user instead of overwriting it with an empty board.
        path.replace(path.with_suffix(".broken.json"))
        ws = {}
    ws.setdefault("cards", {})
    return ws


def save(path: Path, ws: dict) -> None:
    """Write atomically: a crash mid-write must not lose the board."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ws, indent=1, sort_keys=True))
    tmp.replace(path)


def rename(card: dict, title: str) -> None:
    """Set a nickname ("" restores the original); the one it replaces goes into the history."""
    title = title.strip()[:80]
    current = card.get("nick")
    if title == (current or ""):
        return
    names = [n for n in card.get("names", []) if n not in (title, current)]
    if current:
        names.insert(0, current)
    card["names"] = names[:HISTORY]
    if title:
        card["nick"] = title
    else:
        card.pop("nick", None)


def remember(card: dict, name: str) -> None:
    """Put a name the card had into its history (newest first, no repeats)."""
    card["names"] = [name] + [n for n in card.get("names", []) if n != name][:HISTORY - 1]


def place(cards: dict, moved: dict) -> None:
    for key, p in moved.items():
        if isinstance(p, dict) and all(isinstance(p.get(k), int | float) for k in ("x", "y")):
            cards.setdefault(str(key), {}).update(x=round(p["x"]), y=round(p["y"]))


def _view(ws: dict, op: dict) -> bool:
    if not isinstance(op.get("view"), dict):
        return False
    ws["view"] = {k: float(op["view"][k]) for k in ("x", "y", "z")}
    return True


def _portal(how) -> str | dict | None:
    """A portal as the page sends it, made safe: "hand" (badges by the sockets), or a Tidy's with
    where its two badges sit ({"how": "tidy", "out": [x, y] | None, "in": …}); else None."""
    if how == "hand":
        return how
    def spot(p):
        ok = isinstance(p, list) and len(p) == 2 and all(isinstance(v, int | float) for v in p)
        return [round(v) for v in p] if ok else None
    if isinstance(how, dict) and how.get("how") == "tidy":  # a spot left out: beside its socket
        return {"how": "tidy", "out": spot(how.get("out")), "in": spot(how.get("in"))}
    return None


def _portals(ws: dict, op: dict) -> bool:
    """Wires drawn as portals: {wire id: a portal (_portal) or null to undo}. A Tidy's own are
    redone each time (op tidy); the user's stay."""
    portals = ws.setdefault("portals", {})
    if op.get("tidy"):
        for wid in [w for w, how in portals.items() if isinstance(how, dict)]:
            del portals[wid]
    for wid, how in (op.get("set") or {}).items():
        if (safe := _portal(how)) is not None:
            portals[str(wid)] = safe
        else:
            portals.pop(str(wid), None)
    return True


WHOLE = {"place": lambda ws, op: place(ws["cards"], op.get("cards") or {}) or True,
         "view": _view, "portals": _portals,
         # Tidy, all at once (owner: no steps seen): the cards' new places and its own portals
         "tidy": lambda ws, op: (place(ws["cards"], op.get("cards") or {})
                                 or _portals(ws, {**op, "tidy": True})),
         # whether Tidy folds a long board into rows with portals (the bar's switch; on unless off)
         "tidy-portals": lambda ws, op: ws.update(tidy_portals=bool(op.get("on"))) or True}


def apply(ws: dict, op: dict) -> bool:
    """Apply one board operation from the page; return whether anything changed."""
    kind, cards = op.get("op"), ws["cards"]
    if kind in WHOLE:
        return WHOLE[kind](ws, op)
    key = str(op.get("key", ""))
    if not key:
        return False
    if kind == "rename":
        rename(cards.setdefault(key, {}), str(op.get("title", "")))
    elif kind == "forget-name":
        card = cards.setdefault(key, {})
        card["names"] = [n for n in card.get("names", []) if n != op.get("name")]
    elif kind == "hide":
        cards.setdefault(key, {})["hidden"] = bool(op.get("hidden"))
    else:
        return False
    return True


class Store:
    """One config folder: the board being worked on, the settings, and the saved profiles."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.board = folder / "workspace.json"
        self.profiles = folder / "profiles"
        self.settings_path = folder / "settings.json"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.profiles.glob("*.json"))

    def _path(self, name: str) -> Path:
        from pipeboard.profiles import valid_name  # the name becomes a file name

        if not valid_name(name):
            raise ValueError(f"not a profile name: {name!r}")
        return self.profiles / f"{name}.json"

    def read(self, name: str) -> dict | None:
        path = self._path(name)
        return load(path) if path.exists() else None

    def write(self, name: str, profile: dict) -> None:
        save(self._path(name), profile)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def settings(self) -> dict:
        try:
            found = json.loads(self.settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            found = {}
        return {"active": found.get("active"), "launch": found.get("launch"),
                "live": found.get("live", True),  # live wires: on unless turned off
                "board": found.get("board")}  # this board's id, tagged on its nodes (devices.BOARD)

    def save_settings(self, settings: dict) -> None:
        save(self.settings_path, settings)
