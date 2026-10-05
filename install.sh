#!/usr/bin/env bash
# Installs Ixel MAT into its own virtualenv and puts an `ixel` command on PATH.
#   From a checkout:  ./install.sh
#   Standalone:       bash install.sh   (clones IXEL_REPO_URL first)
set -euo pipefail

REPO_URL="${IXEL_REPO_URL:-https://github.com/OpenIxelAI/ixel-mat.git}"
BRANCH="${IXEL_BRANCH:-main}"
INSTALL_ROOT="${IXEL_INSTALL_ROOT:-$HOME/.local/share/ixel-mat}"
BIN_DIR="${IXEL_BIN_DIR:-$HOME/.local/bin}"
REPO_DIR="$INSTALL_ROOT/repo"
VENV_DIR="$INSTALL_ROOT/.venv"
WRAPPER_PATH="$BIN_DIR/ixel"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing required command: $1" >&2
    os_hint "$1" >&2
    exit 1
  }
}

os_hint() {
  # One line telling the user how to get what's missing on this OS.
  local what="$1" id="" like=""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    case "$what" in
      python) echo "  Install it with Homebrew:  brew install python@3.13   (https://brew.sh)" ;;
      git)    echo "  Install it with:  xcode-select --install   or   brew install git" ;;
    esac
    return
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && echo "${ID:-}")"
    like="$(. /etc/os-release && echo "${ID_LIKE:-}")"
  fi
  case " $id $like " in
    *" debian "*|*" ubuntu "*)
      case "$what" in
        python) echo "  Install it with:  sudo apt install python3 python3-venv" ;;
        venv)   echo "  Install it with:  sudo apt install python3-venv   (or python3.X-venv for your version)" ;;
        git)    echo "  Install it with:  sudo apt install git" ;;
      esac ;;
    *" fedora "*|*" rhel "*|*" centos "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo dnf install python3" ;;
        git)         echo "  Install it with:  sudo dnf install git" ;;
      esac ;;
    *" arch "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo pacman -S python" ;;
        git)         echo "  Install it with:  sudo pacman -S git" ;;
      esac ;;
    *" suse "*|*" opensuse "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo zypper install python313   (or python311)" ;;
        git)         echo "  Install it with:  sudo zypper install git" ;;
      esac ;;
    *" alpine "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo apk add python3" ;;
        git)         echo "  Install it with:  sudo apk add git" ;;
      esac ;;
    *)
      echo "  Install $what with your system's package manager." ;;
  esac
}

python_ok() {
  # 3.10 or newer, and a final release: libraries Ixel needs can break on alphas and release candidates
  "$1" -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 10) and v.releaselevel == "final" else 1)' \
    >/dev/null 2>&1
}

pick_python() {
  # The system's python3 first when it's new enough (best supported by prebuilt
  # packages), then the newest versioned one, so an old default (macOS's 3.9)
  # doesn't stop us; Homebrew paths too, since GUI-launched shells often lack them.
  local candidate
  if [[ -n "${IXEL_PYTHON:-}" ]]; then
    if python_ok "$IXEL_PYTHON"; then
      command -v "$IXEL_PYTHON"
      return
    fi
    echo "error: IXEL_PYTHON=$IXEL_PYTHON isn't a final release of Python 3.10 or newer." >&2
    exit 1
  fi
  for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      command -v "$candidate"
      return
    fi
  done
  echo "error: Ixel MAT needs Python 3.10 or newer (a final release, not an alpha or release candidate)." >&2
  if command -v python3 >/dev/null 2>&1; then
    found_version="$(python3 --version 2>/dev/null || true)"
    if [ -n "$found_version" ]; then
      echo "  Found $found_version at $(command -v python3), which isn't usable (too old, or a pre-release)." >&2
    else
      echo "  Found $(command -v python3), but it doesn't run." >&2
    fi
  fi
  os_hint python >&2
  echo "  Or set IXEL_PYTHON=/path/to/python3.10+ and run this again." >&2
  exit 1
}

append_path_hint() {
  if [[ "${IXEL_SKIP_PATH_UPDATE:-0}" == "1" ]]; then
    return
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*) return ;;
  esac

  local shell_name profile_line target_file=""
  shell_name="$(basename "${SHELL:-}")"
  profile_line="export PATH=\"$BIN_DIR:\$PATH\""

  case "$shell_name" in
    zsh) target_file="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash)
      # macOS Terminal opens login shells, which read ~/.bash_profile, not ~/.bashrc
      if [[ "$(uname -s)" == "Darwin" ]]; then target_file="$HOME/.bash_profile"; else target_file="$HOME/.bashrc"; fi ;;
    fish)
      target_file="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/ixel-mat.fish"
      profile_line="fish_add_path \"$BIN_DIR\"" ;;
  esac

  if [[ -n "$target_file" ]]; then
    mkdir -p "$(dirname "$target_file")"
    touch "$target_file"
    if ! grep -Fq "$profile_line" "$target_file"; then
      printf '\n# Added by Ixel MAT installer\n%s\n' "$profile_line" >> "$target_file"
      echo "Added $BIN_DIR to PATH in $target_file"
    fi
  fi
}

linux_window_hint() {
  # One line on getting GTK and WebKit for the system's Python, which Ixel's own window on Linux uses
  local id="" like=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && echo "${ID:-}")"
    like="$(. /etc/os-release && echo "${ID_LIKE:-}")"
  fi
  case " $id $like " in
    *" debian "*|*" ubuntu "*) echo "  For Ixel's own window:  sudo apt install python3-gi gir1.2-webkit2-4.1" ;;
    *" fedora "*|*" rhel "*)   echo "  For Ixel's own window:  sudo dnf install python3-gobject webkit2gtk4.1" ;;
    *" arch "*)                echo "  For Ixel's own window:  sudo pacman -S python-gobject webkit2gtk-4.1" ;;
    *)                         echo "  For Ixel's own window, install PyGObject and WebKit2GTK for the system's python3." ;;
  esac
}

desktop_taken() {
  # Is there an app menu entry of this name that isn't Ixel's, in your menu folder or a system one (where yours
  # would hide it)?
  local dir IFS=:
  for dir in "${XDG_DATA_HOME:-$HOME/.local/share}" ${XDG_DATA_DIRS:-/usr/local/share:/usr/share}; do
    if [[ -e "$dir/applications/$1" || -L "$dir/applications/$1" ]] && \
       ! grep -q -- "-m ixel_mat app" "$dir/applications/$1" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

install_app() {
  # Ixel as an app: Ixel.app in ~/Applications on a Mac, an Ixel entry in the app menu on Linux. `ixel update`
  # makes it again, so it follows the install; IXEL_SKIP_APP_ENTRY=1 (trial installs) leaves both alone.
  if [[ "${IXEL_SKIP_APP_ENTRY:-0}" == "1" ]]; then
    return
  fi
  local python="$VENV_DIR/bin/python"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local made kind name
    # make-app.sh prints the kind of window, then where the app went: Ixel.app, or Ixel MAT.app beside
    # an Ixel.app of your own
    if made="$(bash "$SOURCE_DIR/macos/make-app.sh" "$python" "$HOME/Applications" "$PATH")"; then
      { IFS= read -r kind; IFS= read -r APP_PATH || true; } <<<"$made"
      APP_PATH="${APP_PATH:-$HOME/Applications/Ixel.app}"
      name="$(basename "$APP_PATH" .app)"
      if [[ "$kind" == "native" ]]; then
        APP_NOTE="$name in ~/Applications (or: ixel app)"
      else
        APP_NOTE="$name in ~/Applications, in Chrome or Edge's app mode (xcode-select --install, then ixel update, for the native window)"
      fi
      if [[ "$name" != "Ixel" ]]; then
        APP_NOTE="$APP_NOTE"$'\n'"  Named $name because you have an Ixel.app of your own, which is left as it is."
      fi
    else
      echo "warning: couldn't make Ixel.app; ixel app still works" >&2
    fi
    return
  fi
  local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications" icon entry=ixel name=Ixel
  # Never over (or hiding) a menu entry that isn't Ixel's: beside one, it's ixel-mat.desktop
  if desktop_taken ixel.desktop; then
    if desktop_taken ixel-mat.desktop; then
      echo "warning: your app menu already has ixel.desktop and ixel-mat.desktop entries that aren't Ixel's; Ixel isn't added to it (ixel app still works)" >&2
      return
    fi
    entry=ixel-mat name="Ixel MAT"
    if [[ -f "$apps/ixel.desktop" ]] && grep -q -- "-m ixel_mat app" "$apps/ixel.desktop"; then
      rm -f "$apps/ixel.desktop"  # an earlier install's, hiding a system entry of that name
    fi
  elif [[ -f "$apps/ixel-mat.desktop" ]] && grep -q -- "-m ixel_mat app" "$apps/ixel-mat.desktop"; then
    rm -f "$apps/ixel-mat.desktop"  # back under its own name
  fi
  icon="$("$python" -I -c 'import ixel_mat, os; print(os.path.join(os.path.dirname(ixel_mat.__file__), "assets", "ixel.png"))')"
  mkdir -p "$apps"
  cat > "$apps/$entry.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$name
Comment=Ask your AI models as a panel
Exec="$python" -I -m ixel_mat app
Icon=$icon
Terminal=false
Categories=Development;Utility;
StartupWMClass=Ixel
DESKTOP
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$apps" 2>/dev/null || true
  fi
  APP_NOTE="$name in your app menu (or: ixel app)"
  APP_PATH="$apps/$entry.desktop"
  if ! "$python" -I -c 'import sys; from ixel_mat.gui.window import linux_window_command; sys.exit(0 if linux_window_command() else 1)' 2>/dev/null; then
    APP_NOTE="$APP_NOTE, in Chrome's app mode or your browser for now"$'\n'"$(linux_window_hint)"
  fi
}

PYTHON_BIN="$(pick_python)"
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

# Install the checkout this script lives in, if it is one; otherwise clone.
SCRIPT_DIR=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/ixel_mat" ]]; then
  SOURCE_DIR="$SCRIPT_DIR"
else
  need_cmd git
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch origin
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
  else
    rm -rf "$REPO_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
  fi
  SOURCE_DIR="$REPO_DIR"
fi

echo "Using $("$PYTHON_BIN" --version 2>&1) at $PYTHON_BIN"
if [[ "${IXEL_USE_UV:-1}" != "0" ]] && command -v uv >/dev/null 2>&1; then
  # uv is faster and doesn't need the distro's python3-venv package
  uv venv --quiet --allow-existing --python "$PYTHON_BIN" "$VENV_DIR"
  uv pip install --quiet --python "$VENV_DIR/bin/python" --upgrade "$SOURCE_DIR"
else
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1; then
    rm -rf "$VENV_DIR"
    echo "error: $PYTHON_BIN can't create virtual environments (the venv module is missing)." >&2
    os_hint venv >&2
    exit 1
  fi
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$SOURCE_DIR"
fi
# Import everything a review uses, not just the entry point: a broken dependency
# should fail the install, not the first question.
if ! import_error="$("$VENV_DIR/bin/python" -c "import ixel_mat.agents.http, ixel_mat.mcp_server, ixel_mat.gui.server" 2>&1)"; then
  echo "error: Ixel MAT didn't finish installing: its libraries don't load on $("$PYTHON_BIN" --version 2>&1)." >&2
  printf '%s\n' "$import_error" | tail -n 3 | sed 's/^/  /' >&2
  echo "  Try another Python:  IXEL_PYTHON=/path/to/python3.12 bash \"$SOURCE_DIR/install.sh\"" >&2
  exit 1
fi

cat > "$WRAPPER_PATH" <<WRAPPER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/ixel" "\$@"
WRAPPER
chmod +x "$WRAPPER_PATH"

append_path_hint
APP_NOTE=""
APP_PATH=""
install_app

# Last, so it's only written once everything above worked: where this install came from and the
# commit it installed, for `ixel update` (which installs again if the checkout has moved on since)
COMMIT=""
if command -v git >/dev/null 2>&1; then
  COMMIT="$(git -C "$SOURCE_DIR" rev-parse --verify -q HEAD 2>/dev/null || true)"
fi
"$VENV_DIR/bin/python" -c 'import json, sys; info = {"source": sys.argv[2], "install_root": sys.argv[3], "bin_dir": sys.argv[4], "installer": "install.sh"}; info.update(zip(["commit"], filter(None, sys.argv[5:]))); json.dump(info, open(sys.argv[1], "w", encoding="utf-8"))' \
  "$INSTALL_ROOT/install.json" "$SOURCE_DIR" "$INSTALL_ROOT" "$BIN_DIR" "$COMMIT"

echo
echo "Ixel MAT installed from $SOURCE_DIR"
echo "Command: $WRAPPER_PATH"
if [[ -n "$APP_NOTE" ]]; then
  echo "App:     $APP_NOTE"
fi
echo "Next:    ixel setup"
echo "Update:  ixel update"
echo "Remove:  rm -rf \"$INSTALL_ROOT\" \"$WRAPPER_PATH\"${APP_PATH:+ \"$APP_PATH\"}"
echo "If your shell cannot find 'ixel' yet, restart the shell or run:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
