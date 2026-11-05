#!/usr/bin/env bash
# Install PipeBoard for this user: check what it needs, set it up in your home folder, add it to
# your app menu and start it at login (in the tray; off again from its tray menu). Nothing outside
# your home is changed unless you agree to install missing system packages (that step asks, then
# uses sudo).
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

# ---- how it looks: numbered steps, ✓ / ○ / ! per line, colour only on a terminal -------------
if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
  B=$'\033[1m' D=$'\033[2m' R=$'\033[0m' G=$'\033[32m' Y=$'\033[33m' E=$'\033[31m' A=$'\033[38;5;79m'
else
  B='' D='' R='' G='' Y='' E='' A=''
fi
STEP=0 STEPS=4
banner() { printf '\n  %s◉ PipeBoard%s  %s%s%s\n  %s──────────────────────────────%s\n' "$A$B" "$R" "$D" "$1" "$R" "$D" "$R"; }
step()   { STEP=$((STEP + 1)); printf '\n  %s%d/%d%s  %s%s%s\n' "$D" "$STEP" "$STEPS" "$R" "$B" "$*" "$R"; }
ok()     { printf '     %s✓%s %s\n' "$G" "$R" "$*"; }
skip()   { printf '     %s○%s %s\n' "$D" "$R" "$*"; }  # optional, and not there
warn()   { printf '     %s!%s %s\n' "$Y" "$R" "$*"; }
note()   { printf '       %s%s%s\n' "$D" "$*" "$R"; }
finish() { printf '\n  %s✓ %s%s\n' "$G$B" "$*" "$R"; }
after()  { printf '    %s%s%s\n' "$D" "$*" "$R"; }  # a line under the final one
die()    { printf '\n  %s✗ %s%s\n\n' "$E" "$*" "$R" >&2; exit 1; }
has()    { command -v "$1" >/dev/null 2>&1; }
# A yes/no question, read from the terminal (also under `curl … | bash`). No terminal: no.
ask()  {
  local a
  { printf '       %s [y/N] ' "$1" >/dev/tty && read -r a </dev/tty; } 2>/dev/null || return 1
  [[ $a == [yY]* ]]
}
# busy <label> <command…>: a "⋯ label" line while it runs, turned into ✓ when it is done
busy() {
  local label=$1 status=0; shift
  [[ -t 1 ]] && printf '     %s⋯ %s%s' "$D" "$label" "$R"
  "$@" || status=$?
  [[ -t 1 ]] && printf '\r\033[K'
  (( status == 0 )) && ok "$label"
  return $status
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
  if [[ -z $cmd ]]; then note "Install $1 with your package manager."; return 1; fi
  note "To install $1: $cmd"
  if ask "Run that now?"; then $cmd; else return 1; fi
}

# ---- uninstall --------------------------------------------------------------------------------
uninstall() {
  STEPS=2
  banner "uninstall"
  if pgrep -f -- "$SRC/.venv/bin/python3? -m pipeboard|$SRC/.venv/bin/pipeboard" >/dev/null 2>&1; then
    die "PipeBoard is running from $SRC. Quit it first (tray icon → Quit PipeBoard)."
  fi
  step "Removing PipeBoard"
  if [[ -f $CONFIG/systemd/user/pipeboard.service ]]; then
    systemctl --user disable pipeboard.service >/dev/null 2>&1 || true
    rm -f "$CONFIG/systemd/user/pipeboard.service"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Launch at login"
  fi
  rm -f "$BIN/pipeboard" "$SHARE/applications/$APP_ID.desktop" \
        "$SHARE"/icons/hicolor/scalable/apps/pipeboard{,-tray,-tray-alert}.svg
  ok "Command, app-menu entry and icons"

  step "Your files"
  if [[ -d $CLONE ]]; then
    if ask "Delete the downloaded code in $CLONE?"; then
      rm -rf "$CLONE"; rmdir "$SHARE/pipeboard" 2>/dev/null || true; ok "Code removed"
    else skip "Code kept  ${D}$CLONE${R}"
    fi
  fi
  if [[ -d $CONFIG/pipeboard ]]; then
    if ask "Delete your boards and profiles in $CONFIG/pipeboard?"; then
      rm -rf "$CONFIG/pipeboard"; ok "Boards and profiles removed"
    else skip "Boards and profiles kept  ${D}$CONFIG/pipeboard${R}"
    fi
  fi
  finish "PipeBoard is removed."
  after "Virtual devices it made stay in PipeWire until PipeWire restarts (or you log out and in)."
  echo
}

# ---- install ----------------------------------------------------------------------------------
check_system() {
  step "Checking your system"
  [[ $(uname -s) == Linux ]] || die "PipeBoard runs on Linux (PipeWire)."
  local missing=()
  for c in pipewire pw-dump pw-link pw-cli pw-cat wpctl stdbuf; do has "$c" || missing+=("$c"); done
  if (( ${#missing[@]} )); then
    warn "PipeWire's tools: missing ${missing[*]}"
    offer "PipeWire's tools and WirePlumber" "pipewire pipewire-bin wireplumber coreutils" \
          "pipewire pipewire-utils wireplumber coreutils" "pipewire wireplumber coreutils" \
      || die "PipeBoard needs PipeWire's tools and WirePlumber."
  else
    ok "PipeWire and WirePlumber"
  fi
  pgrep -x pipewire >/dev/null 2>&1 \
    || warn "PipeWire is not running: PipeBoard has nothing to show until it is."

  local browser=""
  for b in google-chrome google-chrome-stable chromium chromium-browser; do has "$b" && { browser=$b; break; }; done
  if [[ -n $browser ]]; then
    ok "Window  ${D}$browser${R}"
  else
    skip "Window: no Chrome or Chromium"
    note "PipeBoard still runs: open http://127.0.0.1:7870 in any browser."
  fi

  if [[ -f /usr/share/carla/carla_backend.py ]] && has pw-jack; then
    ok "Plugins  ${D}Carla, pw-jack${R}"
  else
    skip "Plugins (optional): LV2 and VST3 plugins need Carla and pw-jack"
    offer "Carla and pw-jack" "carla pipewire-jack" "Carla pipewire-jack-audio-connection-kit" \
          "carla pipewire-jack" || note "Skipped: everything but plugins works without them."
  fi
  # (grep reads it all: `grep -q` stops at the first match, ldconfig gets SIGPIPE, and under
  # pipefail the check failed on a system that had both)
  if has cc && ldconfig -p 2>/dev/null | grep 'libspeexdsp\.so\.1' >/dev/null; then
    ok "Noise Suppression  ${D}C compiler, libspeexdsp${R}"
  else
    skip "Noise Suppression (optional): built once with a C compiler, against libspeexdsp"
    offer "gcc and libspeexdsp" "gcc libspeexdsp1" "gcc speexdsp" "gcc speexdsp" \
      || note "Skipped: everything but Noise Suppression works without them."
  fi
  if has kdialog || has zenity || has notify-send; then
    ok "Desktop messages"
  else
    skip "Desktop messages (optional): kdialog, zenity or notify-send"
  fi
}

get_uv() {
  if has uv; then ok "uv"; return; fi
  [[ -x $BIN/uv ]] && { export PATH="$BIN:$PATH"; ok "uv"; return; }
  skip "uv: not installed"
  note "PipeBoard runs with uv, a Python tool that also fetches the right Python."
  ask "Install uv for your user from astral.sh?" || die "uv is needed: https://docs.astral.sh/uv/"
  has curl || die "curl is needed to install uv."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$BIN:$PATH"
  has uv || die "uv did not install."
  ok "uv"
}

get_code() {
  if [[ $SRC == "$CLONE" ]]; then
    has git || die "git is needed to download PipeBoard."
    if [[ -d $CLONE/.git ]]; then
      busy "Updated  ${D}$CLONE${R}" git -C "$CLONE" pull --ff-only --quiet \
        || die "Could not update $CLONE (git pull)."
    else
      mkdir -p "$(dirname "$CLONE")"
      busy "Downloaded  ${D}$CLONE${R}" git clone --quiet --depth 1 "$REPO" "$CLONE" \
        || die "Could not download PipeBoard."
    fi
  else
    ok "From  ${D}$SRC${R}"
  fi
}

setup() {
  step "Setting up"
  busy "Python environment" bash -c 'cd "$1" && uv sync --quiet' _ "$SRC" \
    || die "Could not set up PipeBoard's Python environment (uv sync)."
  mkdir -p "$BIN"
  printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$SRC/run.sh" > "$BIN/pipeboard"
  chmod +x "$BIN/pipeboard" "$SRC/run.sh"
  (cd "$SRC" && uv run --quiet python -c "from pipeboard.__main__ import install_entry; install_entry()")
  has update-desktop-database && update-desktop-database "$SHARE/applications" >/dev/null 2>&1 || true
  ok "Command ${B}pipeboard${R} and the app-menu entry"
  case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH: start it from the app menu, or add it." ;; esac
}

install() {
  banner "install"
  check_system
  step "Getting PipeBoard"
  get_uv
  get_code
  setup
  # On by default (owner): PipeBoard lives in the tray and loads your launch profile at login.
  step "Launch at login"
  (cd "$SRC" && uv run --quiet python -c \
    "import asyncio; from pipeboard import autostart; raise SystemExit(asyncio.run(autostart.set_enabled(True)))") \
    || die "Could not set up launch at login."
  ok "On  ${D}in the tray, with your launch profile${R}"
  note "Turn it off in PipeBoard's tray menu."

  finish "PipeBoard is installed."
  after "Start it from your app menu, or run: pipeboard"
  after "Remove it with: $SRC/install.sh --uninstall"
  echo
}

case "${1:-}" in
  --uninstall) uninstall ;;
  ""|--install) install ;;
  -h|--help) echo "Usage: install.sh [--uninstall]  (see the top of the file)" ;;
  *) die "unknown option $1 (try --help)" ;;
esac
