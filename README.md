# PipeBoard

A calm node board for routing PipeWire audio, with effects built in.

The `qpwgraph` PipeWire GUI is cool and all, but I don't like how cluttered, unreadable, hard to navigate, and limiting it is.
I also used to use `EasyEffects` for a bass boost EQ for my headphones, but I found it limiting (at times even unstable) too.
Frustrated, I decided to build my own: route your audio easily, and add gain, EQ, dynamics or your own
plugins to any signal path.

![The board, live wires carrying the sound](docs/images/board1.gif)

## What it does

- **Route anything:** apps, virtual mixes, sound cards and USB devices are cards; drag a wire from
  an output to an input. Stereo, mono and single channels are handled for you.
- **Effects on any path:** Gain, an EQ with dynamic bands, Limiter, Compressor, Noise Gate,
  De-Esser and Noise Suppression, plus your own LV2 and VST3 plugins.
- **See the sound:** each wire carries a moving picture of its sound: broad waves for a kick drum,
  fine ripples for a hi-hat, glowing red where it clips. Every effect has a live panel too.
- **Virtual devices:** make microphones and speakers that apps can use.
- **Profiles:** save a routing; before loading one, see what it will change.
- **Problems, explained:** feedback loops, failed connections and one-sided stereo are listed,
  worst first.

![A microphone chain with Noise Suppression open](docs/images/board2.png)

## Portals

Long boards fold into rows with **Tidy**. A wire between rows becomes a pair of lettered portals,
so it doesn't cross the whole board. The arrow on each badge shows which way the sound goes.

![The board tidied into two rows, wires between them hidden behind portals A, B and C](docs/images/portals.png)

Hover a portal, or either card, to see its whole wire. Right-click any wire to make it a portal
yourself, or untick **Portals** next to Tidy to keep one row. Portals only change the drawing; the
sound still goes straight from A to B.

![Portal B hovered: its wire from the Limiter to the interface shows with its sound, the rest dim](docs/images/portals-hover.gif)

## Effects

| EQ with dynamic bands | Limiter |
|---|---|
| ![EQ](docs/images/effects/eq-dynamic.png) | ![Limiter](docs/images/effects/limiter.png) |
| **Compressor** | **Noise Gate** |
| ![Compressor](docs/images/effects/compressor.png) | ![Noise Gate](docs/images/effects/gate.png) |
| **De-Esser** | **Noise Suppression** |
| ![De-Esser](docs/images/effects/deesser.png) | ![Noise Suppression](docs/images/effects/denoise.png) |

Amber is what an effect turned down. Every effect card has a power button (bypass) and a gear
that opens its panel.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/TominoCZ/PipeBoard/main/install.sh | bash
```

Or from a clone: `./install.sh`. It checks what your system needs, offers the install command for
your distribution (it only uses sudo if you say yes), adds a `pipeboard` command and an
app-menu entry, and starts PipeBoard at login, in the tray (turn that off in the tray menu). Run it
again to update; `install.sh --uninstall` removes it. Everything it may ask
for is in [`docs/deps.md`](docs/deps.md).

Built for Linux with PipeWire and WirePlumber; the tray and window are made for KDE Plasma. It is
young software, so expect rough edges.

## Using it

- **Connect:** drag from an OUT socket to an IN socket. **Disconnect:** click a wire, then Delete;
  or right-click a card › Disconnect to unplug it completely.
- **Add:** right-click the board › Add node (effects, devices, apps). Drop a wire on empty board to
  add something already connected.
- **Numbers:** scroll or type, Enter applies, Esc cancels.
- **Move around:** drag to pan, scroll to zoom, **F** to fit, **Tidy** to lay out by signal flow
  (see [Portals](#portals)). **Animate** turns the moving wires on and off.
- **Tray:** closing the window keeps PipeBoard running. Launch at login is in the tray menu.

Your board is saved in `~/.config/pipeboard/`. The full design is in [`docs/PLAN.md`](docs/PLAN.md).

## Develop

```sh
./run.sh --fixture pedal   # a saved graph to click through; never touches PipeWire
./run.sh shots             # render every UI state to .dev/shots/ and audit the layout
./run.sh check             # the checks
```

## License

MIT, see [LICENSE](LICENSE).
