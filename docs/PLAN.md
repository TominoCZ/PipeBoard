# PLAN.md — the design

The design of record: how PipeBoard works and why. Change it when a decision changes.

---

## 0. Two-minute orientation

PipeBoard is a small Linux app for re-routing audio on a PipeWire desktop. The user's audio
interface is a Studio Interface (Pro mode, 192 kHz). On top of it sit virtual devices: **Voice
Mix**, **Instrument Mix**, **Mic Instrument Mix**, **Stream Mix**, **Input1 Mix** (mics
apps record from) and **Desktop Mix**, **Music Mix** (speakers apps play into). Today these are
`pw-loopback` processes started by `a setup script`; changing the routing
means editing that script. The user also has a **Guitar Pedal** pedal that is a USB audio
device in its own right, and wants to use it as an effect in the chain.

PipeBoard shows the graph as a **corkboard of cards joined by curved wires**, laid out by signal
flow, inputs on the left of each card and outputs on the right (like Unity's Shader Graph or
Unreal's Blueprints):

```
 Music Player ─┐                                       ┌─ Studio Interface ─┐
          ├─▶ Desktop Mix ─▶ Easy Effects ─▶ IN 1 ─┤                   ├─ OUT 2 ─▶ Mic Mix
Browser ┘                               IN 2 ─┤                   ├─ OUT 3 ─▶ Pedal ─▶ Instrument Mix
                                                └───────────────────┘
```

Drag from socket to socket to connect (from an input that already has a wire, the drag moves that
wire to another input; Shift makes a new one); click a wire (it stays lit) and press Delete (or its
✕ chip) to disconnect; drag cards; double-click a title to rename; right-click for more. A problem center
(top right) lists what is wrong with the graph, worst first.

**Stack.** Python 3.12+ (uv) backend, one HTML/CSS/JS page with no build step, shown in a Chrome
`--app` window, the same stack as Jarvis. The backend drives PipeWire through its own tools
(`pw-dump`, `pw-link`, `pw-cli`), so there are no compiled bindings.

```
PipeWire ──pw-dump --monitor──▶ server.py ──graph.py, problems.py──▶ state ──WebSocket──▶ board.js
   ▲                                                                                       │
   └──────────── pw-link / pw-cli / child processes ◀── actions (connect, rename, …) ◀─────┘
```

## 1. Goals and non-goals

**Goals**

1. Re-routing the user's mixes takes seconds and needs no script editing.
2. **Calm to look at, spotless to use.** A card shows what it is, its sockets and nothing else.
   No raw port names, no node ids. Details appear on hover. Every action is obvious, reversible
   where it can be, and confirmed where it cannot.
3. Processing in the chain: an external USB pedal (Pedal) as an effect loop, a gain node, LV2
   and VST3 plugins.
4. Create virtual speakers and mics; rename them for real; show or hide any device.
5. Profiles: the whole board, saved, loaded, and optionally loaded at launch.
6. A problem center that catches real mistakes (feedback, one-sided stereo, doubled signal,
   failed links, missing devices) and stays quiet about free sockets.

**Non-goals**

- A DAW. Meters may come later, only if they stay calm.
- Renaming hardware in PipeWire: a hardware card's name is a nickname PipeBoard alone shows.
- Windows or macOS. The page is portable, the backend is PipeWire-only.
- Replacing WirePlumber's policy. PipeBoard makes and removes links, it does not police them.

## 2. The board model (`pipeboard/graph.py`)

A pure function from a `pw-dump` snapshot to what the page draws:

```json
{"cards": [{"key": "mic_mix", "title": "Mic Mix", "kind": "virtual", "role": "mic",
            "inputs":  [{"id": "in",  "label": "IN",  "ports": [151, 152], "names": ["input_FL", "input_FR"]}],
            "outputs": [{"id": "out", "label": "OUT", "ports": [155, 156], "names": ["capture_FL", "capture_FR"]}],
            "hidden": false, "owned": false, "rate": null, "clock": null, "detail": "mic_mix + mic_mix_capture"}],
 "wires": [{"id": "device:alsa_card.usb-…Studio Interface-00:out2>mic_mix:in", "from": ["device:…", "out2"],
            "to": ["mic_mix", "in"], "links": [249, 250], "fromPorts": [0], "toPorts": [0, 1],
            "partial": false, "failed": false, "ch": "", "chEnd": ""}],
 "clock": {"rate": 192000, "quantum": 1024}}
```

### 2.1 Cards and their keys

- **One card per thing the user thinks of as one device:**
  - a `pw-loopback` pair (both halves share `node.link-group`): *Mic Mix* is one card with IN
    on the left and OUT on the right;
  - a **whole hardware device** (its nodes share `device.id`): the Studio Interface's playback and capture
    are one card, its outputs on the left and its inputs on the right. So is the Pedal, which
    makes "Studio Interface OUT 3 → Pedal → Instrument Mix" an effect loop you can see;
  - EasyEffects' sink, its ~10 internal `ee_soe_*` filters and its test-tone generator
    (`ee_test_signals`): one *Easy Effects* card showing only its outside connections (hiding them
    would cut the speaker path in two);
  - every other audio node, one card each.
- **Keys are identifiers, never display names** (the user's requirement): they are what positions,
  nicknames, wires and profiles hang on.

  | Card | Key |
  |---|---|
  | hardware | `device:` + the Device's `device.name` (bus, vendor, product, serial) |
  | PipeBoard's own devices (M6) | `pb:` + its permanent `pipeboard.id` |
  | Easy Effects | `easyeffects:out`, `easyeffects:in` |
  | everything else | its `node.name` (`mic_mix`, `music_player`), `#2`… when two share one |

- **Kinds** set the outline colour: `hardware` amber, `virtual` teal, `app` violet, `effect`
  rose, `plugin` green (§4).
- **Never on the board:** level-meter streams (KDE's volume applet and pavucontrol attach one to
  every device while open; the Pulse side marks them `stream.monitor` / `resample.peaks`) and live
  wires' own capture. **One card per app stream:** streams are never merged, even with the same
  or no name (keys `Discord`, `Discord#2`; the app's name when a stream has no `node.name`).
- **Hidden by default:** MIDI, video, drivers, nodes with no audio ports, stray `ee_*` nodes.
  Hidden cards are listed in the Add menu under "Hidden devices".

### 2.2 Sockets and labels

The user asked for labels like `IN_1_L` instead of `capture_FL` / `playback_AUX3`. Per side:

| Ports on that side | Sockets | Labels |
|---|---|---|
| 1 | 1 | `IN` / `OUT` |
| 2 | **1 stereo socket** (a wider pill) | `IN` / `OUT` |
| 4, 6, 8 in FL/FR pairs | stereo sockets | `IN 1`, `IN 2`… |
| anything else (a pro-audio interface) | one per channel | `IN 1`…`IN 6`, `OUT 1`…`OUT 8` |

Labels follow graph direction (left IN, right OUT), so the Studio Interface's mic jack 2 is `OUT 2` on its
right side: sound comes *out of the card* there. The number matches the front panel and the
user's script (`1→FL, 2→FR, 3→RL, 4→RR`); the real port name is in the tooltip. (User approved.) Monitors are hidden unless used, except on `Audio/Source` nodes, where a
null-sink virtual mic's only outputs are flagged as monitors.

Tidy (and a new board's first layout) puts cards in columns by how far along the signal they are;
a second pass then moves a card that is the only one of its category (colour) in its column into a
neighbouring column with its own kind, below them, when the flow still reads left to right (owner:
a speaker mix stacks under the other mixes, not among the apps). The right side mirrors it: as
apps that only send sit in the first column, a card that only receives (a mix apps record from)
stacks with its own kind in the last column; only it moves, what feeds it stays (owner: the
guitar mix beside the voice mixes, not among the effects). Rows and columns are whole grid steps
apart (28 and 126 px) from a start on the grid, so a column's rows are evenly spaced (owner); the
Tidy shot fails if they are not.

**Portals** (owner: cleaner wiring, a board that is not only ever wider). Board-only: the sound
still goes straight from A to B, nothing new in PipeWire. A portal wire is drawn as a short S
from its output into a badge and from a badge into its input; the whole wire shows while a badge,
either card or the wire is hovered, or it is selected; its live wave only then. The badge (owner's
pick of five drawn): a tunnel, a dark hole with rings fading in and a lit rim in the source's
colour, the pair's letter (A, B… by wire id), and an arrowhead on its edge: pointing away on the
right where the wire leaves, pointing in on the left where it arrives (owner). Badges sit on their
own layer above every wire, 52 px off the socket (owner: wires covered them). All badges are
placed in one pass (owner: they overlapped, and one moved when another was toggled): each wants
its spot (Tidy's, or off its socket at the socket's own height), then, top down in a fixed order,
any nearer than 30 px to one already placed is pushed below it; the portal-toggle shot checks that
toggling one moves none and that none overlap. Made by hand (wire
menu › Make portal / Show as a wire) or by Tidy, which folds the columns into rows when one row is
far wider than the window: candidates scored by how near their shape is to the window's plus
0.04 per wire between rows; those wires become portals, their badges in one column past the row's
right edge and one before the next row's left (owner), but beside the socket for a card not at a
row's edge (a column there drew a stub across the row). A Tidy's portals are redone each Tidy; the
user's stay. Saved on the board (`portals`: wire id → "hand" or {how: "tidy", out, in}) and in
profiles. The bar's Tidy is a split button, **[ Tidy | ▾ ]**: its menu holds Portals, with a line
saying what it does (owner's pick W2). Off, Tidy keeps one row and drops its own portals (the
user's stay); saved on the board (`tidy_portals`, off unless turned on: owner). Toggling it
tidies again (owner: turned off, Tidy's portals stayed). A card dragged away takes its Tidy
badges with it: that end goes beside its socket (owner: they stayed behind).
**A column far taller than the rest** (more than 1.4× the tallest other one) wraps: its cards that
only receive go into extra columns right after it, none taller than the tallest other column
(owner: five diagnostic cards fed from one EQ stacked into one column, and folding could not help
while that column set the height; a fold rule by fit size was tried and dropped: it flipped on 87
px of window height).
Tidy is worked out in full, shown in one go and sent as one `tidy` message (places and portals):
owner saw cards jitter, as Tidy sent its portals and its places separately and the server's state
after the first still held the old places (each state puts the cards at their saved places).
Until the board shows the new places saved (or 3 s), a state that disagrees does not move them;
the tidy-echo shot replays a stale state after a Tidy and fails if a card moves. Card sizes for
the layout are each card's own layout size (whole CSS pixels at any zoom), socket spots rounded to
a quarter pixel: read off the screen and divided by the zoom they carried float noise that changed
with it, and a column centred exactly half a grid step off rounded one way, then, after Tidy's Fit
changed the zoom, the other (owner: a card moved back and forth between clicks; reproduced with
real clicks on a fixture server of their board, Mic EQ ±14 px). Since then positions and sizes are
whole cells (see Grid), Tidy works in cells and centres the board on cell [0, 0], and the
tidy-any-zoom shot lays it out at six zooms and fails on any difference. Which rows read best is still open (owner: "something we gotta delve deep into").

A card that appears (an app starts playing, a device is plugged in) goes to its place in a fresh
layout if that is free, else to the nearest free spot (its own column first), and stays there: it
never lands on a card moved there since (owner).

### 2.3 Wires

One wire per socket pair, however many port links it stands for. A wire is drawn from an output; an
input with no wire starts nothing (owner's design choice; no drag cursor there), an input with one
picks that wire up to move it (Shift: a new one from it). Connecting pairs ports by index
(L→L, R→R); mono into stereo fans out to both. Stereo into mono takes **one channel per wire, in
order** (user): L, unless L already goes somewhere on its own and R does not, then R — so two
drags route L and R to two mono inputs. While a wire from a stereo output is held, a golden tag
follows the pointer with the channel it carries: L or R, or "L R" over a stereo input (user:
"so that i know what channel im holding"); from a mono input it shows over a stereo output
(`graph.next_side`, mirrored in the page). A new mono device made from a dropped wire
takes a side the same way. Disconnecting removes every link the wire stands for. A wire that carries one side of a
stereo socket gets a small **L** or **R** tag at that end and meets the socket a few pixels apart
from its partner; a stereo-to-stereo wire with one link missing is dashed.

## 3. Backend (`pipeboard/`)

| Module | Job |
|---|---|
| `__main__.py` | `pipeboard`: serve and open the window; `--fixture [machine\|pedal\|problems]` serves a saved graph and never touches PipeWire; `--no-window` |
| `graph.py` | pw-dump → board model; `pair()` for connecting sockets (pure, tested) |
| `problems.py` | board model → problems, worst first (pure, tested; §5a) |
| `pipewire.py` | the only module that runs PipeWire tools (`Live`); `Fixture` keeps a graph in memory and applies links to it |
| `server.py` | aiohttp: the page, `/ws` (state out, actions in), Origin guard |
| `workspace.py` | the board file and `Store` (board, settings, profiles folder); nickname history; atomic writes |
| `profiles.py` | snapshot, missing, plan, changes, retarget (pure, tested; §7) |
| `devices.py` | PipeBoard's own virtual devices and gains: definitions, names, PipeWire properties (§5.1, §6) |
| `gain.py` | gain levels: ramped volume changes through one persistent pw-cli (§5.1) |
| `tray.py`, `autostart.py`, `takeover.py` | the tray icon, launch at login, taking over a service's devices (§8) |
| `scope.py` | live wires: the async capture, per-ms peaks, binary frames; FakeScope for fixtures (§8a) |
| `eq.py`, `web/eq.js` | the EQ node: bands, coefficients, its filter-chain process, the spectrum capture; the curve editor (§5.3) |
| `denoise.py` | the Noise Suppression node's plugin (`native/pb_denoise.c`, Speex), built on demand (§5.8) |
| `dynamics.py`, `web/dynamics.js` | the Limiter, Compressor, Noise Gate and De-Esser nodes: LSP's plugins in a filter-chain, their settings, the in/out peaks; their live panel (§5.5–5.7a) |
| `plugins.py`, `plughost.py` | plugin nodes: the installed list, one Carla host process per plugin; FakePlugins for fixtures (§5.2) |
| `headroom.py` | Buffer headroom: how close the interface's ALSA buffers come to running out, sampled while the Audio engine panel is open (§8c) |
| `moment.py` | what the machine was doing when a dropout was logged: CPU (all, busiest core), GPU and USB interrupts against the last minute's usual, the kernel's and PipeWire's log lines; added to each `dropouts:` log line. Never polls the GPU driver |

**Watching.** `pw-dump --monitor` is only a "something changed" trigger. 150 ms after the first
change the server takes one full `pw-dump`, rebuilds the model and pushes it only if it differs.
A dump of this machine is ~0.5 MB and takes milliseconds; simpler and harder to get wrong than
applying incremental updates. Only a change to what the board is made of counts (a node, port,
link, device or metadata; `pipewire.matters`): a client connecting does not. PipeBoard's own
`pw-dump` and `pw-top` are clients, and before this each read caused the next, ~13 full reads a
second around the clock (2026-11-06, found chasing the owner's report of load and clicks).

**Acting.** `pw-link <out-port-id> <in-port-id>`, `pw-link -d <link-id>`. Links made this way
outlive the tool but **not a PipeWire restart**: M9 re-makes them.

**Safety rules.**
1. Never kill or alter a process or device PipeBoard did not create.
2. **Removing a card from the board never touches the system.** A hard delete exists only for
   PipeBoard's own devices, behind a confirmation that names what will break (§6).
3. PipeBoard's creations carry `pipeboard.owned = true` and `pipeboard.id` in their props.
4. `a setup script` does `pkill -f pw-loopback`, so PipeBoard's loopbacks must not have
   `pw-loopback` in their command line: run them as `pipewire -c <generated conf>` instead.
5. The WebSocket only accepts its own page's Origin; it listens on 127.0.0.1 only.
7. **One PipeBoard per board.** A `flock` on `$XDG_RUNTIME_DIR/pipeboard-<hash of the config
   folder>.lock`, held for the process's life (the kernel drops it on any exit, so it is never
   stale). A second launch shows a desktop dialog (kdialog, else zenity, else notify-send) saying
   PipeBoard is already running, and exits at once. A port taken by another program gets a
   dialog too instead of a crash. Fixtures and test boards have their own folders, so their own
   locks.
6. The board file is written atomically; a corrupt one is set aside as `*.broken.json`, never
   overwritten.

## 4. The look

The user asked for: dark gray, almost a corkboard; darker or lighter gray cards with a coloured
outline; nice to look at; not overwhelming; smooth.

- **Board:** `#232323`, faint warm grain (SVG noise), a sparse dot grid that pans and zooms with
  the cards, a soft vignette.
- **Cards:** `#2e2e2e` body, `#272727` title strip, 1.5 px outline in the kind's colour, 10 px
  radius, soft shadow, a glowing pin dot, a small role icon (mic, speaker, interface, app,
  effect). Width fits the title (150–260 px).
- **Kind colours:** hardware `#e0a050` · virtual `#4fc1b0` · app `#a68cf0` · effect `#e57a9a` ·
  plugin `#8ccf6a`.
- **Sockets:** centred on the card's edge; a stereo socket (L and R together) is a pill, a mono one
  a circle; free ones are filled dark grey, wired ones in the kind's colour. The card's outline is
  its own layer (`::before`) under the sockets, so it never shows through a socket.
- **Wires:** cubic béziers with horizontal tangents, 2.5 px, a gradient from the source card's
  colour to the destination's, a dark shadow under it. Hovering a card or wire fades every other
  wire. Selected: white, with a ✕ Disconnect chip at its middle. Failed: red dotted. In a problem:
  breathing.
- **Bar** (top left): name, connection status, graph rate · buffer · latency, + Add, Fit, Tidy.
  **Problem button** (top right): "All good" / "2 problems" / "1 note", coloured by the worst.
- **Tooltips** (user: the plain ones looked monotone): the name (bright, semibold), a subtitle with
  a dot in the card's colour, a two-column list of facts (rate, real name, made by PipeBoard,
  PipeWire names in small monospace, one line, cut with an ellipsis), a warning line in amber when
  something is wrong, and a quiet hint under a hairline; the card's colour as a thin edge on the
  left. A wire's tooltip shows both ends with their colours, the ports, and "left only" / "right
  only". Plain tooltips (buttons, rows) keep plain text in the same frame.
- **Text:** Inter/Cantarell/system sans; title 13 px semibold, sockets 11 px at 60 % white.

**Menus** cascade like the desktop's own right-click menu (user's request, after
KDE's): an icon column, the label, a hint or a chevron; a submenu opens beside its row on hover
(140 ms, so a mouse cutting across rows does not flicker) or on click, keeps opening the way the
cascade already turned, and flips left at the screen edge. Arrow keys, Enter and Esc work.

- **Right-click the board:** Add node ▸ (Effects ▸ Gain, LV2 plugin…, VST3 plugin… ·
  Devices ▸ Virtual ▸ New microphone…, New speaker… / Physical ▸ · Apps ▸), Fit to window,
  Tidy layout. Existing devices that are not on the board are listed under "Not on the board" in
  their category; picking one puts it where you right-clicked. "+ Add" in the bar opens the same
  Add node menu.
- **Right-click a card:** Rename, Names ▸ (current, original, earlier names with ✕), Remove from
  board (the device stays). M5/M6 add Use another device…, Create this device, Delete device….
- **Let a new wire go on empty board** (from an output, or from a free input): an *Add and connect*
  menu opens there: a new gain or virtual device is made at that spot and connected to the socket
  the wire came from as soon as it appears; a removed device with a socket on the right side comes
  back connected. (A wire being *moved* goes back where it was instead.)
- **A wire:** selected, its chip offers **＋ Insert** and **✕ Disconnect**; right-click gives Insert
  node › and Disconnect. Inserting turns A → B into A → new → B, each channel on its own path (a
  left-only wire stays left-only); the old link is removed only once the new path is made, so the
  sound does not drop.

**Landing on another card.** A card appearing for the first time on top of another is placed
again: an app (back at an old spot) at a fresh layout's spot, by its wires with the other
sources; a card the user put somewhere (a device made from a dropped wire, one shown again from
a menu) kept there, only moved to the nearest free spot (owner: a node made from a dropped wire
landed far away, at a layout spot; shot drop-over).

**Grid.** Positions are whole **cells** (a cell is 14 px; the dot grid is every 28 px, so corners
land on a dot or halfway), and card sizes whole cells too (widths rounded up when drawn; the page
audit fails a card off the grid). Owner: zoom only changes how the board is drawn, and Tidy cannot
drift on float error. Cell [0, 0] is the window's centre on a new board, and Tidy centres the
board on it. Saved boards and profiles hold cells, marked `"grid": "cells"`; a file without the
mark is from before and holds pixels, converted once on reading (`workspace.in_cells`). There is
no free placement (Alt used to give one). While dragging, the card follows the mouse and a dashed
outline in its colour shows the cell it will drop into (user's request); on release it lands
there. No slide animation: wires would lag behind the card.

**Layout.** Columns by signal flow: longest path from the sources after dropping the edges that
close a loop (DFS back edges). On the user's graph that gives apps → speaker mixes → Easy Effects
→ Studio Interface → mic mixes, with no wire running backwards. Within a column, cards sit near what feeds
them. Tidy re-runs it; dragged positions are saved.

The board is a flat near-black (#121212) with a sparse dot grid and a faint grain (owner's pick of six,
rendered on their board).
Device cards, virtual and physical, have a thicker outline (2.5 px) and a glow in their colour so
they stand out from apps and effects (owner's pick of six options rendered on their board).

## 5. Problems (`pipeboard/problems.py`)

The user asked for a problem center with severities, and said free sockets are not a problem.

| Severity | Problem | When |
|---|---|---|
| 🔴 error | **Feedback loop** | a cycle through cards that pass sound on (not hardware jacks, not apps) |
| 🔴 error | **Connection failed** | a link in PipeWire's `error` state |
| 🟠 warning | **Only one side of a stereo input** | a stereo input fed on L or R only |
| 🟠 warning | **Same sound arrives twice** | one source reaches one input socket by two routes |
| 🟠 warning | **Device missing** (M5) | a profile's device is not on the system |
| 🔵 info | **Converted to another sample rate** | a device runs at another rate than the graph |
| 🔵 info | **Two clocks in one chain** | sound passes from one hardware clock to another |
| 🔵 info | **Two devices with one name** | two virtual devices share a title |

A card in an error or warning gets a small **tag centred on its top edge** (user's pick of three): the severity dot and one
word (Feedback, Failed, One side, Doubled, Clipping; "+1" when there are more), the full titles in
its tooltip. Above, not inside: a problem must never resize the card (user). Missing
devices keep their ! corner badge instead. "Show" in the panel flashes the cards, selects the wire
and pans to them.

**Sample rate and buffer size.** PipeWire runs the whole graph at one rate and one quantum
(here 192 kHz, 1024 = 5.3 ms, shown in the bar). A device that cannot run at that rate is
resampled automatically; two devices with separate clocks (Studio Interface and Pedal) are kept in step by
adaptive resampling. Both work and cost a little CPU and a few ms, which is why they are notes,
not warnings. Changing the rate or quantum from the app is open.

## 5a. Processing nodes

### 5.1 Gain (M7, built)

Add node › Effects › Gain: a card with IN, OUT and a **dB box** (user: a box, not a slider). The
level reaches the sound only when confirmed with Enter; Esc or clicking away cancels (owner, 2026-11-04;
before, leaving the box applied). ↑/↓ and the wheel step the number by 0.5 dB (Shift 0.1), without
applying; double-click just selects the text. **Every number box** works this way (`web/field.js`):
the cards' boxes and the EQ's and dynamics panels' strips; frequency steps by a semitone, Q and
times by 15 % (Shift 3 %).
Levels run from **−144 dB** (silence in practice, the floor of 24-bit sound; user's pick over
−inf) to **+24 dB** (volume 15.85; PipeWire applies it, checked), one decimal at most (shown `−6`,
`+3.5`, `−144`). Typed values accept `−6`, `-6`, `+3.5`, `3,5`; `-inf` is −144; out of range is
clamped on confirming (the box flashes); anything else is refused. Ramps make 0.1 dB steps (at most
200) above −40 dB and move straight to anything quieter, where a step is too small to hear. While the box has the focus, board updates wait, so a
redraw cannot replace it mid-edit (the same holds while a card is dragged). It is built like M6's devices, not as a process: a
lingering null sink of class `Audio/Sink/Internal` (not listed to apps) with
`monitor.channel-volumes`, so **its volume is the gain** on its monitor outputs. Measured exact
(a tone at 0.5 → −6.0 dB, 2.0 → +6.0 dB; live: −6 dB → 0.5012). It survives PipeBoard and costs no
CPU of its own. Live wires show the result on the wire leaving it.

Changes are **ramped by PipeBoard** in 0.1 dB steps every 2 ms through one persistent `pw-cli`
(`gain.py`): PipeWire's own volume ramp is ignored for this node (tested: with a scale enum the
whole command was rejected, without it accepted and ignored). A hard 0.5 → 2.0 jump made a 0.24
sample step; the ramp brings it to 0.06, against 0.03 for the tone itself. So a large, fast jump
can leave a faint tick; normal drags are small steps. If that ever matters, an LV2 gain with
built-in smoothing through 5.2's filter-chain is the upgrade. The level is saved on the board (and
in profiles) half a second after it is confirmed; a gain made again starts at its level at once.

### 5.2 Plugins (M8, built)

Add node › Effects › **Plugin…** opens a picker: type to search by name or maker, ↑/↓, Enter (or
double-click). It lists the installed **Linux** effects (audio in and out): LV2 from Carla's own
cache (~1 s for the 314 here) and VST3 bundles in `~/.vst3`, `/usr/local/lib/vst3`,
`/usr/lib/vst3`, discovered once and cached per bundle in `~/.cache/pipeboard/vst3.json` (LSP's
bundle of 194 takes ~50 s the first time, in the background from launch); a VST3 that is the same
plugin as an LV2 (LSP, ZAM) is left out. Windows plugins are not supported (user: Linux only;
they would need Carla's Wine bridge).

**The card** has IN and OUT, and two buttons (user: the plugin's own window has the controls):
- **Bypass** (amber while on): the plugin's own switch when it has one (a control named Bypass,
  or LSP's Enabled, which fades), else Carla's dry/wet at fully dry for an LV2 with as many outputs
  as inputs, else no button. Never dry/wet on a VST3: it crashes Carla 2.5.10 on a bridged VST3.
  Flipping the switch in the plugin's window updates the card.
- **Editor**: opens or closes the plugin's own window (pressed while it is open).

The right-click menu has the same, plus Rename (PipeBoard's name only: apps never see a plugin)
and Remove… (stops it, after asking). Values moved in the plugin's window and Bypass are saved on
the board and in profiles, and set again when it starts; "Changed the settings of …" shows in the
profile's changes. A plugin's other state (an internal preset not exposed as a control) is not
saved: ponytail ceiling; Carla's state files are the upgrade path.

**How it runs** (`plughost.py`, `plugins.py`): one host process per plugin,
`pw-jack python -m pipeboard.plughost <spec>`, Carla's Python backend (stdlib ctypes) with the
JACK driver, one JACK client per plugin, and the plugin itself in Carla's bridge process
(Hum Remover crashes when loaded in-process; a crashing plugin ends only its own card).
PIPEWIRE_PROPS tags the plugin's node and Carla's empty engine node with `pipeboard.owned`/`id`;
the graph groups them into one card `pb:<id>`, titled with the board's name. JSON lines on
stdin/stdout. Carla never loads into PipeBoard itself; listing runs in a child process too.
Stopping: PipeBoard closes the host's stdin (it closes the bridge and exits), then SIGTERM, then
SIGKILL, 3 s apart. If PipeBoard dies, the kernel sends SIGTERM (PR_SET_PDEATHSIG); the host
sets its handler after Carla's engine starts (Carla installs one of its own that left the bridge
running). Plugins, unlike devices and gains, end with PipeBoard: launch starts the board's again;
a profile load starts its missing ones and sets the running ones' values. A host that ends by
itself shows a toast, and its card turns missing ("Start this plugin" in its menu).

### 5.4 Quitting: the sound goes around (M12, built)

The EQ and plugins are processes that end with PipeBoard (devices and gains linger in PipeWire).
Quitting used to cut the chain they sit in: silence (user). Now, like Easy Effects
turned off (user's choice, over keeping them running): on quit, before they stop, every link into
an EQ/plugin is followed through it (input k → output k) and through any others after it, and its
source linked straight to where the sound went (`graph.around`); the board notes these links
(`around` in workspace.json). At the next launch each EQ/plugin starts again, its own wires are
made, and only then the links around it go (as for a node inserted into a wire: no gap). A crash or
`kill -9` cannot route around (accepted). Live: the tone kept reaching the recorder at full level
right after quit; relaunched, the EQ was back in the path and the direct link gone.

At launch with a launch profile (user: effects came back disconnected, Revert fixed
it): the profile load used to start the EQ/plugins a second time (they were not up yet) and
removed the links around them before they were back. Now a device already on its way is not made
again, the profile load spares the links still carrying the sound, and those go only once every
EQ/plugin they went around is back and wired (a plugin host takes about a second after the EQ).
Live: tone → EQ → Hum Remover → recorder, quit, relaunch: no quiet 50 ms window, chain back,
no unsaved changes.

An EQ's nodes appear a moment before their ports (a plugin host's too): a device now counts as
arrived for its wires only once the sockets they use exist (or after 5 s), and its wires stay on
the board while it is on its way — wiring it at first sight left its wires unmade and forgotten,
so the EQs came back disconnected *sometimes* (user's log: "routed around … with 0 wires"). EQs,
plugin hosts and the graph watcher start in their own session: Ctrl+C in a terminal reaches only
PipeBoard, which routes around them before they stop (they still end with it).

Quitting with the window open (user: tray Quit did nothing until the window was
closed; a second Quit then killed PipeBoard mid-quit: no route-around, the window left behind):
aiohttp waits for open connections before it stops, so `on_shutdown` now tells every page "bye"
(the page closes its window, or shows "PipeBoard has quit" if Chrome will not let it) and closes
its connection; the tray's Quit acts once; PipeBoard's own background tasks are cancelled and
awaited before the loop ends. Live: quit with a page connected → exited in 0.03 s, route-around done.
Commands to the shared pw-cli go one at a time: at launch, two gains and two EQs each started
a pw-cli while the first was still connecting, and the forgotten ones' pipes errored at exit
(user's console).

Started by the system (login, `--background`): only the tray, never the window (checked: no
Chrome process for it). PipeBoard may start before the panel's tray is up, and a restarted panel
forgets its icons, so the tray checks every 3 s that its icon is registered with the panel's
StatusNotifierWatcher and registers it again if not (a tray-only app must never run unseen).

### 5.3 EQ (M11, built)

Add node › Effects › **EQ**: a parametric equalizer built into PipeBoard, no plugin needed. The
card shows a small curve with **Bypass** and the **gear** (icons, owner); the gear (or a click on the curve) opens the
panel, docked at the bottom so the board and its wires stay in view.

**The panel:** a log frequency axis 20 Hz–20 kHz, ±18 dB of boost or cut (the view shows ±20),
the summed curve, numbered handles in eight colours, a band's own shape only while its handle is
hovered or held (owner: shown all the time it looked messy), and the spectrum of the EQ's output behind it all. Double-click empty space: a bell there. Drag a handle:
frequency and gain (Shift: fine). Scroll on it, or anywhere over the graph for the selected band:
Q (up is wider, a lower Q: owner); on a cut, its slope. Double-click it: off or on (user). Right-click:
type (Bell, Low shelf, High shelf, Low cut, High cut, Notch), slope for cuts (12/24/48 dB per
octave), on/off, Remove. Delete removes the selected band, ←/→ pick bands, Esc closes. The strip
below shows the selected band's type, frequency, gain, Q or slope as boxes (scroll or type, Enter applies, Esc or clicking away cancels). At most
8 bands and 16 filter slots (a 48 dB cut takes 4); a band that would not fit is refused with a note.

**The sound** (`eq.py`): a PipeWire filter-chain run as its own `pipewire -c <conf>` process,
16 `bq_raw` biquads per channel. PipeBoard computes every slot's coefficients itself (the RBJ
audio-EQ cookbook at the graph's rate; Butterworth cascades for steep cuts) and sets the ones that
changed live with `pw-cli set-param` through the gain's persistent pw-cli: no restart to add,
retype or drag a band, and the curve on screen is the math the sound gets (the page has the same
formulas in `eq.js`). Unused slots and Bypass are exact pass-through. Its two nodes are `/Internal`
streams (no app or mixer lists them), tagged as PipeBoard's own; the board shows them as one card.
Like plugins it ends with PipeBoard (the sound goes around it, §5.4) and starts again at launch; a profile load sends the saved
bands. Measured: bell +6 → +6.0 dB, with a 24 dB cut at 2 kHz → −18.1 dB at 1 kHz (predicted
−18.1), bypass 0.0, both channels. A large instant jump can tick (no ramp): ponytail ceiling.

**PipeWire's ±10 cap.** `bq_raw`'s b0…a2 controls are clamped to ±10 without a word. A high shelf
above about +15 dB needs more (|b1| 10.9 at +16 dB, 13.6 at +18 at 4 kHz); clamped, it boosted the
lows by ~+28 dB (the user was blasted; measured: peaks 7.8 where 0.8 was the most
possible). Now no coefficient leaves ±8: a band that needs more is divided down and the factor
put back as plain gain in up to 3 reserved slots (bands get 13 of the 16), exactly the same sound;
`check.py` sweeps every type × frequency × gain × Q × rate for it. Live after the fix: 0.92.

**The spectrum:** only while a visible window has the panel open, one `pw-cat` records the EQ's
output (both channels summed, 48 kHz), under the live wires' rules (§8a: async, `/Internal`,
never on the board, never a crash). The server only forwards the audio; the page runs a 4096-point
FFT, 240 log bins, tilted +4.5 dB/octave, peaks falling 36 dB/s.

**Designing it:** `./run.sh shots eq` renders every EQ state (card, panel, cut, drag, band menu,
empty, bypass, small window) with a made-up spectrum to `.dev/shots/eq-*.png` and
`.dev/shots/eq-sheet.png`; `./run.sh --fixture` lets you click it with a fake spectrum. The page's
audit runs `EQ.selftest()` in every shot (curve math, FFT).

### 5.5 Limiter (built)

**Why.** Between nodes PipeWire's sound is 32-bit float: a gain or an EQ can go over 0 dBFS and
nothing clips. It clips where it becomes integers: at a card (the UR44C takes s32le; 32-bit has
the same 0 dBFS ceiling as 16 and 24, the extra bits are below it), an app recording in s16, or
inside some plugins. A limiter in front of the card turns peaks down smoothly instead.

Add node › Effects › **Limiter**: the card has **Ceiling** (the gain's dB box, −24 … 0 dB,
default −0.5), **Bypass**, **Soft** and the **gear**. The gear opens a panel docked like the EQ's: the last 6 s,
what goes in (faint, amber where it is over the ceiling) and what comes out, the ceiling as a
yellow line to drag (or scroll, or type below), and "Turning down −x dB" over the last 0.3 s.
The sound is drawn as smooth curves (3-block evening, Catmull-Rom), filled brighter the louder,
fading in at the left. It scrolls every frame on its own clock, kept a few blocks behind the
newest peak (more if they come in bigger bursts), so peaks arriving a few at a time never show as
jumps (owner: it was not smooth). Measured with uneven arrivals (scratch/dynsmooth.py): 61 fps,
0.98 blocks per 10 ms, 1.8 % of frames off pace by over 25 %; a redraw costs 0.8 ms.
Out is drawn in step with in (owner: the peaks looked misaligned, then slightly different): out
trails in by the plugin's own delay (limiter 240, Noise Suppression 1016 samples at 48 kHz) plus
none, one or two graph cycles, as the graph happens to run it: fed by a player it was one, on the
owner's board behind their Bass EQ none (measured on it, read-only: 240 exactly). So the server
tries those few delays on the sound itself about once a second (correlation of the last half
second) and delays the capture's in channels by the best, to the sample, before cutting blocks
(`dynamics.Aligned`): a block's in and out are the same sound. Measured live with Bypass on,
|in − out| per block: compressor and limiter 0.00 dB, Noise Suppression ≤ 0.26 (its 48 kHz
resampling; whole-block shifts on the page had left up to 1.7 dB). Only Noise Suppression runs at
48 kHz (Speex needs it); every other effect at the graph's rate; the 48 kHz capture only listens.
"Turning down" compares the loudest in and out over the last 0.3 s, in's window lag earlier and
out's a block wider, so a peak at a window's edge never reads as turning down (owner saw a phantom
−0.8 dB on fading music: reproduced live with unshifted windows, 0 of 100 readings now; a real
−10 dB of limiting still reads −10.1).
The graph is two layers (owner: it stuttered while live wires ran): the sound's shapes in their
own SVG, wider than the plot, slid by the GPU every frame and rebuilt at most every 100 ms (the
clock runs that much further behind); in front, still, the grid, the line and the fade. Traced on
the owner's board: raster work 1456 → 176 ms per second. The EQ's spectrum likewise has its own
GPU layer and now moves every frame (every other one stepped): 465 → 97 ms per second.

**The sound** (`dynamics.py`): LSP's Limiter Stereo (LADSPA) in a filter-chain process run and
stopped like an EQ's (`eq.Eqs`, `eq.chain`); the ceiling (as LSP's Threshold factor) and bypass go
live with `set-param` through the shared pw-cli. LSP's defaults turn on Gain boost (makeup gain to
0 dBFS) and automatic level regulation: both off. Lookahead 5 ms, attack 5, release 20 ms, no
oversampling or dither. Owner's choice: LSP's DSP, not our own. Measured live: a +6 dB tone under
a −6 dB ceiling records at 0.501 (ceiling 0.5012), its onset included; quiet sound passes untouched.

**Soft** (a button on the card and the panel, off by default; owner; called Smooth until the owner
renamed it, still `smooth` in the saved board): heavy limiting with
Sharp was harsh, since a 20 ms release moves the gain within a bass wave. Soft turns on LSP's
automatic level regulation (a leveller before the brickwall: attack 20 ms, release 500 ms, its
Knee +7.5 dB so it settles just under the ceiling; its target is Threshold × Knee − ~8 dB) and
the Herm Wide shape. The brickwall stays last: the ceiling holds either way. Measured, 50 Hz at
0 dBFS under a −14 dB ceiling, offline and live alike: Sharp 34.7% THD, Soft 0.25%, held at
−14.8 dB; quiet sound untouched; music-like loudness within 0.4 dB of Sharp; it breathes a little
instead (a steady tone dips 9–11 dB per beat, 13.6 with Sharp). Knee +9 dB hands the work back to
the brickwall (1.2%). LSP's "Knee smooth" does nothing here.

**The panel's levels:** LSP's meters are output controls, which filter-chain does not publish, so
PipeBoard measures: while a visible window shows the panel, one `pw-cat` records 4 channels at
48 kHz (the ports wired into the limiter summed on 2, its output on 2; it follows rewiring), and
the server sends the loudest sample in and out per 10 ms (`lim-peaks`), not the audio.

### 5.6 Compressor (built)

Add node › Effects › **Compressor**, for squashing music on purpose (owner); put a Limiter after
it. The card: **Threshold** (the dB box, −60 … 0, default −18), **Bypass**, the **gear**. The live
panel is the limiter's (`dynamics.js`, one panel for both kinds): the history, the threshold as
the line to drag, and a strip with Threshold, Ratio (1–20, default 4:1), Attack (0.1–200 ms,
10), Release (10–2000 ms, 150) and Makeup (0 … +24 dB, 0). "Turning down" leaves the makeup out.
In both panels amber is where in was louder than out: what was turned down.

**The sound:** LSP's Compressor Stereo (LADSPA), downward, RMS detection over 10 ms (LSP's own
default; smoother on music than peaks), a −6 dB soft knee, no sidechain filters, all wet. LSP's
LADSPA defaults are not its own (attack 1.5 s, reactivity 187 ms), so every control is set.
Measured offline and live alike, threshold −20, ratio 4: a +6 dB tone comes out at −11.2 dB, a
−6 dB one at −14.3 dB; 1 dB out per 4 dB in above the threshold; attack and release reach 63 %
in 12 and 117 ms. Limiter and compressor share one module, one op (`dyn-set`), one capture.

**Panels move:** the EQ's, the limiter's and the compressor's panels can be dragged by their
header (owner); each opens docked at the bottom again. The card whose panel is open glows in its colour,
and a click on the panel's title frames that card on the board and flashes it. A click on empty board closes
the panel (a pan does not).

### 5.7 Noise gate (built)

Add node › Effects › **Noise Gate**: turns the sound down while it stays under the threshold
(hiss between words). The card: **Threshold** (−60 … 0 dB, default −40), Bypass, the gear; the
panel's strip adds **Range** (how far it turns down when closed: −70 … 0, default −40),
**Attack** (5 ms), **Hold** (100 ms) and **Release** (150 ms). LSP's Gate Stereo, in the same
module and panel as the limiter and compressor (`dynamics.py`, `dynamics.js`; the panel's scale
now reaches −60 dB). Peak detection over 10 ms, so the line means what the panel's peaks show
(RMS opened 3 dB above it on a tone); hysteresis: it closes 4 dB under where it opens, so a voice
at the threshold does not chatter. A soft 6 dB zone under the threshold (owner: the first version
was "a sharp on-off"): LSP's attack and release smooth its level reading, not the gain, so a hard
threshold switched 40 dB within 1 ms; with the zone the gain fades as the level crosses it, at
most 2.6–3.4 dB/ms where audible (a sudden word still opens at once, masked by the word). Measured offline: a −30 dB threshold opens at −29.7 and closes
at −34.8 dB; on syllables over −55 dB hiss it stays open through the phrase and the hiss between
phrases goes down by the range. Live: a −50 dB tone under −30 comes out at −90 dB; −10 dB passes
at −10.0.

### 5.7a De-esser (built)

Add node › Effects › **De-Esser**: turns harsh "s"/"sh" sounds down. The card: **Threshold**
(−60 … 0 dB, default −30), Bypass, the gear; the panel's strip adds **Frequency** (2–12 kHz,
default 5 kHz, steps of a semitone; "6k" and "6.5 kHz" both type). **Split-band**: per channel the
sound is split at Frequency by PipeWire's own biquads (two low-passes, two high-passes in a row:
Linkwitz–Riley 24 dB/oct, which sum back flat), only the top band goes through LSP's stereo
compressor (peak detection over 1 ms, ratio 6, attack 1 ms, release 60 ms, no lookahead: a delay
on one band would comb when they are summed), then both are mixed. The threshold is for the top
band only, so the panel shows no line for it (it would not match the whole-sound graph). Chosen
by measuring three on a voice-like signal with "s" bursts, each set for about −8 dB on the "s":

| De-esser | "s" | voice body during "s" | between | air between |
|---|---|---|---|---|
| split-band (built) | −7.6 | ±0.0 | ±0.0 | −0.3 |
| ZamDynamicEQ high shelf | −7.8 | −0.1 | ±0.0 | −0.9 (and at most −10 dB) |
| TAP DeEsser / LSP compressor wideband | −7 | **−7** (ducks the voice) | ±0 | −0.1 |

Live through the node: "s" −7.3 dB, the voice ±0.0, air −0.2 (the bands sum back flat in PipeWire).

### 5.8 Noise suppression (built)

Add node › Effects › **Noise Suppression**: takes steady noise (hiss, fans, hum) out of a voice.
The card has **Strength** (−40 … 0 dB, default −12: how far noise is turned down; owner: at −30,
the first default, the voice sounded low quality, Speex's watery artefacts), the **power**
button and the **gear**; the panel shows in and out, amber where it removed something.

It is PipeBoard's own small LADSPA plugin, `native/pb_denoise.c`, around Speex's preprocessor
(libspeexdsp, installed with PipeWire's Pulse side; only noise suppression on, no AGC, VAD or
dereverb). `denoise.py` compiles it once, on demand, into `~/.cache/pipeboard/pb_denoise.so`
(rebuilt when the source is newer); it runs like a dynamics node (`dynamics.py`), one plugin per
channel, the graph at 48 kHz. Speex takes 10 ms frames of 16-bit samples: sent in at half level
(6 dB of headroom) and brought back up. It is 20 ms late (reported on its latency port); the dry
sound is delayed the same, so **Bypass** is a 20 ms crossfade to the input exactly (owner: a
bypass is a pass-through). It replaced PipeWire's WebRTC echo-cancel module, which had no bypass
(ignored changes while running, and changed the sound even with suppression off).

Measured offline at 48 kHz, −40 dB steady hiss under a voice (plan notes, M17):

| | hiss alone | between syllables | voice kept |
|---|---|---|---|
| Strength −20 | −58.2 dB | −55.7 dB | −0.1 dB |
| **Strength −30** | **−64.8 dB** | **−58.8 dB** | **−0.1 dB** |
| Strength −40 | −69.4 dB | −60.1 dB | −0.1 dB |
| WebRTC (before) | −59.2 dB | −54.2 dB | −2.4 dB |

Classic, not neural: it does little against sudden noises (keys, clicks); a gate after it helps.
A neural model is O11 (open.md).

### 5.3a Dynamic EQ bands (built)

Pro-Q style (owner): a bell or shelf band's **Dynamic** button (in its strip) makes it move with
how loud its own region is. A second row: **Threshold** (−60 … 0 dB, default −24, the band's level
where it starts), **Range** (−18 … +18 dB, default −6: negative cuts it when loud, positive boosts
it when quiet), **Attack** (10 ms), **Release** (150 ms). On the curve its reach is shaded in its
colour (exact: the curve with the stage at full movement), its edge drawn as a line where it can
move (FabFilter-like) which **drags the Range** up and down (owner), and its handle gets a dashed ring.
Every curve is sampled densely around each band (closest at its tip), so a sharp Q is drawn round.
**Live movement** (Pro-Q 4, owner): while the panel is open the band's curve moves between rest and
its edge as it works. LSP's meter cannot be read, so it is measured: the EQ's capture carries what
goes in (all wired in, summed) and what comes out; per dynamic band the page weighs each FFT bin by
the region filter's power squared and compares what came out with what the static EQ alone makes
of what went in, clamped to the range; a cut counts only while the band is near or over its
threshold (≥ T − 6 dB), a boost while under it (≤ T + 6), else at rest (a trace of another
frequency, moved by something else, was counted). Checked on known signals in the real page
(−6/−3/0 exact, clamped, gated, off-band 0, boost +3) and end to end (headless Chrome on a test
PipeBoard, live EQ at 1 kHz, T −30, R −6): 0 on a −40 dB tone, −6.0 on −10 dB, back to 0 when it
stops. Drawn so it never looks like another band (owner): the reach barely tinted, its edge a thin
dashed line (solid under the pointer). The **"now" curve** (owner) is one continuous thin line, the EQ
curve with every band's live movement applied, white over each dynamic band's core and fading into
the pink curve away from it (a gradient along it): at rest it lies on the curve, working it moves
inside the range. A **level meter** beside Threshold (owner) shows the band's
own level live as its processor reads it (a bar −60 … 0 dB, a tick at its recent peak, amber while
over the threshold; the threshold a white line to drag or scroll; the reading beside it) (the input through the static EQ and the region filter, its
peak; amber while over the threshold), and gates the live measure: the owner's band "did nothing"
because on their music its region peaked at −44 dB (−29 at most) under a −24 threshold, invisible
until now (an FFT average read ~9 dB lower still).
**The sound** (`eq.py`): after the two rows, 4 stages, each per channel `x − F(x) + P(F(x))`: F the
band's region in a raw biquad (an RBJ band-pass for a bell, a low-/high-pass for a shelf), P LSP's
Dynamics Processor (mono) with two curve points (input level, output level) so it moves by at most
Range at a 3:1 slope, unity outside; the subtraction is the builtin `invert` (PipeWire's mixer
clamps a negative gain to 0: it doubled the band). At rest P is bypassed and the stage passes the
sound exactly; a glitch in F when its frequency changes reaches −F and P(F) alike and cancels.
Settings go live with `set-param`, only the stages that changed. 4 stages because a stage costs
CPU even at rest (LSP keeps processing while bypassed): 8 doubled an EQ's (3.7 → 7.3 % of a
core); a 5th dynamic band is refused with a note. Without LSP the EQ starts without stages.
Measured live at 192 kHz: a 1 kHz band, Threshold −30, Range −6: a −40 dB tone → −40.0, −10 dB →
−16.0; ten frequency moves at rest while a tone plays: leftover −176 dB (nothing); offline, at rest
exact to 1e-8, a boost +6.0 when quiet, 2 octaves off ≤ 0.5 dB.

The movement adds to the band's own gain: the stage works on the sound after the static EQ, so a
+4 dB band with Range −6 goes from +4 to −2 dB (owner asked whether a band already boosting
offsets the range: it does not). Measured live, 1 kHz: +4 / −6: −50 → −46.0, −10 → −12.0;
−4 / +6: −50 → −48.0, −10 → −14.0; the page's movement at full: −5.999 for a Range of −6, so
the white line meets the dashed edge.

**Found on the way (M25):** filter coefficients set before an EQ's node has ever run are lost:
PipeWire's raw biquads start from the config's then (a plugin's controls keep theirs; once it has
run, an idle pause keeps them too). So a new EQ edited before any sound flowed played flat, and
later edits only sent what differed. The server now watches each EQ's input node and, the first
time it runs, sends everything again (`Eqs.ran`).

## 6. Virtual devices; renaming, removing, deleting (M6, built)

- **Create:** Add node › Devices › Virtual › New microphone… / New speaker…: a dialog with the name
  (up to 40 characters, no quotes, brackets, braces or `=`) and Stereo / Mono. The card appears where
  you right-clicked. Each device is **one null-sink node inside PipeWire** (`devices.py`):
  `object.linger`, so it outlives PipeBoard (tested: still there after PipeBoard exited) and needs
  no process; `a setup script`'s `pkill` cannot touch it; a PipeWire restart (login) clears it,
  and the board/profile remembers it. A mic is `Audio/Source/Virtual`, a speaker `Audio/Sink`
  whose monitor ports are its outputs. Tagged `pipeboard.owned`, `pipeboard.id` (card key
  `pb:<id>`, never changes) and a `node.name` that never changes either (apps remember devices by
  it; a slug of the first name, unique).
- **Rename in place:** double-click the title (or right-click → Rename). A floating hint says
  what the name does. Enter saves, Esc cancels.
  - **Hardware, apps and the user's script-made mixes:** a nickname only PipeBoard shows; empty
    restores the original; Names › lists current, original and earlier names (✕ forgets one).
  - **PipeBoard's own devices:** a real rename: apps see it. The device is made again under the
    same node.name and id, and its connections come back (the board marks it *waiting* and makes
    its saved links the moment it appears, before anything can forget them). Earlier names › to
    go back.
- **One removal per card, always called Remove** (user: fewer options, consistent),
  its hint saying what it does:
  - *a device on the system:* **Remove** takes it off the board. The wires the user made to it in
    PipeBoard (connect, move, insert, drop-to-add; the board's `made`) are set aside on the card
    and unplugged; the system's wires (WirePlumber's, a script's, a loaded profile's) stay. + Add
    brings it back with its name and place and remakes the set-aside wires.
  - *PipeBoard's own device (mic, speaker, gain):* **Remove…** deletes it from the system, behind a
    dialog that names it, says apps using it lose it, and counts the connections that go (Cancel
    has the focus). The server checks the board's record and the live node's `pipeboard.owned`.
  - *a missing device:* **Remove** forgets it and its waiting wires. (Besides: Use another device ›,
    and Create this device for PipeBoard's own.)
  Wires keep their own Disconnect; a card's menu also has **Disconnect** (owner: wanted back): every
  wire in or out of it (`disconnect` with `card`), shown only when it has some.
- **Profiles keep PipeBoard's devices** (`devices` in the board and profile files). Loading a
  profile makes any that are missing and wires them when they appear; a missing one offers
  **Create this device**.

- **Only this board's own.** Every device, EQ and plugin node is tagged `pipeboard.board` (a hash
  of the config folder). PipeBoard nodes of another board (a test run, another config) are not
  drawn and not remembered; ghost cards already saved under `pb:` keys the board does not own are
  forgotten. A lingering device of this board (or untagged, from before) that the board has lost
  — a launch profile saved before it was made replaced the board — is taken back from its node
  (a gain with its current level) and shown again, so it has its level box and can be removed.

## 7. Profiles (M5, built)

A profile is the whole board in one file, `~/.config/pipeboard/profiles/<name>.json`:

```json
{"version": 1,
 "cards": {"mic_mix": {"x": 1330, "y": 530, "nick": "Stream Mic", "names": ["Podcast"], "hidden": false,
                         "seen": {"title": "Mic Mix", "kind": "virtual", "role": "mic",
                                  "inputs": [{"id": "in", "label": "IN", "n": 2}], "outputs": [...]}}},
 "wires": [{"from": ["device:alsa_card.usb-…Studio Interface-00", "out2"], "to": ["mic_mix", "in"],
            "pairs": [[0, 0], [0, 1]]}],
 "view": {"x": 0, "y": 0, "z": 1}}
```

`seen` is the card's last known shape, so a device that is gone can still be drawn and re-wired.
`pairs` say which channel of one socket feeds which of the other.

**The board is itself a profile** (`workspace.json`). On every graph change it is re-snapshotted
against the system (`profiles.snapshot`): live cards and wires, plus whatever it knew about devices
that are gone now. Everything else is a comparison:

| | How |
|---|---|
| Unsaved changes | `changes(saved profile, board)`: wires added and removed, renames, cards removed or put back, cards moved |
| Load preview | `plan(profile, system)`: links to make, links to remove, devices not on the system |
| Load | the profile becomes the board, then `plan` is applied (removing only links between cards the profile knows, so apps started later keep their routing) |
| Settings | a gain's level, an EQ's bands and bypass, a plugin's control values and bypass are part of the profile: changing one shows as "Changed the settings of …"; loading a profile or Revert sets them on the running devices themselves (the gain ramps), not only on the board |
| Revert | undoes exactly what the changes card lists: every link added since the save goes, whatever it connects, and PipeBoard's own devices created since, in this session, are deleted (never one that was already there: a Revert once deleted two gains the board had just taken back); then as Load |

- **First launch:** the board shows the system as it is, and that is saved **once** as *Default*.
  **No profile saves itself** (user, replacing the earlier auto-saving Default). The
  board itself (`workspace.json`) is still remembered between runs.
- **A profile, changed:** the bar says "unsaved", and a card at the bottom right lists the
  changes ("+ Music Player → Music Mix", "− Music Player → Desktop Mix", "Renamed Mic Mix to Stream Mic",
  "Moved 2 cards") with **Revert** and **Save**. Ctrl+S saves. Moving cards counts as a change
  (user).
- **The profile button** (bar): the profiles, the active one ticked and the launch one marked;
  Save; Save as new profile… (name checked as you type; replacing one asks first); Load at launch ›
  (Nothing, or a profile); Delete profile › (asks; says nothing on the system changes).
- **Loading** shows what changes first (Connect / Disconnect / Not on this system) and asks.
  Only links between two cards the profile knows are removed; anything new is left alone.
- **Load at launch:** the server loads that profile when it starts (`server.startup`).
- **Missing devices:** a card whose key is not on the system is drawn from `seen`, dimmed and dashed,
  with an amber ! in its corner, and listed as a *Device missing* warning. The wires that wait for it
  are grey dashed ghosts (select one and Delete to forget it). Right-click it:
  - **Use another device ›** existing devices, same kind first. The chosen device takes the missing
    one's place, name and wires: socket by id, else by position; pairs that still fit are kept, a
    socket whose size changed is re-paired the default way; a wire whose socket has no counterpart is
    dropped and listed in a notice. Nothing crashes on a mismatch;
  - **Create this device** (M6, for virtual devices);
  - **Forget this device** and its connections.
- Apps are never missing: their streams come and go. A device on the system with nothing to show
  right now (Easy Effects' idle mic chain) is not missing either: the model lists everything
  `present`, shown or not. Cards removed from the board are never reported, and a missing device
  with no connections waiting is dimmed but not listed as a problem.
- Profile names are file names: letters, digits, spaces, `- _ ( ) '`, up to 40; checked in the
  page and again in the server.

## 8. Running in the background (M9, built)

**PipeBoard replaces `a setup script`** (user): it can start at login, minimized
to the tray, apply the launch profile, and make the virtual devices itself.

- **Tray** (`tray.py`): a StatusNotifierItem with a dbusmenu over the session D-Bus (`dbus-fast`).
  Click: raise the window, or open one. Menu: Open PipeBoard · Launch at login ✓ ·
  Quit PipeBoard. A red dot while an error stands; the tooltip names the profile and the problems.
  No bus or tray host: logged, PipeBoard runs on without it. (Checked live: registered with
  Plasma, menu read like the panel reads it, a toggle saved, Quit left the panel and ended cleanly.)
- **Window:** closing it keeps PipeBoard running in the tray. Open raises the existing window with
  a KWin script over D-Bus (window found by its app id); elsewhere it opens a new one.
  `--background` starts with the tray only; `--no-window` (tests) with neither.
- **Launch at login** (`autostart.py`; tray menu and the board's right-click menu): a systemd user
  unit `pipeboard.service` (`After`/`PartOf` graphical-session.target, `ExecStart` this checkout's
  Python with `-m pipeboard --background`, restarted on failure), enabled or disabled. It starts
  **minimized** (user): tray only, launch profile applied.
- **One copy only:** a second launch shows "PipeBoard is already running: click its icon in the
  system tray" and closes.
- **Taking over `a setup script`** (`takeover.py`; right-click menu "Take over devices…", shown
  only when there is something to take over): the virtual devices whose loopback processes run in a
  systemd user service (by the cgroup of each node's client process, never by name). A dialog lists
  them and says what happens and how to undo it. Then: stop and disable the service (systemd ends
  its `pw-loopback` processes), move the board's cards to PipeBoard keys, make each device again
  under the **same `node.name`** (apps and the default devices keep finding them), and restore its
  wires as it appears. Undo: delete them in PipeBoard, `systemctl --user enable --now <service>`.
  Checked on the fixture with the user's seven mixes, and live with a throwaway service of two
  loopbacks (the user's service untouched).
- **Taskbar icon and app menu:** the window opens `/pipeboard/`, so Chrome gives it its own app id,
  `chrome-127.0.0.1__pipeboard_-Default` (at `/` it shared `chrome-127.0.0.1__-Default` with any
  local Chrome app window, Jarvis included). Each start installs a desktop entry of that name and
  the icons (`pipeboard`, `pipeboard-tray`, `pipeboard-tray-alert`) into `~/.local/share`.
- **Icon:** `tray.svg` is the mark: two sockets joined by the board's amber-to-teal wire, bold
  enough for 16–24 px; `tray-alert.svg` adds a red dot; `icon.svg` is the mark on a dark tile.
  Rule: nothing thinner than ~1/10 of the tile, no detail that vanishes at 22 px.

## 8a. Live wires (M10, built)

The user wants to **see live activity** on the board: each wire swings around its curve with the
signal it carries, a low-resolution picture of the last ~100 ms, newest at the source end. The swing
is tapered to zero at both sockets (widest in the middle); silence is the plain curve. The bar's
**Wires** menu chooses: **Still** (off), **Flowing** (the sound travels along) or **Instant** (the
whole wire shows the sound now); one choice, as Instant means nothing while still (owner; was an
Animate toggle and an Instant switch). Flowing is the default (`settings.json` `live`,
`live_instant`); not in the tray, the animation being the window's own.

**Never touch the audio** (the user's hard rule):
- The capture is one `pw-cat --record --raw` at 8 kHz, resampler quality 0, with
  `node.async=true`: the audio cycle never waits for it. Proven on this machine: wired to 16 real
  outputs and blocked for 8 s, no xrun counter moved (`scratch/async_stall.py`).
- Its class is `Stream/Input/Audio/Internal`. The plain `Stream/Input/Audio` is listed by the
  Pulse side as an app recording (Plasma's applet, mixers, possibly the mic indicator); a made-up
  class gets no ports at all; the `/Internal` variant has ports and is listed nowhere. It is never
  on the board (`graph.py` skips `pipeboard_scope`). Raw patchbays (qpwgraph, Helvum) still draw
  it while a window is visible.
- **It runs only while a window is visible and Live is on.** The page reports visibility
  (`visibilitychange`); no visible page, no capture. Stopping the capture removes its links.
- **It never crashes anything.** Start, linking, reading, encoding, sending, decoding and drawing
  each contain their failures; a dead capture is retried 5 times with a pause, then left alone, and
  the wires stay plain. Checked by removing `pw-cat` from PATH, killing the capture, feeding junk.

**Cheap:** one process for every wire (0.33 % CPU for 16 channels measured), the strongest sample
per millisecond per port (loudness true for every frequency), binary frames at 25 fps carrying only
the values that are new since the last frame and only for sources with sound.

**Roll mode: smooth, prompt, in the signal's direction** (user: too fast, too square,
then a bit faster, then "quite a bit of delay"; later, faster again, to look more like the real
sound). Sound travels at a steady **480 px/s** (was 260) on every wire, like an oscilloscope in
roll mode: a long wire shows a second or two of history.
- *Capture:* `pw-cat` runs under `stdbuf -o0`. Buffered, it wrote 4 KB blocks: with 2 channels
  data came every 65 ms in lumps (latency and stutter); unbuffered, every PipeWire cycle (~5 ms).
- *Frames:* 50 a second, carrying only new values with a running count; in silence just the
  count (4 bytes), so the page's clock never loses the beat.
- *Wiggle follows the dominant pitch* (owner: a kick drum a wide wave, a hi-hat dense ones). The
  real waveform cannot be drawn (at the roll speed a 100 Hz cycle is 2.6 px), so the capture also
  splits each channel into three bands (one-pole low-passes at 250 Hz and 2 kHz: low, mid, high)
  and each frame carries one brightness byte per source: the bands' pitches (90, 650, 3000 Hz)
  weighted by their energy, 40 Hz … 4 kHz on a log scale (`scope.bright`). A kick reads ~52, a
  voice ~127, a hi-hat ~172, a kick with a quiet hi-hat over it stays ~53 (zero crossings, or a
  change-to-signal ratio, let a little treble take over: owner). The wave's wavelength follows it,
  44 px for a kick down to 5 px for a hi-hat (owner: wider range; a point every pixel); its height is still loudness (±6 px). 16 channels
  cost 16 ms of CPU per second. The live-frame shot feeds the page a frame from the capture's own
  encoder, so the two never disagree on the layout.
- *Page:* values are folded into **13 ms slots** (about 6 px at the 480 px/s roll; owner: faster, was 260 px/s and 24 ms) (high and low) as they arrive and played back
  **50 ms** behind the newest by a real-time playhead, nudged toward the server's clock only past
  ±15 ms (2 % a frame) and put straight in place when the loop starts or after a stall. Measured on
  the fixture (whose made-up sound now keeps real time, like a sound card): 3 × 8 s at 60 fps, every
  step exactly 1.000 ms per ms.
- *Latency:* ~5–10 ms capture, ~10 ms frame wait, 50 ms margin: about 70 ms from sound to wave.
  The swing eases in and out over the same **36 px at both plugs** (user: 18 px at the source
  and 70 px at the destination made the two ends look different; 70 px at the source had hidden
  new sound for ~0.3 s).

Each slot is one point of the wire, alternately its high and its low (by the slot's own number, so
the pattern travels with the sound), smoothed into a wave of ~15 px: up-swings are the sound's real
positive peaks, down-swings its real negative ones, and the swing follows the loudness (one dB gain
per frame, linear inside, so nothing is squared off). Drawing every live wire costs ~0.25 ms per
frame; the loop runs while a wave is in sight or sound arrived in the last 0.5 s, and stops in
silence.

**Restarts that cannot go wrong** (user: waves stopped after changing the graph). Any
change to the wired outputs restarts the capture. Found and fixed, each with a check:
- overlapping restarts each started a capture and kept only the last: 20 quick wire changes left
  12 running → restarts hold a lock (a mutation test confirms the check fails without it);
- a dead capture was never retried (its time of death was re-noted every frame) → noted once;
  retried after 1 s, up to 5 times, the count reset after 10 healthy seconds;
- a capture that delivers nothing for 2 s, for whatever reason, is treated as dead;
- each capture has its own node name (pid and counter), so a restart cannot link to the old one;
- the page gets the new source list before the first frame of the new capture, and drops the old
  history then; a page that fails to take a frame no longer costs the other pages theirs.

**No orphans:** the capture is started with `PR_SET_PDEATHSIG`, so the kernel ends it whenever
PipeBoard ends, even on SIGKILL (tested); its node name carries PipeBoard's pid.

**Clipping:** full-scale samples turn that stretch of the wave red with a soft glow, fading in and
out smoothly (the wave again through a red gradient along the wire, each point as strong as it
is near a clipped one; owner: rather than red flecks, and without steps), and raise a *Clipping* warning in
the problem center for 2.5 s after the last one.

## 8b. Show latency (built 2026-11-05)

A checkbox in the bar, saved with the board like Portals (`show_latency`). On, each effect or
plugin card gets a small tag centred under it with the delay it adds, and each virtual device the
total delay of the sound arriving there (owner's spec; renders in the plan's artifacts,
`./run.sh shots latency`).

- **Where the figures come from** (`graph.latency`, pure): a device's capture and playback delay
  are PipeWire's port Latency figures (block + ALSA headroom; `card["delay"]`). An effect of
  PipeBoard's adds what `dynamics.latency_ms` knows: the limiter its 5 ms lookahead, Noise
  Suppression its two 10 ms Speex frames, the rest 0; gains and virtual devices 0. A plugin or
  Easy Effects shows `?`. PipeWire's own totals are not used: every filter reports 0 and every
  null sink restarts at 0, so totals are added up along the wires, longest path in.
- **Totals** start at an app (0) or a device's capture. `up to` when paths of different length
  meet; `≥` when an unknown delay is on the way. A feedback loop is cut where it closes.
- Not counted: an app's own buffering, and the extra graph cycle a stream can add (seen with a
  player feeding an effect, §5.5's peak alignment).
- **Where a total comes from**: a click on a total opens a panel docked at the bottom (owner's pick
  L1): the route and total, then the chain, every step a stop on one line in order; a step that
  delays stretches with its ms and name under it, one that does not is a dot. Pointing at a step
  lights its card; off-path cards and wires dim. The footer: dropouts since it opened, and what
  most of the delay is (the interface's buffer: a button to the Audio engine).

## 8c. Audio engine (built 2026-11-05)

Click the clock readout in the bar (`192 kHz · 1024 · 5.3 ms`): a panel under it sets the
**block size** (128 … 2048); the **safety margin** of the interface clocking the graph (ALSA's
headroom) follows it, equal (the owner's UR44C, its buffer 3 periods: a smaller margin made the
input drop out ~100 times a second, a larger one did not fit and the stream restarted endlessly;
equal ran). It shows the round trip (block + margin, in and out) old → new,
and under **Advanced** the realtime **priority** of PipeWire's audio thread (1 … 95) and the
**nice** of its control thread (−19 … 0), with the honest note that those do not shorten the
delay. Sample rate is not offered (the owner pins 192 kHz on purpose).

- **Where it is kept** (`engine.py`): two files of PipeBoard's own, sorted after the user's
  so they win, never the user's own files: `~/.config/pipewire/pipewire.conf.d/99-pipeboard-engine.conf`
  (`default.clock.quantum`, min and max: pinned, so the block never floats away from the ALSA
  period, the cause of the owner's August pops; `module.rt.args`) and
  `~/.config/wireplumber/wireplumber.conf.d/99-pipeboard-engine.conf` (the interface's ALSA
  nodes, matched by name: `api.alsa.period-size` = the block, `api.alsa.headroom` = the margin, 1 to 3 blocks; on
  interrupts `api.alsa.period-num` = 2 × (blocks + 1): PipeWire fills to block + headroom but
  clamps that to half the buffer, and a 3-period buffer had cut a 3-block margin to 1.5).
  The interface is the ALSA node most of the graph follows (`node.driver-id`).
- **Applied without restarting PipeWire** (`engine.apply`): a PipeWire restart drops every app's
  stream and most apps never reconnect (owner: they vanished from the board and went silent, yet
  kept "playing"; the first build did that). The block and the margin need the interface's node
  made again: its ALSA period is fixed when it is made, and the block must equal it (the owner's
  UR44C runs on interrupts, `disable-tsched`: the second build changed the block live and ran 512
  on a 128 period, and the sound cut out for long stretches; a headroom set live as Props was cut
  down by the old 3-period buffer). So **only WirePlumber restarts** (it makes the device nodes
  from the rule file just written; apps are PipeWire's clients and keep their streams; WirePlumber
  links them again), the block goes into the `settings` metadata while the interface is away (min,
  block, max, in the order that never makes min > max), PipeBoard's own effects are stopped before and
  started fresh after (WirePlumber setting up their ports anew while they ran put them in error,
  and PipeWire restarted the interface's stream over and over for ~50 s until they recovered),
  and then the board's wires are made again from the board as it was (`rewire`, every quarter
  second until two looks find nothing to make; not the apps', WirePlumber links those): WirePlumber's start sets
  up every audio node's ports anew, which drops **every** link on them, not only the
  interface's (owner: Spotify reached Music Mix and went no further). Priority and nice alone restart nothing: `chrt -f -R` on the audio thread (it
  carries `SCHED_RESET_ON_FORK`; clearing it is not allowed) and `renice`. `pw-cli` exits 0 even
  when a command failed: its printed errors count as failures.
- **A minute to keep it**: a bar under the bar shows the seconds left in a ring, what is tried,
  its round trip and dropouts, and Keep / a red Revert; with no Keep the server goes back by itself (window open or not; if PipeBoard was quit
  during the minute, at its next start): the files as they were, and the old values applied live
  the same way. The graph is read again after each change: if it does not run what was asked, the
  bar says so. The margin shown is the one that runs: the headroom in PipeWire's latency figures
  for the interface's ports.

- **CPU idle states** (Advanced): one toggle per state the kernel's cpuidle list reports (any
  CPU vendor, any idle driver; by name and wake time; POLL, the busy-wait, never offered). A
  change runs one fixed root script through `pkexec` (names checked against what the CPU reports,
  letters and digits only): exactly those states off on every CPU now, and at boot by PipeBoard's
  own `/etc/udev/rules.d/99-pipeboard-cpu-idle.rules`; any other rule touching cpuidle (the
  owner's `99-cpu-cstate-audio.rules`) is set aside as `*.rules.pipeboard-off`, never deleted.
  Nothing restarts and there is no trial. The scripts are checked on a made-up sysfs and rules
  folder, a line carrying a command refused by the helper itself; the owner's password prompt
  appeared from the login service.

- **Buffer headroom** (`headroom.py`, built 2026-11-06; the owner's pick G of eleven renders, always
  open): under Safety margin, how close the interface's buffers came to running out in the last
  minute. The line "● Output 1.3 ms ● Input 7.0 ms" (each side's lowest; green ≥ 1 ms, amber < 1,
  red < 0.5), the minute as a graph from 0 ms (red: a click) to block + margin, its lowest point
  labelled with when. Read from the kernel's status files (`/proc/asound/cardN/pcmD{p,c}/sub0`,
  found from the nodes' `alsa.card` / `alsa.device`): the output is what PipeWire wrote and USB has
  not taken yet (`appl_ptr − hw_ptr`), not ALSA's `delay`, which counts the ~2 ms already in USB
  transfers that cannot cover a late block (the first figure did and looked 2 ms safer than it
  was); the input is the room before the ring overflows. Sampled about every millisecond in a
  thread, a low sent every half second, only while a page shows the panel (~2 % of a core; the
  files stay open and are read again, a fresh open each time cost ~6 %). The owner's machine at
  block 512 with two blocks of margin: output 1.3 ms, input 7 ms, so the output is the tight side.

- **Clipping is seen by a second capture** (`scope.py`): the 8 kHz live-wires capture filters a
  full-scale peak away (a clipped 6 kHz burst read 0.34 there), so beside it runs one at 1 kHz in
  PipeWire's peaks mode (`resample.peaks`: each value the loudest of its stretch; 1.35 for the same
  burst), linked to the same ports, used only to mark which sources reached full scale. Clipping
  shows within a frame (20 ms) of happening (owner: "doesn't appear immediately").

## 8d. Diagnostic cards (built 2026-11-06)

A **Diagnostic** group in Add (owner): **Waveform** and **Stereo**. A diagnostic card is a tap: it
has an input only; wire any output into it beside its usual path and PipeWire copies the sound
there, nothing in the sound changes. Not offered in the middle of a wire (no output) or for a
wire dropped from an input.

- **The device** (`devices.py`): each card is a null sink, `Audio/Sink/Internal` like a Gain (no
  app or desktop list shows it), tagged `pipeboard.kind=diag` and `pipeboard.type`; always there,
  so its wires are real links that profiles keep. The board shows its input only
  (`graph.build`); `adopt` reads the type tag, so it is never taken for a Gain.
- **What it hears** (`diag.py`): one capture (`Taps`) for every card that is on, two channels
  each from its sink's monitor, 48 kHz, and only while a window shows the board (owner: only
  then; off saves the work). Under the live wires' rules: async, passive, /Internal, a
  `pipeboard_scope_*` name (plumbing, never drawn). The server only forwards what came in each
  20 ms frame (`diag-pcm`); the page does all the drawing.
- **The look** (owner's pick D of seven): a dashed edge from L's colour to R's, the pin and
  socket near white, a wire into one ending white. L magenta `#f84fcc`, R green `#0fbb7c`
  (owner's pick G after four rounds: opposite hues, strong, as a special category should be),
  in one place: board.css `--diag-l`, `--diag-r`. A power button: off, nothing is captured or
  drawn for it (saved, as `bypass`).
- **Drawing on its own** (`diag.js`; owner): each display is a canvas on its own compositor layer
  (`contain`, `will-change`), drawn in diag.js's own animation loop, so a frame repaints that
  canvas only. Readouts change four times a second; the correlation marker moves by transform.
  Smooth (owner): what is drawn follows a playhead 40 ms behind the newest sound, moving with
  real time, so each refresh shows a little more instead of a jump per 20 ms lump.
- **Waveform**: L and R, 50 ms, steady (each frame starts at a rising zero crossing of the mid),
  each pixel column's lowest and highest sample, L and R the same weight and added (white where
  they agree; owner: green was lost under magenta); the peak, in the display's corner. Triggered on
  the mid filtered below ~150 Hz, with hysteresis (owner saw it jump half a wave: a ripple at the
  bottom of a bass wave crossing zero; simulated, 251 of 260 frames jumped before, 0 after).
  Controls (owner; on the card and its panel; saved, and a profile counts them): **Stereo |
  Mono** (Mono: one trace of the two combined); **Smooth** (two passes of a centred 0.67 ms average,
  a soft filter fading from ~600 Hz up, and one line through each column's average: crests
  round off, the shape stays, nothing shifts; owner: at half a millisecond spikes still showed); **Zoom**,
  on unless turned off (fitted to the loudest recent peak of what is drawn, up at once, down over
  1.5 s, at most ×250; owner: a song at −12 dB used a third of the box; off: the edge is 0 dB).
  The cards are 20 cells wide for these four controls.
- **Silence is silence**: a card with nothing plugged in sleeps and sends nothing; after 0.3 s
  without sound the pictures draw nothing new and the readouts say "silent". A card drawn anew
  (every state) keeps its canvas, so it never replays the last sound (owner saw "blips").
- **Stereo** (as Ozone's Imager, polar sample): each sample a dot, its angle how far it leans
  (mono straight up, L and R on the 45° lines, out of phase flat), its distance its level against
  the loudest of the last second (zoomed to fit, down over ~1.5 s; owner: a song at −12 dB was a
  small blob, and a dB scale looked inverted, loud near-mono piled at the rim); dots
  fade by time (0.15 s constant, owner: a little longer than first; in steps of at least a
  quarter, or faint pixels round back up and leave a grey ghost), coloured by side, white only
  near mono itself. Below it two tiles (owner's pick D, as the Counter's): **correlation**, how
  alike L and R are (+1 mono, +0.3 … +0.9 normal music, around 0 very wide, below 0 parts cancel
  in mono), with what it means in words ("fine in mono", "very wide", "cancels in mono"; only clearly below 0, −0.05, cancels;
  owner: the number alone meant nothing); **width**, how far the picture fans out against its
  guide lines: 2·atan(side/mid RMS) as a share of 90°, so 0 % a vertical line (mono), 100 % out
  to the L and R lines (unrelated), 200 % flat (fully out of phase), never more; a sine with L
  and R φ apart reads φ/90° (checked at 0, 45, 90, 135, 180°: 0, 50, 100, 150, 200 %). Owner:
  the dots' spread (faint noise counted as much as sound) read 168 % beside a narrow picture,
  and a plain side/mid ratio is unbounded ("what am I supposed to imagine under 240 %"). The
  width bar's colours stay put (green, white at the L/R tick, magenta for too wide; owner) and a
  cover slides off them; both small bars glide every frame (scaleX, the compositor only).
- **Counter**: dropouts on its path (the cards wired into it, through any number of wires, apps
  left out) and on the interface clocking the graph wherever it sits (when it drops out, every
  sound does; owner: 2 dropouts in Problems the Counter on the Bass EQ chain missed); one glitch counted once, as the highest card's new errors each second; none while
  PipeBoard's own change settles), counted all the time (owner's pick: it costs nothing); and
  clipping arriving here (a full-scale sample; a moment held at full scale counts once), only
  while a window shows the board. Totals, the clock time of the last of each, since when;
  Reset; saved with the card (`counts`).
  Correlation (−1 … +1) and width (the side's share of the energy).
- **Large**: a click on the display opens it in a panel docked at the bottom, as the EQ's (one
  at a time, moved by its header, closed by ✕, Esc or a click on the board).
- **Meter** (owner's pick B of four, both card and panel): segmented LED bars for L and R across
  the whole width, no numbers beside them (owner): −60 … 0 dB, green, yellow from −12, red from
  −3; lit to the RMS (300 ms), dimmer on to the peak (falling 20 dB/s), the peak held 1.5 s as one
  white segment (red at full scale). Loudness as streaming platforms measure it (ITU-R BS.1770:
  K-weighting with the standard's 48 kHz coefficients, 100 ms blocks): momentary (0.4 s) and
  short-term (3 s) LUFS in two tiles, with a word (quiet / moderate / streaming loud / very
  loud). Checked on every page audit against the standard's reference: a 997 Hz sine at −20 dBFS
  in both channels reads −20.0 LUFS, full scale in one −3.01 (DIAG.selftest).
- **Spectrum**: the EQ analyser's own picture (eq.js `analyser.binsOf`, shared: 4096-point FFT,
  240 log bins 20 Hz–20 kHz, +4.5 dB/octave tilt, peaks falling) of the two channels combined,
  coloured by level as a meter (green low, yellow, red near the top; owner); the loudest frequency (its real level, the tilt taken off) in the
  corner.
- Not built yet (from the owner's list): Silence watch.

## 9. Preview tooling

**The README's pictures** come from `scripts/readme.py` (`uv run --with playwright --with pillow
python scripts/readme.py [name ...]`): the shots' fixture boards and, for the overview, the
owner's board read-only (a snapshot of the live graph, a copy of the board file: diagnostic cards
hidden, Portals on, tidied). Animations step the page's fake wire sound through exact moments
(`PB.debug.liveAt`), so the motion is even. The latency picture uses the owner's board with plain names
(`PLAIN` in the script: the interface, the pedal, games and apps renamed) and Show latency on. The diagnostic pictures play a few seconds of real
music recorded passively from the owner's Waveform card (`.dev/readme-sound.raw`, not kept in
git). The owner's `diag.mp4` gets a GIF preview: GitHub does not play a video file from the
repository, so the README links the preview to it.

UI work is seen, not guessed. `./run.sh shots` renders the page in headless Chrome for each
scenario (board, small window, Pedal loop, problems open, wire being dragged, wire selected, add
menu, card menu with name history, renaming, zoomed out, empty), writes `.dev/shots/<name>.png`
and a contact sheet `.dev/shots/sheet.png`, and prints the page's self-audit: cards overlapping,
cards off screen, text clipped, menus or hints leaving the window, bar running into the problem
button, NaN in a wire, wires not drawn, console errors. Exit code = faulty shots.

Scenarios come from `tests/fixtures/scenarios.py`: the user's graph as captured
(`scripts/capture_fixture.py` refreshes it), plus a Pedal effect loop and a graph with planted
problems. `./run.sh --fixture pedal` serves one in a real window, clickable, without touching
PipeWire. Add a shot to `SHOTS` in `scripts/shots.py` for any new UI state.

## 10. Status

Built: the board model and problems (M2), the board UI and preview tooling (M3), the live backend
(M4), profiles and missing devices (M5), virtual devices (M6), gain (M7), plugin nodes (M8),
launch at login, tray and take-over (M9), live wires (M10), the EQ (M11), routing around EQs and
plugins on quit (M12). Under consideration: a C# port, after measuring where the app spends its
time (no audio passes through the app's own code: PipeWire and Carla do the processing).
