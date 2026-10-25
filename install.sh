#!/bin/sh
# gsr-categories installer
#   curl -fsSL https://raw.githubusercontent.com/wildesPepega/gsr-categories/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/wildesPepega/gsr-categories/main/install.sh | sh -s -- --uninstall
#
# - hooks gsr-categorize into gpu-screen-recorder via -sc (sorts saved clips into game folders)
# - game mode: while a game runs, the replay records only the game window
# - turns off gsr-ui's own folder/file naming so the two don't fight over the clip
set -e
REPO_RAW="${GSR_CATEGORIES_RAW:-https://raw.githubusercontent.com/wildesPepega/gsr-categories/main}"
share="$HOME/.local/share/gsr-categories"
bin="$HOME/.local/bin"
wrapper="$bin/gpu-screen-recorder"
notify="$bin/gsr-notify"
script="$bin/gsr-categorize"
unit_dir="$HOME/.config/systemd/user"
unit="gsr-gamewatch.service"
gsr_ui_unit='app-gpu\x2dscreen\x2drecorder\x2dui@autostart.service'
marker="written by gsr-categories"

say() { printf '\033[1;32m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

uninstall() {
  systemctl --user disable --now "$unit" 2>/dev/null || true
  rm -f "$unit_dir/$unit"
  systemctl --user daemon-reload 2>/dev/null || true
  if [ -f "$wrapper" ] && grep -q "$marker" "$wrapper"; then
    if [ -f "$wrapper.bak" ]; then mv "$wrapper.bak" "$wrapper"; else rm -f "$wrapper"; fi
  fi
  [ -f "$notify" ] && grep -q "$marker" "$notify" && rm -f "$notify"
  rm -f "$script"
  rm -rf "$share" "${XDG_RUNTIME_DIR:-/tmp}/gsr-categories"
  qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript gsr-categories-windows >/dev/null 2>&1 || true
  say "uninstalled. Your config (~/.config/gsr-categories) and clips were kept."
  say "Re-enable the gsr-ui naming/folder options in its settings if you want them back."
}

[ "$1" = "--uninstall" ] && { uninstall; exit 0; }

# --- checks ------------------------------------------------------------------
[ -x /usr/bin/gpu-screen-recorder ] || die "gpu-screen-recorder not found in /usr/bin"
command -v gsr-ui >/dev/null || warn "gsr-ui not found: sorting works, game mode needs gpu-screen-recorder-ui"
command -v python3 >/dev/null || die "python3 is required"
python3 -c "import gi; gi.require_version('Gio', '2.0')" 2>/dev/null \
  || warn "python-gobject missing: game mode won't run (Arch: python-gobject, Debian/Ubuntu: python3-gi, Fedora: python3-gobject)"
command -v qdbus6 >/dev/null || command -v qdbus >/dev/null \
  || warn "qdbus6 missing: window detection falls back to xprop (no Wayland/gamescope windows, no game mode)"

# --- files -------------------------------------------------------------------
mkdir -p "$share" "$bin" "$unit_dir"
here=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/gsr-categorize.py" ] && [ -f "$here/install.sh" ]; then
  cp "$here/gsr-categorize.py" "$share/gsr-categorize.py"
else
  command -v curl >/dev/null || die "curl is required"
  curl -fsSL "$REPO_RAW/gsr-categorize.py" -o "$share/gsr-categorize.py.new" || die "download failed"
  mv "$share/gsr-categorize.py.new" "$share/gsr-categorize.py"
fi
chmod +x "$share/gsr-categorize.py"
ln -sf "$share/gsr-categorize.py" "$script"
say "installed $script"

# --- gpu-screen-recorder wrapper ----------------------------------------------
# Keep extra args other tools put into an existing wrapper (e.g. a -p plugin)
extra=""
if [ -f "$wrapper" ]; then
  grep -q "$marker" "$wrapper" || cp "$wrapper" "$wrapper.bak"
  if grep -q '^extra=' "$wrapper"; then
    extra=$(sed -n "s|^extra='\(.*\)'$|\1|p" "$wrapper" | tail -1)
  else
    extra=$(sed -n 's|^exec "/usr/bin/gpu-screen-recorder" "\$@" \(.*\)$|\1|p' "$wrapper" | tail -1 \
      | sed 's|-sc "[^"]*"||; s|"||g; s|^ *||; s| *$||')
  fi
fi
cat > "$wrapper" <<EOF
#!/bin/sh
# $marker (previous version, if any: gpu-screen-recorder.bak)
real=/usr/bin/gpu-screen-recorder
cat="$script"
state="\${XDG_RUNTIME_DIR:-/tmp}/gsr-categories/game"
extra='$extra'
case "\$*" in
  *--info*|*--list*|*--version*|*-h*|*--help*) exec "\$real" "\$@" ;;
esac

# Game mode: the replay captures the game window (desktop portal) instead of the monitor
case " \$* " in
  *" -r "*)
    if [ -f "\$state" ]; then
      first=1; drop=0; repl=0
      for a do
        if [ \$first = 1 ]; then set --; first=0; fi
        if [ \$drop = 1 ]; then drop=0; continue; fi
        if [ \$repl = 1 ]; then repl=0; set -- "\$@" portal; continue; fi
        case "\$a" in
          -w) repl=1; set -- "\$@" -w ;;
          -restore-portal-session|-portal-session-token-filepath) drop=1 ;;
          *) set -- "\$@" "\$a" ;;
        esac
      done
      set -- "\$@" -restore-portal-session yes -portal-session-token-filepath "\$("\$cat" --portal-token)"
    fi ;;
esac

case " \$* " in
  *" -sc "*) ;;
  *) set -- "\$@" -sc "\$cat" ;;
esac
# shellcheck disable=SC2086
exec "\$real" "\$@" \$extra
EOF
chmod +x "$wrapper"
say "wrapper written: $wrapper${extra:+ (kept: $extra)}"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) warn "$bin is not in PATH: gsr-ui won't pick up the wrapper" ;;
esac

# --- notifications --------------------------------------------------------------
# While the watcher switches modes, hide gsr-ui's "Replay stopped" and replace its
# "Started replaying ..." with the text the watcher left in $note
if [ -x /usr/bin/gsr-notify ]; then
  cat > "$notify" <<EOF
#!/bin/sh
# $marker
EOF
  cat >> "$notify" <<'EOF'
real=/usr/bin/gsr-notify
note="${XDG_RUNTIME_DIR:-/tmp}/gsr-categories/notify"
if [ -f "$note" ] && [ $(( $(date +%s) - $(stat -c %Y "$note") )) -le 8 ]; then
  text=""; prev=""
  for a do [ "$prev" = --text ] && text=$a; prev=$a; done
  case "$text" in
    *[Ss]topped*) exit 0 ;;
    *[Ss]tarted*)
      msg=$(cat "$note"); rm -f "$note"
      first=1; repl=0
      for a do
        if [ $first = 1 ]; then set --; first=0; fi
        if [ $repl = 1 ]; then repl=0; set -- "$@" "$msg"; continue; fi
        [ "$a" = --text ] && repl=1
        set -- "$@" "$a"
      done ;;
  esac
fi
exec "$real" "$@"
EOF
  chmod +x "$notify"
fi

# --- game mode watcher ------------------------------------------------------------
cat > "$unit_dir/$unit" <<EOF
[Unit]
Description=gsr-categories game mode (replay records only the game window)
After=graphical-session.target $gsr_ui_unit
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/env python3 $script --watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
if systemctl --user daemon-reload && systemctl --user enable "$unit" >/dev/null 2>&1 \
    && systemctl --user restart "$unit"; then
  say "game mode watcher running ($unit)"
else
  warn "could not start $unit (no systemd user session?): run '$script --watch' at login for game mode"
fi

# --- gsr-ui settings ---------------------------------------------------------------
# gsr-ui would otherwise rename/move the file itself
cfg="$HOME/.config/gpu-screen-recorder/config_ui"
if [ -f "$cfg" ] && grep -Eq '"(organize_into_folders|name_after_captured_application)": *"yes"|_(after_game|game_folder)": *true' "$cfg"; then
  running=0
  if pgrep -x gsr-ui >/dev/null; then
    running=1
    systemctl --user stop "$gsr_ui_unit" 2>/dev/null || pkill -x gsr-ui || true
    sleep 1
  fi
  cp "$cfg" "$cfg.bak"
  python3 - "$cfg" <<'EOF'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
for sec in ("record", "replay"):
    s = c.get(sec, {})
    s["organize_into_folders"] = "no"
    s["name_after_captured_application"] = "no"
    s["name_video_file_after_game"] = False
    s["save_video_in_game_folder"] = False
json.dump(c, open(p, "w"), indent=2)
EOF
  say "gsr-ui: turned off its own naming/folder options (backup: $cfg.bak)"
  if [ $running = 1 ]; then
    systemctl --user start "$gsr_ui_unit" 2>/dev/null || (setsid gsr-ui >/dev/null 2>&1 &)
  fi
fi
if pgrep -f '^/usr/bin/gpu-screen-recorder ' >/dev/null && ! pgrep -f '^/usr/bin/gpu-screen-recorder .* -sc ' >/dev/null; then
  say "restart the replay/recording in gsr-ui once so it picks up the wrapper"
fi

say "done. config: ~/.config/gsr-categories/config.json  log: ~/.local/state/gsr-categories/log.txt"
