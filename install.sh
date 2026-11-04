#!/usr/bin/env bash
# Install PipeBoard for this user: check what it needs, set it up in your home folder, and add
# it to your app menu. Nothing outside your home is changed unless you agree to install missing
# system packages (that step asks, then uses sudo).
#
#   ./install.sh                 from a clone of the repository
#   curl -fsSL https://raw.githubusercontent.com/TominoCZ/PipeBoard/main/install.sh | bash
#   ./install.sh --uninstall     remove it again (asks before deleting your boards and profiles)
#
# Running it again updates an installation made by the one-liner (git pull) and refreshes the rest.
set -euo pipefail

REPO=https://github.com/TominoCZ/PipeBoard.git
SHARE=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
CLONE=$SHARE/pipeboard/app            # where the one-liner puts the code
BIN=$HOME/.local/bin
APP_ID=chrome-127.0.0.1__pipeboard_-Default  # the window's id; the app uses the same (__main__.py)

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mpipeboard install: %s\033[0m\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }
# A yes/no question, read from the terminal (also under `curl … | bash`). No terminal: no.
ask()  {
  local a
  { printf '  %s [y/N] ' "$1" >/dev/tty && read -r a </dev/tty; } 2>/dev/null || return 1
  [[ $a == [yY]* ]]
}

# ---- where the code is ------------------------------------------------------------------------
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [[ -n $here && -f $here/pyproject.toml ]] && grep -q '^name = "pipeboard"' "$here/pyproject.toml"; then
  SRC=$here     # run from a clone
else
  SRC=$CLONE    # run by the one-liner
fi

# ---- the distribution's package names ---------------------------------------------------------
distro() {
  local id like
  id=$(. /etc/os-release 2>/dev/null && echo "${ID:-}")
  like=$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-}")
  case " $id $like " in
    *" arch "*) echo arch ;;
    *" fedora "*|*" rhel "*) echo fedora ;;
    *" debian "*|*" ubuntu "*) echo debian ;;
    *) echo other ;;
  esac
}
install_cmd() {  # install_cmd <debian packages> <fedora packages> <arch packages>
  case $(distro) in
    debian) echo "sudo apt install $1" ;;
    fedora) echo "sudo dnf install $2" ;;
    arch)   echo "sudo pacman -S --needed $3" ;;
    *)      echo "" ;;
  esac
}
offer() {  # offer <what> <debian> <fedora> <arch>: show the command; run it if the user agrees
  local cmd; cmd=$(install_cmd "$2" "$3" "$4")
  if [[ -z $cmd ]]; then warn "Install $1 with your package manager."; return 1; fi
  note "To install $1: $cmd"
  if ask "Run that now?"; then $cmd; else return 1; fi
}

# ---- uninstall --------------------------------------------------------------------------------
uninstall() {
  say "Removing PipeBoard"
  if pgrep -f -- "$SRC/.venv/bin/python3? -m pipeboard|$SRC/.venv/bin/pipeboard" >/dev/null 2>&1; then
    die "PipeBoard is running from $SRC. Quit it first (tray icon → Quit PipeBoard)."
  fi
  if [[ -f $CONFIG/systemd/user/pipeboard.service ]]; then
    systemctl --user disable pipeboard.service >/dev/null 2>&1 || true
    rm -f "$CONFIG/systemd/user/pipeboard.service"
    systemctl --user daemon-reload 2>/dev/null || true
    note "Launch at login: removed"
  fi
  rm -f "$BIN/pipeboard" "$SHARE/applications/$APP_ID.desktop" \
        "$SHARE"/icons/hicolor/scalable/apps/pipeboard{,-tray,-tray-alert}.svg
  note "Command, app-menu entry and icons: removed"
  if [[ -d $CLONE ]] && ask "Delete the downloaded code in $CLONE?"; then
    rm -rf "$CLONE"; rmdir "$SHARE/pipeboard" 2>/dev/null || true; note "Code: removed"
  fi
  if [[ -d $CONFIG/pipeboard ]] && ask "Delete your boards and profiles in $CONFIG/pipeboard?"; then
    rm -rf "$CONFIG/pipeboard"; note "Boards and profiles: removed"
  fi
  note "Virtual devices PipeBoard made stay in PipeWire until it restarts (or log out and in)."
  say "Done."
}

# ---- install ----------------------------------------------------------------------------------
check_system() {
  say "Checking your system"
  [[ $(uname -s) == Linux ]] || die "PipeBoard runs on Linux (PipeWire)."
  local missing=()
  for c in pipewire pw-dump pw-link pw-cli pw-cat wpctl stdbuf; do has "$c" || missing+=("$c"); done
  if (( ${#missing[@]} )); then
    warn "Missing: ${missing[*]}"
    offer "PipeWire's tools and WirePlumber" "pipewire pipewire-bin wireplumber coreutils" \
          "pipewire pipewire-utils wireplumber coreutils" "pipewire wireplumber coreutils" \
      || die "PipeBoard needs PipeWire's tools and WirePlumber."
  else
    note "PipeWire and WirePlumber: ok"
  fi
  pgrep -x pipewire >/dev/null 2>&1 || warn "PipeWire is not running: PipeBoard will have nothing to show until it is."

  local browser=""
  for b in google-chrome google-chrome-stable chromium chromium-browser; do has "$b" && { browser=$b; break; }; done
  if [[ -n $browser ]]; then note "Window ($browser): ok"
  else warn "No Chrome or Chromium found: PipeBoard will still run; open http://127.0.0.1:7870 in any browser."
  fi

  if [[ -f /usr/share/carla/carla_backend.py ]] && has pw-jack; then
    note "Plugins (Carla, pw-jack): ok"
  else
    note "Optional: LV2 and VST3 plugins need Carla and pw-jack."
    offer "Carla and pw-jack" "carla pipewire-jack" "Carla pipewire-jack-audio-connection-kit" \
          "carla pipewire-jack" || note "Skipped: everything but plugins works without them."
  fi
  if has cc && ldconfig -p 2>/dev/null | grep -q 'libspeexdsp\.so\.1'; then
    note "Noise Suppression (a C compiler, libspeexdsp): ok"
  else
    note "Optional: the Noise Suppression node is built once with a C compiler, against libspeexdsp."
    offer "gcc and libspeexdsp" "gcc libspeexdsp1" "gcc speexdsp" "gcc speexdsp" \
      || note "Skipped: everything but Noise Suppression works without them."
  fi
  has kdialog || has zenity || has notify-send \
    || note "Optional: kdialog, zenity or notify-send shows PipeBoard's messages on the desktop."
}

get_uv() {
  if has uv; then return; fi
  [[ -x $BIN/uv ]] && { export PATH="$BIN:$PATH"; return; }
  say "PipeBoard runs with uv (a Python tool that also fetches the right Python)"
  ask "Install uv for your user from astral.sh?" || die "uv is needed: https://docs.astral.sh/uv/"
  has curl || die "curl is needed to install uv."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$BIN:$PATH"
  has uv || die "uv did not install."
}

get_code() {
  if [[ $SRC == "$CLONE" ]]; then
    has git || die "git is needed to download PipeBoard."
    if [[ -d $CLONE/.git ]]; then
      say "Updating PipeBoard in $CLONE"
      git -C "$CLONE" pull --ff-only --quiet
    else
      say "Downloading PipeBoard to $CLONE"
      mkdir -p "$(dirname "$CLONE")"
      git clone --quiet --depth 1 "$REPO" "$CLONE"
    fi
  else
    say "Installing PipeBoard from $SRC"
  fi
}

install() {
  check_system
  get_uv
  get_code
  (cd "$SRC" && uv sync --quiet) || die "Could not set up PipeBoard's Python environment (uv sync)."
  note "Python environment: ok"

  mkdir -p "$BIN"
  printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$SRC/run.sh" > "$BIN/pipeboard"
  chmod +x "$BIN/pipeboard" "$SRC/run.sh"
  (cd "$SRC" && uv run --quiet python -c "from pipeboard.__main__ import install_entry; install_entry()")
  has update-desktop-database && update-desktop-database "$SHARE/applications" >/dev/null 2>&1 || true
  note "Command 'pipeboard' and the app-menu entry: ok"
  case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH: start it from the app menu, or add it." ;; esac

  if ask "Start PipeBoard at login (in the tray, with your launch profile)?"; then
    (cd "$SRC" && uv run --quiet python -c \
      "import asyncio; from pipeboard import autostart; asyncio.run(autostart.set_enabled(True))")
    note "Launch at login: on (turn it off in PipeBoard's tray menu)"
  fi
  say "PipeBoard is installed. Start it from your app menu, or run: pipeboard"
  note "Remove it again with: $SRC/install.sh --uninstall"
}

case "${1:-}" in
  --uninstall) uninstall ;;
  ""|--install) install ;;
  -h|--help) echo "Usage: install.sh [--uninstall]  (see the top of the file)" ;;
  *) die "unknown option $1 (try --help)" ;;
esac
