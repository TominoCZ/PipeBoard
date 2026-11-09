# deps.md — what a machine needs to run PipeBoard

`install.sh` checks these and offers to install what is missing (see the README).

Everything that has to be installed on the system, outside the Python packages `uv` fetches
from `pyproject.toml`. Package names are Ubuntu/Debian (this machine: Ubuntu, Plasma 6,
PipeWire 1.6.2). **Keep this file current:** a change that calls a new program, loads a new
library or relies on a new desktop service adds a row here in the same change.

## Required

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| PipeWire (as the sound server) with WirePlumber | `pipewire`, `pipewire-audio`, `wireplumber` | The graph PipeBoard routes | Nothing to show or route |
| `pw-dump`, `pw-link`, `pw-cli`, `pw-cat` | `pipewire-bin` | Reading and watching the graph; links; making devices and gains; gain levels; live wires capture | No board (pw-dump); no changes (others) |
| `chrt`, `renice` | `util-linux`, `bsdutils` | The Audio engine: PipeWire's audio thread priority and nice, set live | Priority is only set at PipeWire's next start |
| `stdbuf` | `coreutils` | Live wires: unbuffered `pw-cat` output; line-buffered `pw-cli` (it says when it is connected) | Live wires stutter in lumps; the first level sent waits 3 s |
| Python ≥ 3.12 and `uv` | `uv` (astral.sh) | Running the app; `uv` installs `aiohttp` and `dbus-fast` itself | Does not start |
| Google Chrome or Chromium | `google-chrome-stable` or `chromium` | The window (`--app` mode) | Opens in the default browser instead (also when Chrome cannot start: a library missing) |
| glibc (`libc.so.6`) | `libc6` | `prctl(PR_SET_PDEATHSIG)`: helper processes end with PipeBoard | Does not start (always present) |
| A systemd user session | `systemd` | Launch at login (`pipeboard.service`); taking over a service's devices; the Audio engine finding PipeWire's process (`systemctl --user show`) | Those features fail with a message |

## For the EQ node (M11)

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| `pipewire` (the binary, run as `pipewire -c <conf>`) | `pipewire-bin` | Each EQ runs as its own filter-chain process | The EQ does not start (a toast says why) |
| The filter-chain module | `libpipewire-0.3-modules` | The EQ's chain of filters | The EQ does not start |
| The built-in filter-graph plugin (`bq_raw` biquads) | `libspa-0.2-modules` | The EQ's filters, coefficients set live | The EQ does not start |
| `pw-cat`, `stdbuf` (see above) | | The spectrum behind the curve | No spectrum; the EQ works |

## For the Noise Suppression node

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| Speex's DSP library (`libspeexdsp.so.1`) | `libspeexdsp1` (comes with PipeWire's Pulse side) | Its noise suppression (`native/pb_denoise.c`) | It does not start (a toast says why) |
| A C compiler (`cc`) | `gcc` | Building that plugin once, into `~/.cache/pipeboard/` (no headers needed) | It does not start (a toast says why) |

## For the Limiter, Compressor and Noise Gate nodes

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| LSP's LADSPA plugins (`lsp-plugins-ladspa.so`: Limiter Stereo, Compressor Stereo, Gate Stereo; Dynamics Processor Mono for dynamic EQ bands), found in `LADSPA_PATH` or `/usr/lib/ladspa` and the like | `lsp-plugins-ladspa` (Easy Effects installs LSP too) | Their sound | They do not start (a toast says what to install) |
| `pipewire`, the filter-chain module, `pw-cat`, `stdbuf` (as the EQ) | | Its process; the live panel's capture | As the EQ |

## For plugin nodes (M8)

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| Carla 2.5.x: its Python backend (`/usr/share/carla/carla_backend.py`), `libcarla_standalone2.so`, `libcarla_utils.so`, `carla-bridge-native`, `carla-discovery-native` (in `/usr/lib/carla`) | `carla` (KXStudio's 2.5.10b here; Ubuntu's own 2.5.10 has the same files) | Hosting LV2/VST3 plugins, listing them | Add ▸ Plugin… lists nothing; no plugin starts (a toast says why) |
| `pw-jack` and PipeWire's JACK library | `pipewire-jack` | Carla's JACK engine runs on PipeWire | Plugins do not start |
| The plugins themselves | e.g. `lsp-plugins-lv2`, `zam-plugins`, `calf-plugins`, or VST3 bundles in `~/.vst3` | What there is to add | An empty picker |

Linux plugins only: Windows `.dll`/`.vst3` plugins would need Carla's Wine bridge
(`carla-bridge-win64`), which is not supported (user's decision).

## Desktop (KDE Plasma); optional

| Dependency | Package | Used for | Without it |
|---|---|---|---|
| A StatusNotifierItem host (Plasma's system tray) on the session D-Bus | `plasma-workspace` | The tray icon and its menu | Runs without a tray (logged) |
| KWin's scripting D-Bus interface | `kwin-wayland` / `kwin-x11` | Raising the open window when launched again from the tray | Opens a new window instead |
| `kdialog`, else `zenity`, else `notify-send` | `kdialog` / `zenity` / `libnotify-bin` | The "already running" and "port in use" messages | The message goes to the terminal only |

## Development only (not shipped)

| Dependency | Package | Used for |
|---|---|---|
| Headless Chrome/Chromium, driven by Playwright (`uv run --with playwright`, fetched on first use) | as above | `./run.sh shots`, `scripts/readme.py` |
| `ruff` (via `uvx`) | `uv` | Lint |
