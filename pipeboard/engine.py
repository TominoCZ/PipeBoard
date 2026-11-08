"""The audio engine (PLAN §8c): the graph's block size, the safety margin of the interface that
clocks it, and the realtime priority of PipeWire's audio thread.

They live in two files of PipeBoard's own, sorted after anything of the user's so they win, and
never in the user's files: PipeWire's (`99-pipeboard-engine.conf`: the block, pinned, and
`module.rt.args`) and WirePlumber's (the interface's ALSA period = the block, and its headroom).
`apply` puts them into the running audio at once without restarting PipeWire (apps stay
connected): the block and margin by making the interface's node again through WirePlumber.
Pinned: the block size is the graph's minimum and maximum too, so it never floats away from the
interface's ALSA period (the owner's pops in August came from a period that did not match it).
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path

from pipeboard import graph, headroom

BLOCKS = (64, 128, 256, 512, 1024, 2048)
# An interface with implicit feedback (its playback follows its capture's packets, the UR44C)
# moves sound in USB transfers of 1 ms: snd-usb-audio makes capture URBs, and the playback that
# follows them, at most 1 ms (endpoint.c, data_ep_set_params). A block that is not whole
# transfers saws the output buffer against them, and the clocks' drift pushes PipeWire's check
# over the edge: a resync, silence written, a click (owner: every power of two at 192 kHz did).
# Such an interface is offered whole milliseconds only.
STEP_MS = (1, 2, 4, 8, 16)
ASOUND = Path("/proc/asound")
# The safety margin in whole blocks (owner's UR44C: under one block the input dropped out ~100
# times a second; over one it did not fit its buffer of 3 periods). The buffer is made to fit:
# twice the block and the margin: PipeWire steers the fill to block + headroom but clamps that
# to half the buffer (alsa-pcm.c, max_delay), so a smaller buffer quietly cut the margin
# (owner: 3 blocks of 128 ran as 320 - block = 1.5 blocks).
MARGIN_BLOCKS = (1, 2, 3)
PRIO, NICE = (1, 95), (-19, 0)  # what the pipewire group may have (limits.d: rtprio 95, nice -19)
DEFAULT_PRIO, DEFAULT_NICE = 88, -11  # PipeWire's own (pipewire.conf, module-rt)
HEAD = "# Written by PipeBoard (Audio engine). Delete this file and restart PipeWire to undo.\n"


def files() -> dict[str, Path]:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return {"pipewire": base / "pipewire" / "pipewire.conf.d" / "99-pipeboard-engine.conf",
            "wireplumber": base / "wireplumber" / "wireplumber.conf.d" / "99-pipeboard-engine.conf"}


def _driver(nodes: dict[int, dict]) -> dict | None:
    """The ALSA node most of the graph follows (its clock), or None."""
    count = Counter(p.get("node.driver-id") for p in nodes.values() if p.get("node.driver-id"))
    alsa = [k for k, _ in count.most_common() if nodes.get(k, {}).get("device.api") == "alsa"]
    return nodes[alsa[0]] if alsa else None


_props = graph._props


def interfaces(dump: list[dict]) -> list[dict]:
    """Every sound card with audio nodes, by its device: a key that stays across restarts (its
    device.name), its name, its ALSA nodes, and the rate it runs at now (its nodes' format: a
    card can run at its own rate, beside another at the graph's; None while it is closed).
    Ordered by how much of the graph each one clocks, the busiest first."""
    nodes = {o["id"]: o for o in dump if o["type"].endswith("Node")
             and _props(o).get("device.api") == "alsa"
             and str(_props(o).get("media.class", "")).startswith("Audio/")}
    devs = {o["id"]: _props(o) for o in dump if o["type"].endswith("Device")}
    follows = Counter(_props(o).get("node.driver-id") for o in dump if o["type"].endswith("Node"))
    cards: dict = {}
    for nid, o in nodes.items():
        p = _props(o)
        dev = devs.get(p.get("device.id"), {})
        key = dev.get("device.name") or f"card{p.get('alsa.card', p.get('device.id'))}"
        c = cards.setdefault(key, {"key": key, "nodes": [], "props": [], "ids": set(),
                                   "title": dev.get("device.description")
                                   or p.get("api.alsa.card.name") or p.get("node.description"),
                                   "rate": None, "clocks": 0})
        c["nodes"].append(p.get("node.name"))
        c["props"].append(p)
        c["ids"].add(nid)
        c["clocks"] += follows.get(nid, 0)
        c["rate"] = c["rate"] or graph._rate(o)
    for c in cards.values():
        c["nodes"].sort()
    return sorted(cards.values(), key=lambda c: (-c["clocks"], c["title"] or c["key"]))


def pick(dump: list[dict], choice: str | None = None,
         cards: list[dict] | None = None) -> dict | None:
    """The interface the Audio engine shows: the one chosen (by key) while it is there, else the
    one most of the graph follows. A friend's two cards (on the motherboard, and a USB mic) each
    ran at their own rate, and the panel followed whichever clocked more at the moment."""
    cards = interfaces(dump) if cards is None else cards
    chosen = next((c for c in cards if c["key"] == choice), None)
    if chosen:
        return chosen
    nodes = {o["id"]: _props(o) for o in dump if o["type"].endswith("Node")}
    driver = _driver(nodes)
    return next((c for c in cards if driver and driver.get("node.name") in c["nodes"]), None)


def read(dump: list[dict], choice: str | None = None) -> dict:
    """What runs now: the rate and block, the interface shown (`pick`: its ALSA nodes, its period
    and safety margin, its own rate and the rates it could run at), every interface there is, and
    the priority PipeBoard's file sets (else PipeWire's own).

    Block, period and margin are counted at the graph's default rate (`rate`), as PipeWire counts
    its block: a card running at another rate gets the block scaled to it (quantum × its rate ÷
    the default), so a block is the same milliseconds on every card."""
    clock, cards = graph._clock(dump), interfaces(dump)
    iface = pick(dump, choice, cards)
    found = iface["props"] if iface else []
    ids = iface["ids"] if iface else set()
    at = (iface or {}).get("rate") or clock["rate"]
    scale = lambda n: round(n * clock["rate"] / at) if at else n  # noqa: E731 - to graph samples
    own = files()["pipewire"]
    text = own.read_text() if own.is_file() else ""
    def setting(key: str, dflt: int) -> int:
        m = re.search(rf"{key}\s*=\s*(-?\d+)", text)
        return int(m[1]) if m else dflt
    rules = own_rules()
    rule_of = lambda c: next((r for r in rules if c and set(r["nodes"]) & set(c["nodes"])), None)  # noqa: E731
    rule = rule_of(iface)
    return {"rate": clock["rate"], "block": clock["quantum"],
            "device": iface["key"] if iface else None,
            # (each one's own margin, scheduling and fixed rate: the panel's list of them)
            "interfaces": [{"key": c["key"], "title": c["title"], "rate": c["rate"],
                            "clocks": c["clocks"] > 0, "tsched": _timer(dump, c["ids"]),
                            "margin_blocks": (rule_of(c) or {}).get("blocks", 1),
                            "fixed_rate": (rule_of(c) or {}).get("rate")} for c in cards],
            "interface": iface["nodes"] if iface else [],
            "pcms": headroom.folders(found),  # (its buffers' status, for Buffer headroom)
            "card": next((p["alsa.card"] for p in found if p.get("alsa.card") is not None), None),
            "title": iface["title"] if iface else None,
            "dev_rate": iface["rate"] if iface else None,  # (None while it is closed)
            "rates": rates(found, dump, ids),
            "fixed_rate": (rule or {}).get("rate"),
            "period": scale(max((int(p.get("api.alsa.period-size") or 0) for p in found),
                                default=0)),
            "margin": scale(_margin(dump, found, ids)),
            "tsched": _timer(dump, ids),
            "margin_blocks": (rule or {}).get("blocks", 1),
            # (another interface PipeBoard set runs on interrupts: a new block reopens it too)
            "irq_elsewhere": any(not r["tsched"] for r in rules if r is not rule),
            "prio": setting("rt.prio", DEFAULT_PRIO), "nice": setting("nice.level", DEFAULT_NICE),
            "own": own.is_file()}


def _margin(dump: list[dict], found: list[dict], ids: set) -> int:
    """The margin the interface was made with, in its own samples: its nodes' headroom (from
    PipeBoard's rule, or the user's). PipeWire's latency figure for its ports was read before: it
    is the headroom scaled by the graph's rate when the device opened, which after a change of
    rate still held the old one (a friend: the margin drawn as long as before the change until
    the margin was set again), and it adds what comes from upstream. Only when no node says its
    headroom: that figure, its own direction only."""
    said = max((int(p.get("api.alsa.headroom") or 0) for p in found), default=0)
    if said:
        return said
    own = {"input": "Input", "output": "Output"}
    ran = [lat.get("maxRate", 0) for o in dump if o["type"].endswith("Port")
           and _props(o).get("node.id") in ids
           for lat in ((o.get("info") or {}).get("params") or {}).get("Latency") or []
           if lat.get("maxQuantum") and lat.get("direction") == own.get(
               (o.get("info") or {}).get("direction"))]
    return max(ran, default=0)


# The rates a card can run at, offered only when the card itself lists them (owner: "ONLY if we
# have supported rates provided"): a USB card's stream0 ("Rates: 44100, 48000, 96000"), an HDA
# codec's default PCM ("rates [0x560]: 44100 48000 96000 192000"), else PipeWire's own list of
# them for the node (an enumeration, not a range). /proc first: once PipeBoard fixes a card's rate,
# PipeWire lists only that one.
RATES_LINE = re.compile(r"^\s*Rates:\s*([\d,\s]+)$", re.M)
CODEC_RATES = re.compile(r"Default PCM:\s*\n\s*rates \[0x[0-9a-f]+\]:\s*([\d ]+)")


def usb_rates(stream0: str) -> list[int] | None:
    """The rates both directions of a USB card offer (stream0's Playback and Capture parts)."""
    sides = [part for part in re.split(r"^(?=Playback:|Capture:)", stream0, flags=re.M)
             if part.startswith(("Playback:", "Capture:"))]
    found = None
    for part in sides:
        here = {int(r) for line in RATES_LINE.findall(part) for r in re.findall(r"\d+", line)}
        if not here:  # ("8000 - 48000 (continuous)": no list)
            return None
        found = here if found is None else found & here
    return sorted(found) if found and len(found) > 1 else None


def codec_rates(codec: str) -> list[int] | None:
    m = CODEC_RATES.search(codec)
    found = sorted({int(r) for r in m[1].split()}) if m else []
    return found if len(found) > 1 else None


def _enum_rates(dump: list[dict], ids: set) -> list[int] | None:
    """PipeWire's EnumFormat rates for the nodes, when they are a list: pw-dump writes a choice of
    values as {"default": …, "alt1": …}, a range as {"min": …, "max": …} (no list: None)."""
    found: set[int] = set()
    for o in dump:
        if o["id"] not in ids:
            continue
        for f in ((o.get("info") or {}).get("params") or {}).get("EnumFormat") or []:
            r = f.get("rate") if isinstance(f, dict) else None
            if isinstance(r, dict) and "min" not in r:
                found |= {v for v in r.values() if isinstance(v, int)}
            elif isinstance(r, list):
                found |= {v for v in r if isinstance(v, int)}
    return sorted(found) if len(found) > 1 else None


def rates(found: list[dict], dump: list[dict], ids: set) -> list[int] | None:
    """The rates this card lists, or None: then no rate is offered."""
    card = next((p["alsa.card"] for p in found if p.get("alsa.card") is not None), None)
    if card is not None:
        folder = ASOUND / f"card{card}"
        with contextlib.suppress(OSError):
            if (listed := usb_rates((folder / "stream0").read_text())) is not None:
                return listed
        with contextlib.suppress(OSError):
            for codec in sorted(folder.glob("codec#*")):
                if (listed := codec_rates(codec.read_text())) is not None:
                    return listed
    return _enum_rates(dump, ids)


RULE = re.compile(r"matches\s*=\s*\[(.*?)\]\s*actions\s*=\s*\{\s*update-props\s*=\s*\{(.*?)\}",
                  re.S)


def own_rules(text: str | None = None) -> list[dict]:
    """PipeBoard's WirePlumber rules, one per interface (a file from before held one): its node
    names, margin in blocks, scheduling and the rate it fixes (None: the graph's). A card that is
    not plugged in keeps its rule."""
    if text is None:
        path = files()["wireplumber"]
        text = path.read_text() if path.is_file() else ""
    out = []
    for names, props in RULE.findall(text):
        kv = dict(re.findall(r"([\w.-]+)\s*=\s*(\S+)", props))
        size, head = int(kv.get("api.alsa.period-size", 0)), int(kv.get("api.alsa.headroom", 0))
        blocks = head // size if size else 1
        out.append({"nodes": re.findall(r'node\.name\s*=\s*"([^"]*)"', names),
                    "blocks": blocks if blocks in MARGIN_BLOCKS else 1,
                    "tsched": kv.get("api.alsa.disable-tsched") == "false",
                    "rate": int(kv["audio.rate"]) if kv.get("audio.rate", "").isdigit() else None})
    return out


def _timer(dump: list[dict], ids: set) -> bool:
    """Whether the interface runs on PipeWire's timer (else on its interrupts: the Pro Audio
    profile's default, `api.alsa.disable-tsched`), from its nodes' live Props."""
    for o in dump:
        if o["id"] in ids:
            for props in ((o.get("info") or {}).get("params") or {}).get("Props") or []:
                pairs = props.get("params") or []
                found = dict(zip(pairs[::2], pairs[1::2], strict=False))
                if "api.alsa.disable-tsched" in found:
                    return not found["api.alsa.disable-tsched"]
    return False


def usb_step(stream0: str, rate: int) -> int | None:
    """Frames per USB transfer when they must line up with the block (implicit feedback: 1 ms);
    None for any other interface (its playback transfers are fitted to the block) or a rate
    that is no whole number of samples a millisecond (44.1 kHz)."""
    if "Implicit Feedback Mode: Yes" not in stream0 or not rate or rate % 1000:
        return None
    return rate // 1000


def blocks(card: int | None, rate: int, base: int = 0) -> tuple[list[int] | None, int | None]:
    """The block sizes to offer for the interface on this ALSA card and its transfer step, or
    (None, None) when any size lines up (the panel's own list then). `rate`: the card's own;
    `base`: the graph's default rate, which blocks are counted at (0: the same)."""
    try:
        step = usb_step((ASOUND / f"card{card}" / "stream0").read_text(), rate)
    except OSError:  # (not USB, or no card)
        step = None
    if step and base and base != rate:  # (a millisecond's transfer, in the graph's samples)
        step = step * base // rate if step * base % rate == 0 else None
    return ([step * m for m in STEP_MS], step) if step else (None, None)


def check(op: dict, allowed=BLOCKS, offered: list[int] | None = None) -> dict:
    """A request's values, or ValueError: only the steps the panel offers, in the allowed range,
    and a rate only of those the interface lists (`offered`; none listed: none may be asked)."""
    try:
        v = {k: int(op[k]) for k in ("block", "margin", "prio", "nice")}
        v["tsched"] = bool(op.get("tsched", False))
        v["rate"] = int(op["rate"]) if op.get("rate") else None
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("The audio engine needs a block size, a margin and priorities.") from e
    if v["block"] not in allowed:
        raise ValueError("That block size is not one PipeBoard offers.")
    if v["margin"] % v["block"] or v["margin"] // v["block"] not in MARGIN_BLOCKS:
        raise ValueError("The safety margin is 1, 2 or 3 blocks.")
    if not (PRIO[0] <= v["prio"] <= PRIO[1] and NICE[0] <= v["nice"] <= NICE[1]):
        raise ValueError(f"Priority is {PRIO[0]}…{PRIO[1]} and nice {NICE[0]}…{NICE[1]}.")
    if v["rate"] is not None and v["rate"] not in (offered or ()):
        raise ValueError("That sample rate is not one this interface lists.")
    return v


def _rule(nodes: list[str], block: int, r: dict, scale: tuple[int, int]) -> str:
    """One interface's rule: its ALSA period is the block at its own rate (PipeWire scales the
    block by the card's rate over the default one: `scale`), its margin whole periods, and the
    rate it is fixed at with the graph made to follow it (node.force-rate), if one was chosen."""
    at, base = scale
    period = block * at // base if at and base else block
    matches = " ".join(f"{{ node.name = {json.dumps(n)} }}" for n in nodes)
    # On interrupts the buffer holds twice the block and margin (half of it is all PipeWire fills).
    buffer = "" if r.get("tsched") else f"api.alsa.period-num = {2 * (r['blocks'] + 1)} "
    rate = f"audio.rate = {r['rate']} node.force-rate = {r['rate']} " if r.get("rate") else ""
    return (f"  {{\n    matches = [ {matches} ]\n"
            f"    actions = {{ update-props = {{ api.alsa.period-size = {period} "
            f"api.alsa.headroom = {period * r['blocks']} {buffer}{rate}"
            f"api.alsa.disable-tsched = {str(not r.get('tsched', False)).lower()} }} }}\n  }}\n")


def texts(v: dict, interface: list[str], others: list[dict] = (), base: int = 0,
          at: int = 0) -> dict[str, str]:
    """Both files' text for these values (the interface: its ALSA node names). `others`: the
    other interfaces' rules (own_rules), kept, each with its period fitted to the new block;
    `base` the graph's default rate and `at` the interface's own (0: the same)."""
    pw = (f"{HEAD}context.properties = {{\n"
          f"    default.clock.quantum     = {v['block']}\n"
          f"    default.clock.min-quantum = {v['block']}\n"
          f"    default.clock.max-quantum = {v['block']}\n"
          f"    module.rt.args = {{ rt.prio = {v['prio']} nice.level = {v['nice']} }}\n}}\n")
    mine = {"blocks": v["margin"] // v["block"], "tsched": v.get("tsched"), "rate": v.get("rate")}
    rules = [_rule(interface, v["block"], mine, (v.get("rate") or at, base))] if interface else []
    rules += [_rule(r["nodes"], v["block"], r, (r.get("rate") or r.get("at") or 0, base))
              for r in others if r["nodes"] and not set(r["nodes"]) & set(interface)]
    wp = f"{HEAD}monitor.alsa.rules = [\n{''.join(rules)}]\n"
    return {"pipewire": pw, "wireplumber": wp if rules else None}


def write(contents: dict[str, str | None]) -> None:
    """Put the files as given: a text is written, None removes the file (going back to none)."""
    for key, path in files().items():
        text = contents.get(key)
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


def current() -> dict[str, str | None]:
    """The files as they are now, to go back to."""
    return {k: (p.read_text() if p.is_file() else None) for k, p in files().items()}


async def _run(*cmd: str) -> str | None:
    """Run a tool; its complaint, or None. pw-cli exits 0 even when the command failed, and only
    prints the error (a Suspend without its argument passed every test that way), so its output
    is read too (but not its usual noise about object types it does not know)."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out = (await proc.communicate())[0].decode()
    said = [line for line in out.splitlines() if re.match(r'(Error: "|remote \d+: error)', line)
            and "unsupported type" not in line]
    if proc.returncode or said:
        return f"{cmd[0]}: {(said or [out.strip() or 'failed'])[0]}"
    return None


async def _daemon() -> tuple[str, list[str]]:
    """PipeWire's own process and its audio threads (data-loop.*), for its priority."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "show", "-p", "MainPID", "--value", "pipewire.service",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    pid = (await proc.communicate())[0].decode().strip()
    tasks = Path(f"/proc/{pid}/task")
    if pid in ("", "0") or not tasks.is_dir():
        return pid, []
    threads = [t.name for t in tasks.iterdir() if (t / "comm").read_text().startswith("data-loop")]
    return pid, threads


# CPU idle states (owner: a toggle for each). Any CPU, any idle driver: the kernel's cpuidle
# list, by name and wake time. POLL is the CPU busy-waiting, not a sleep: never offered.
CPUS = Path("/sys/devices/system/cpu")
RULES = Path("/etc/udev/rules.d")
OWN_RULE = "99-pipeboard-cpu-idle.rules"  # (in RULES)
ASIDE = ".pipeboard-off"  # a rule set aside: udev reads only *.rules; never deleted
STATE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")  # what may go into the root script


def cstates() -> dict:
    """Each idle state (from cpu0: name, wake time in µs, off on every CPU), and the boot rules
    that touch idle states. No root needed."""
    def state(d: Path) -> dict | None:
        try:
            return {"name": (d / "name").read_text().strip(),
                    "us": int((d / "latency").read_text()),
                    "off": (d / "disable").read_text().strip() == "1"}
        except (OSError, ValueError):
            return None
    every = [(d.name, x) for d in sorted(CPUS.glob("cpu[0-9]*/cpuidle/state*")) if (x := state(d))]
    first = [x for d in sorted(CPUS.glob("cpu0/cpuidle/state*")) if (x := state(d))]
    states = [{**x, "off": all(y["off"] for n, y in every if y["name"] == x["name"])}
              for x in first if x["name"] != "POLL" and STATE_NAME.match(x["name"])]
    return {"states": states,
            "rules": sorted(str(r) for r in RULES.glob("*.rules") if "cpuidle" in _read(r)),
            "aside": sorted(str(r) for r in RULES.glob(f"*.rules{ASIDE}"))}


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def check_off(names, known: list[str]) -> list[str]:
    """The idle states to turn off, or ValueError: names this CPU reports, nothing else."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("Which idle states to turn off: a list of their names.")
    if not set(names) <= set(known):
        raise ValueError("This CPU has no idle state of that name.")
    return sorted(set(names))


def own_rule(off: list[str]) -> str:
    """PipeBoard's boot rule: these idle states off on every CPU (names checked by check_off)."""
    return ("# Written by PipeBoard (Audio engine): these CPU idle states stay off.\n"
            'ACTION=="add", SUBSYSTEM=="cpu", KERNEL=="cpu[0-9]*", '
            "RUN+=\"/bin/sh -c 'for d in /sys/devices/system/cpu/%k/cpuidle/state*; do "
            f"case $(cat $d/name) in {'|'.join(off)}) echo 1 > $d/disable;; esac; done'\"\n")


# Low-latency kernel options (owner: "an advanced setting for this"). On Ubuntu 26.04
# linux-lowlatency is the generic kernel (already 1000 Hz, PREEMPT_DYNAMIC) plus these two boot
# options, so PipeBoard offers just them: a GRUB snippet of its own, applied at the next boot.
KERNEL_OPTS = ("preempt=full", "rcu_nocbs=all")
GRUB = Path("/etc/default/grub")  # (and GRUB.parent / "grub.d")
OWN_GRUB = "99-pipeboard-kernel.cfg"  # (in grub.d)
UPDATE_GRUB = "update-grub"  # (tests: a stand-in)
ROOT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"  # (pkexec's PATH)


def kernel() -> dict:
    """Each option: on in the running kernel (now), on at the next boot (next), and the other
    GRUB files that set it (elsewhere: PipeBoard's toggle cannot take it off). No root needed;
    /boot/grub/grub.cfg is root-only, so the next boot is read from GRUB's sources."""
    now = _read(Path("/proc/cmdline")).split()
    files = [GRUB, *sorted(GRUB.parent.glob("grub.d/*.cfg"))]
    said = {str(f): " ".join(ln for ln in _read(f).splitlines() if not ln.lstrip().startswith("#"))
            for f in files}
    ours = said.pop(str(GRUB.parent / "grub.d" / OWN_GRUB), "")
    has = lambda o, t: re.search(rf"(?<![^\s\"']){re.escape(o)}(?![^\s\"'])", t)  # noqa: E731
    return {"opts": [{"name": o, "now": o in now, "ours": bool(has(o, ours)),
                      "elsewhere": [f for f, t in said.items() if has(o, t)]}
                     for o in KERNEL_OPTS],
            "grub": GRUB.exists() and bool(shutil.which(UPDATE_GRUB, path=ROOT_PATH))}


def check_kernel(opts) -> list[str]:
    """The options PipeBoard's snippet should set, or ValueError: only KERNEL_OPTS."""
    if not isinstance(opts, list) or not set(opts) <= set(KERNEL_OPTS):
        raise ValueError("Kernel options: a list of " + ", ".join(KERNEL_OPTS) + ".")
    return [o for o in KERNEL_OPTS if o in opts]


def own_grub(opts: list[str]) -> str:
    """PipeBoard's GRUB snippet: these options added to the default boot (checked by
    check_kernel). GRUB sources it as shell, so its own variable stays unexpanded here."""
    return ("# Written by PipeBoard (Audio engine): low-latency kernel options.\n"
            f'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT {" ".join(opts)}"\n')


def helper_script() -> str:
    """The root helper, fixed text: one line per change on its stdin, "ok" or "bad" back.
    `idle <names>` (space-separated; none = all on): exactly those states off on every CPU,
    every other one (but POLL) on; at boot PipeBoard's own rule for them (none if none), every
    other rule touching idle states set aside (renamed, never deleted).
    `kernel <options>` (of KERNEL_OPTS; none = none): PipeBoard's GRUB snippet with exactly
    those (none if none), then update-grub; if that fails, the snippet as it was and "failed".
    It checks each line itself: only letters, digits and . _ - (and = for kernel options, each
    one of a fixed list) pass, so no line can become a command."""
    rule = RULES / OWN_RULE
    head = "# Written by PipeBoard (Audio engine): these CPU idle states stay off."
    snippet = GRUB.parent / "grub.d" / OWN_GRUB
    khead, kline = own_grub(["X"]).splitlines()
    kline = kline.replace("$", "\\$").replace('"', '\\"').replace(" X", " $opts")
    allowed = "|".join(KERNEL_OPTS)
    return f"""while read -r what rest; do
  case "$what" in
  idle)
    off=$rest
    case "$off" in *[!A-Za-z0-9_.\\ -]*) echo bad; continue;; esac
    for d in {CPUS}/cpu[0-9]*/cpuidle/state*; do
      n=$(cat "$d/name"); [ "$n" = POLL ] && continue
      v=0; for o in $off; do [ "$n" = "$o" ] && v=1; done; echo $v > "$d/disable"
    done
    for r in {RULES}/*.rules; do
      [ "$r" = {rule} ] && continue; grep -qs cpuidle "$r" && mv "$r" "$r{ASIDE}"
    done
    if [ -n "$off" ]; then
      pat=$(echo $off | tr ' ' '|')
      r1='ACTION=="add", SUBSYSTEM=="cpu", KERNEL=="cpu[0-9]*", '
      r2="RUN+=\\"/bin/sh -c 'for d in /sys/devices/system/cpu/%k/cpuidle/state*; do "
      r3="case \\$(cat \\$d/name) in $pat) echo 1 > \\$d/disable;; esac; done'\\""
      printf '%s\\n%s%s%s\\n' '{head}' "$r1" "$r2" "$r3" > {rule}
    else
      rm -f {rule}
    fi
    echo ok;;
  kernel)
    opts=$rest
    case "$opts" in *[!a-z_=\\ ]*) echo bad; continue;; esac
    ok=1; for o in $opts; do case "$o" in {allowed}) ;; *) ok=;; esac; done
    [ -z "$ok" ] && {{ echo bad; continue; }}
    was=$(cat {snippet} 2>/dev/null); mkdir -p {snippet.parent}
    if [ -n "$opts" ]; then
      printf '%s\\n%s\\n' '{khead}' "{kline}" > {snippet}
    else
      rm -f {snippet}
    fi
    if {UPDATE_GRUB} >/dev/null 2>&1; then echo ok; else
      if [ -n "$was" ]; then printf '%s\\n' "$was" > {snippet}; else rm -f {snippet}; fi
      echo failed
    fi;;
  *) echo bad;;
  esac
done
"""


class RootHelper:
    """The root helper (helper_script) for CPU idle states and kernel options, started at the
    first change (one password prompt) and kept while PipeBoard runs (owner: "require the
    password once"); it ends with it (its stdin closes). `command`: how it is run (tests run it
    without root)."""

    def __init__(self, command: tuple = ("pkexec", "/bin/sh", "-c")):
        self.command, self.proc = command, None

    @property
    def running(self) -> bool:
        """Started and not ended: a change asks no password."""
        return bool(self.proc and self.proc.returncode is None)

    async def idle(self, off: list[str]) -> str | None:
        """These states off, the rest on; a complaint, or None."""
        assert all(STATE_NAME.match(n) for n in off)
        return await self._ask("idle " + " ".join(off))

    async def kernel(self, opts: list[str]) -> str | None:
        """PipeBoard's GRUB snippet with these options (none: no snippet); a complaint, or None."""
        assert set(opts) <= set(KERNEL_OPTS)
        return await self._ask("kernel " + " ".join(opts))

    async def _ask(self, line: str) -> str | None:
        if not self.running:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.command, helper_script(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                self.proc = None
                return f"Not changed: {self.command[0]} could not be run (is it installed?)."
        try:
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
            said = (await asyncio.wait_for(self.proc.stdout.readline(), 180)).decode().strip()
        except (TimeoutError, ConnectionError):
            said = ""
        if said == "ok":
            return None
        if said == "failed":  # (the helper stays: the next change needs no password)
            return "Not changed: update-grub failed; the boot options are as they were."
        await self.stop()
        return ("Not changed: the password prompt was closed, or no prompt could be shown."
                if not said else "Not changed: the helper refused the request.")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
        self.proc = None


def reopens(v: dict, was: dict) -> bool:
    """Whether these values need the interface's node made again (WirePlumber restarted): a new
    scheduling or safety margin (in blocks: its headroom and buffer are fixed when the node is
    made), a new rate (the card opens at it), or on interrupts a new block (its ALSA period,
    which must equal the block; `irq_elsewhere`: another interface with a rule of PipeBoard's
    runs on interrupts, its period follows the block too). On PipeWire's timer the period is its
    own: the block alone changes live."""
    blocks = lambda x: (x.get("margin") or 0) // (x.get("block") or 1)  # noqa: E731
    if v.get("tsched") != was.get("tsched") or blocks(v) != blocks(was) \
            or v.get("rate") != was.get("rate"):
        return True
    irq = not v.get("tsched") or v.get("irq_elsewhere") or was.get("irq_elsewhere")
    return bool(irq) and v.get("block") != was.get("block")


async def node_errors() -> dict[str, int]:
    """Every node's dropout (xrun) count so far, by node name: pw-top's ERR column, from its
    second sample (the first can be stale). Empty if pw-top could not say."""
    proc = await asyncio.create_subprocess_exec(
        "pw-top", "-b", "-n", "2", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        out = (await asyncio.wait_for(proc.communicate(), 5))[0].decode()
    except TimeoutError:
        proc.kill()
        return {}
    return pw_top_errors(out)


AUDIO_FORMAT = re.compile(r"[SFU](8|16|24|32|64)")


def pw_top_errors(out: str) -> dict[str, int]:
    """pw-top's ERR column by node name, from its last sample that has data. Now and then a sample
    comes back empty: every node at quantum 0, rate 0 and ERR 0 (no timing from PipeWire in that
    moment). Taken as real, the next good one looked like every node's whole lifetime count of
    dropouts at once (owner: ~1500 at a time, none heard). A sample counts only if an audio node
    in it (an audio format: S32LE, F32P…) has a quantum; a screen-capture stream can still have
    one when the audio has none (seen: kwin_wayland, BGRA). None does: nothing is listed (each
    card keeps its last count)."""
    samples, cur = [], None
    for line in out.splitlines():
        f = line.split()
        if f[:2] == ["S", "ID"]:
            cur = {"errors": {}, "data": False}
            samples.append(cur)
        elif cur is not None and len(f) > 9 and f[8].isdigit():
            cur["errors"][f[-1]] = int(f[8])
            audio = AUDIO_FORMAT.match(f[9])
            cur["data"] = cur["data"] or bool(audio and f[2].isdigit() and int(f[2]) > 0)
    good = [s for s in samples if s["data"]]
    return good[-1]["errors"] if good else {}


async def _back(names: set[str], wait: float = 15.0) -> bool:
    """Wait for the interface's nodes to be in the graph again."""
    for _ in range(int(wait * 4)):
        await asyncio.sleep(0.25)
        proc = await asyncio.create_subprocess_exec(
            "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            dump = json.loads((await proc.communicate())[0])
        except ValueError:
            continue
        if names <= {((o.get("info") or {}).get("props") or {}).get("node.name") for o in dump}:
            return True
    return False


async def sound_back(interface: list[str], cap: float = 20.0) -> bool:
    """Wait until the interface's input carries sound again after it was made again: a device that
    stays quiet while it starts gives exact zeros until then (owner's UR44C: ~13 s after PipeWire
    was ready, the time differs per device); any other device at once. Its own short capture,
    8 kHz mono. False if it has no input, or nothing came within cap seconds."""
    # ponytail: an input that is digital silence even when running (no preamp noise) waits out
    # the cap; the output is assumed to come back with the input (unproven beyond the UR44C).
    source = next((n for n in interface if n.startswith("alsa_input.")), None)
    if not source:
        return False
    proc = await asyncio.create_subprocess_exec(
        "pw-record", "--target", source,
        "-P", "{ node.name=pipeboard_listen node.dont-fallback=true node.dont-reconnect=true }",
        "--format", "f32", "--channels", "1", "--rate", "8000", "--raw", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(cap):
            while True:
                if any(array.array("f", await proc.stdout.readexactly(4096))):  # (-0.0 is 0 too)
                    return True
    except (TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def apply(v: dict, was: dict, dump: list[dict]) -> str | None:
    """Put the values into the running audio, never restarting PipeWire: that drops every app's
    stream, and most apps never reconnect (owner: they went silent, yet kept playing).

    The block and the margin need the interface's node made again: its ALSA period is fixed when
    it is made, the block must equal it (the owner's UR44C runs on interrupts: a block changed
    live ran 512 on a 128 period, and the sound cut out for long stretches), and a headroom set
    live was cut down by the old buffer. So only WirePlumber restarts (it makes the device nodes,
    from the rule file already written; apps are PipeWire's and keep their streams); the block is
    set while the interface is away, so it never runs against the wrong period; the caller makes
    the board's wires to it again. Priority and nice are set on the running PipeWire: chrt -R (its
    audio thread keeps SCHED_RESET_ON_FORK; clearing it is not allowed) and renice, only when
    they were changed: set every time, a block change on a system where RTKit gives PipeWire its
    real time and the user may not (Fedora) ended in "chrt: failed to set pid …'s policy:
    Operation not permitted", a toast for a priority nobody had touched (a friend)."""
    errors = []
    reopen = reopens(v, was)
    if reopen:
        errors.append(await _run("systemctl", "--user", "restart", "wireplumber.service"))
    if v["block"] != was["block"] or reopen:  # (set while the interface is away, if it is)
        keys = ("max-quantum", "quantum", "min-quantum")
        for key in keys if v["block"] > was["block"] else keys[::-1]:
            errors.append(await _run("pw-metadata", "-n", "settings", "0", f"clock.{key}",
                                     str(v["block"])))
    if reopen and not await _back(set(read(dump, v.get("device"))["interface"])):
        errors.append("the interface did not come back within 15 s")
    if v["prio"] != was.get("prio") or v["nice"] != was.get("nice"):
        pid, threads = await _daemon()
        if v["prio"] != was.get("prio"):
            errors += [await _run("chrt", "-f", "-R", "-p", str(v["prio"]), t) for t in threads]
        if pid not in ("", "0") and v["nice"] != was.get("nice"):
            errors.append(await _run("renice", "-n", str(v["nice"]), "-p", pid))
    return next((_plain(e) for e in errors if e), None)


def _plain(error: str) -> str:
    """A tool's complaint, in words when it is about permission to set a priority."""
    if error.startswith(("chrt:", "renice:")) and "not permitted" in error:
        return ("PipeWire's priority was not changed: this account may not set it (here "
                "PipeWire gets real time through RTKit).")
    return error
