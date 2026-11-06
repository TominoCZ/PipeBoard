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
from collections import Counter
from pathlib import Path

from pipeboard import graph, headroom

BLOCKS = (64, 128, 256, 512, 1024, 2048)
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


def read(dump: list[dict]) -> dict:
    """What runs now: the rate and block, the interface clocking the graph (its ALSA nodes, its
    period and safety margin), and the priority PipeBoard's file sets (else PipeWire's own)."""
    nodes = {o["id"]: (o.get("info") or {}).get("props") or {} for o in dump
             if o["type"].endswith("Node")}
    names = {o["id"]: ((o.get("info") or {}).get("props") or {}).get("device.description")
             for o in dump if o["type"].endswith("Device")}
    driver, clock = _driver(nodes), graph._clock(dump)
    iface = [p for p in nodes.values() if driver and p.get("device.api") == "alsa"
             and p.get("device.id") == driver.get("device.id")
             and p.get("media.class", "").startswith("Audio/")]
    # The margin that runs: the headroom in PipeWire's latency figures for the interface's ports
    # (block + headroom). A live change shows there once the device reopened; its creation
    # properties keep the value it was made with.
    ids = {int(p["object.id"]) for p in iface if p.get("object.id") is not None}
    ran = [lat.get("maxRate", 0) for o in dump if o["type"].endswith("Port")
           and ((o.get("info") or {}).get("props") or {}).get("node.id") in ids
           for lat in ((o.get("info") or {}).get("params") or {}).get("Latency") or []
           if lat.get("maxQuantum")]
    own = files()["pipewire"]
    text = own.read_text() if own.is_file() else ""
    def found(key: str, dflt: int) -> int:
        m = re.search(rf"{key}\s*=\s*(-?\d+)", text)
        return int(m[1]) if m else dflt
    return {"rate": clock["rate"], "block": clock["quantum"],
            "interface": sorted(p["node.name"] for p in iface),
            "pcms": headroom.folders(iface),  # (its buffers' status, for Buffer headroom)
            "title": (names.get(driver.get("device.id")) or driver.get("node.description"))
            if driver else None,
            "period": max((int(p.get("api.alsa.period-size") or 0) for p in iface), default=0),
            "margin": max(ran) if ran else max((int(p.get("api.alsa.headroom") or 0)
                                                 for p in iface), default=0),
            "tsched": _timer(dump, ids),
            "margin_blocks": _margin_blocks(),
            "prio": found("rt.prio", DEFAULT_PRIO), "nice": found("nice.level", DEFAULT_NICE),
            "own": own.is_file()}


def _margin_blocks() -> int:
    """The safety margin, in blocks, PipeBoard's rule gives the interface (1 without one)."""
    path = files()["wireplumber"]
    text = path.read_text() if path.is_file() else ""
    size, head = (re.search(rf"api\.alsa\.{k}\s*=\s*(\d+)", text)
                  for k in ("period-size", "headroom"))
    found = int(head[1]) // int(size[1]) if size and head and int(size[1]) else 1
    return found if found in MARGIN_BLOCKS else 1


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


def check(op: dict) -> dict:
    """A request's values, or ValueError: only the steps the panel offers, in the allowed range."""
    try:
        v = {k: int(op[k]) for k in ("block", "margin", "prio", "nice")}
        v["tsched"] = bool(op.get("tsched", False))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("The audio engine needs a block size, a margin and priorities.") from e
    if v["block"] not in BLOCKS:
        raise ValueError("That block size is not one PipeBoard offers.")
    if v["margin"] % v["block"] or v["margin"] // v["block"] not in MARGIN_BLOCKS:
        raise ValueError("The safety margin is 1, 2 or 3 blocks.")
    if not (PRIO[0] <= v["prio"] <= PRIO[1] and NICE[0] <= v["nice"] <= NICE[1]):
        raise ValueError(f"Priority is {PRIO[0]}…{PRIO[1]} and nice {NICE[0]}…{NICE[1]}.")
    return v


def texts(v: dict, interface: list[str]) -> dict[str, str]:
    """Both files' text for these values (the interface: its ALSA node names)."""
    pw = (f"{HEAD}context.properties = {{\n"
          f"    default.clock.quantum     = {v['block']}\n"
          f"    default.clock.min-quantum = {v['block']}\n"
          f"    default.clock.max-quantum = {v['block']}\n"
          f"    module.rt.args = {{ rt.prio = {v['prio']} nice.level = {v['nice']} }}\n}}\n")
    matches = " ".join(f"{{ node.name = {json.dumps(n)} }}" for n in interface)
    # On interrupts the buffer holds twice the block and margin (half of it is all PipeWire fills).
    periods = 2 * (v["margin"] // v["block"] + 1)
    buffer = "" if v.get("tsched") else f"api.alsa.period-num = {periods} "
    wp = (f"{HEAD}monitor.alsa.rules = [\n  {{\n    matches = [ {matches} ]\n"
          f"    actions = {{ update-props = {{ api.alsa.period-size = {v['block']} "
          f"api.alsa.headroom = {v['margin']} {buffer}"
          f"api.alsa.disable-tsched = {str(not v.get('tsched', False)).lower()} }} }}\n  }}\n]\n")
    return {"pipewire": pw, "wireplumber": wp if interface else None}


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


def helper_script() -> str:
    """The root helper, fixed text: one line per change on its stdin (the names of the states to
    turn off, space-separated; empty = all on), "ok" or "bad" back. Exactly those states off on
    every CPU, every other one (but POLL) on; at boot PipeBoard's own rule for them (none if
    none), every other rule touching idle states set aside (renamed, never deleted). It checks
    each line itself: only letters, digits and . _ - pass, so no line can become a command."""
    rule = RULES / OWN_RULE
    head = "# Written by PipeBoard (Audio engine): these CPU idle states stay off."
    return f"""while IFS= read -r off; do
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
    printf '%s\n%s%s%s\n' '{head}' "$r1" "$r2" "$r3" > {rule}
  else
    rm -f {rule}
  fi
  echo ok
done
"""


class IdleHelper:
    """The root helper for CPU idle states, started at the first change (one password prompt)
    and kept while PipeBoard runs (owner: "require the password once"); it ends with it (its
    stdin closes). `command`: how it is run (tests run it without root)."""

    def __init__(self, command: tuple = ("pkexec", "/bin/sh", "-c")):
        self.command, self.proc = command, None

    async def set(self, off: list[str]) -> str | None:
        """These states off, the rest on; a complaint, or None."""
        assert all(STATE_NAME.match(n) for n in off)
        if not self.proc or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, helper_script(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            self.proc.stdin.write((" ".join(off) + "\n").encode())
            await self.proc.stdin.drain()
            said = (await asyncio.wait_for(self.proc.stdout.readline(), 180)).decode().strip()
        except (TimeoutError, ConnectionError):
            said = ""
        if said == "ok":
            return None
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
    made), or on interrupts a new block (its ALSA period, which must equal the block). On
    PipeWire's timer the period is its own: the block alone changes live."""
    blocks = lambda x: (x.get("margin") or 0) // (x.get("block") or 1)  # noqa: E731
    if v.get("tsched") != was.get("tsched") or blocks(v) != blocks(was):
        return True
    return not v.get("tsched") and v.get("block") != was.get("block")


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


async def dropouts(names: list[str]) -> int | None:
    """The interface's dropout count so far, summed over its nodes; None if pw-top could not say."""
    found = [n for name, n in (await node_errors()).items() if name in names]
    return sum(found) if found else None


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
    audio thread keeps SCHED_RESET_ON_FORK; clearing it is not allowed) and renice."""
    errors = []
    reopen = reopens(v, was)
    if reopen:
        errors.append(await _run("systemctl", "--user", "restart", "wireplumber.service"))
    if v["block"] != was["block"] or reopen:  # (set while the interface is away, if it is)
        keys = ("max-quantum", "quantum", "min-quantum")
        for key in keys if v["block"] > was["block"] else keys[::-1]:
            errors.append(await _run("pw-metadata", "-n", "settings", "0", f"clock.{key}",
                                     str(v["block"])))
    if reopen and not await _back(set(read(dump)["interface"])):
        errors.append("the interface did not come back within 15 s")
    pid, threads = await _daemon()
    errors += [await _run("chrt", "-f", "-R", "-p", str(v["prio"]), t) for t in threads]
    if pid not in ("", "0"):
        errors.append(await _run("renice", "-n", str(v["nice"]), "-p", pid))
    return next((e for e in errors if e), None)
