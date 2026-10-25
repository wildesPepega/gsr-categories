#!/usr/bin/env python3
"""Sort GPU Screen Recorder clips into per-game folders.

Called by gpu-screen-recorder via `-sc <script>` after a video is saved:
    gsr-categorize.py <saved_file> <regular|replay>

The focused window at save time decides the folder. Steam and Faugus games get
their official name, everything else goes into the "Desktop" folder.

    gsr-categorize.py --watch         game mode daemon (replay records only the game window)
    gsr-categorize.py --portal-token  token file for the current game (used by the wrapper)
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".config/gsr-categories/config.json"
LOG_PATH = HOME / ".local/state/gsr-categories/log.txt"
STEAM_ROOTS = [HOME / ".local/share/Steam", HOME / ".steam/steam",
               HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
FAUGUS_GAMES = HOME / ".local/share/faugus-launcher/games.json"
VIDEO_EXTS = {".mp4", ".mkv", ".flv", ".webm", ".mov", ".ts", ".m3u8"}

DEFAULT_CONFIG = {
    "desktop_folder": "Desktop",
    # Final folder name -> name you want instead
    "rename": {},
    # Names that are never treated as games (compared after cleanup)
    "not_games": ["Wallpaper Engine"],
    # Wine/Proton exe name (without .exe) -> game name, for games without a known launcher
    "exe_names": {},
    # While a game runs, the replay records only the game window
    "game_mode": True,
    # Seconds a game must be running before switching (its window has to exist)
    "game_mode_delay": 8,
}
# Steam apps that are tools, not games
STEAM_TOOL_RE = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common|SteamVR|Steam Audio)", re.I)


def log(msg):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


_config_cache = (None, None)


def load_config():
    global _config_cache
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None and _config_cache[0] == mtime:
        return _config_cache[1]
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n")
    except (OSError, ValueError) as e:
        log(f"config error, using defaults: {e}")
    _config_cache = (mtime, cfg)
    return cfg


def clean_name(name):
    """Keep the official spelling, only drop characters that break folder names."""
    name = re.sub(r"[™®©]", "", name)
    name = re.sub(r'[:/\\*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name


# --- focused window -------------------------------------------------------

def focused_pid():
    """PID of the focused window, asking KWin first (sees Wayland + gamescope)."""
    return kwin_focused_pid() or xprop_focused_pid()


def kwin_focused_pid():
    marker = f"GSRCAT{os.getpid()}x{time.time_ns()}"
    script = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"{marker}.js"
    try:
        script.write_text("const w = workspace.activeWindow;\n"
                          f'print("{marker} " + (w ? w.pid : 0));\n')
        qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
        if not qdbus:
            return None
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))
        sid = subprocess.run([qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript",
                              str(script), marker], capture_output=True, text=True, timeout=3).stdout.strip()
        if not sid.lstrip("-").isdigit() or int(sid) < 0:
            return None
        subprocess.run([qdbus, "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
                       capture_output=True, timeout=3)
        pid = None
        for _ in range(20):
            out = subprocess.run(["journalctl", "--user", "--since", since, "-o", "cat", "--grep", marker],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(rf"{marker} (\d+)", out)
            if m:
                pid = int(m.group(1)) or None
                break
            time.sleep(0.05)
        subprocess.run([qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", marker],
                       capture_output=True, timeout=3)
        return pid
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        script.unlink(missing_ok=True)


def xprop_focused_pid():
    """PID of the focused XWayland window, or None for native Wayland windows."""
    try:
        out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                             capture_output=True, text=True, timeout=2).stdout
        wid = re.search(r"0x[0-9a-f]+", out)
        if not wid or int(wid.group(0), 16) == 0:
            return None
        out = subprocess.run(["xprop", "-id", wid.group(0), "_NET_WM_PID"],
                             capture_output=True, text=True, timeout=2).stdout
        pid = re.search(r"=\s*(\d+)", out)
        return int(pid.group(1)) if pid else None
    except (OSError, subprocess.SubprocessError):
        return None


def proc_environ(pid):
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def proc_parent(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def proc_exe_stem(pid):
    """Windows exe name for Wine processes, else the Linux binary name."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    for part in cmd:
        p = part.decode(errors="replace")
        if p.lower().endswith(".exe"):
            return re.split(r"[\\/]", p)[-1][:-4]
    return None


# --- Steam ----------------------------------------------------------------

def steam_libraries():
    libs = []
    for root in STEAM_ROOTS:
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            libs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))]
            libs.append(root)
    return list(dict.fromkeys(Path(os.path.realpath(p)) for p in libs))


def steam_app_name(appid):
    for lib in steam_libraries():
        acf = lib / f"steamapps/appmanifest_{appid}.acf"
        if acf.is_file():
            m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(errors="replace"))
            if m:
                return m.group(1)
    return None


def _read_binary_vdf(data, i=0):
    """Minimal parser for Steam's binary VDF (shortcuts.vdf)."""
    out = {}
    while i < len(data):
        t = data[i]
        i += 1
        if t == 0x08:
            return out, i
        end = data.index(b"\0", i)
        key = data[i:end].decode(errors="replace").lower()
        i = end + 1
        if t == 0x00:
            out[key], i = _read_binary_vdf(data, i)
        elif t == 0x01:
            end = data.index(b"\0", i)
            out[key] = data[i:end].decode(errors="replace")
            i = end + 1
        elif t == 0x02:
            out[key] = struct.unpack_from("<I", data, i)[0]
            i += 4
        else:
            break
    return out, i


def steam_shortcut_name(appid):
    """Name of a non-Steam game added to Steam (appid is the unsigned 32-bit id)."""
    for root in STEAM_ROOTS:
        for vdf in (root / "userdata").glob("*/config/shortcuts.vdf"):
            try:
                tree, _ = _read_binary_vdf(vdf.read_bytes())
            except (OSError, ValueError, struct.error):
                continue
            for sc in tree.get("shortcuts", {}).values():
                if isinstance(sc, dict) and sc.get("appid") == appid:
                    return sc.get("appname")
    return None


# --- detection --------------------------------------------------------------

def faugus_titles():
    try:
        games = json.loads(FAUGUS_GAMES.read_text())
    except (OSError, ValueError):
        return {}
    return {os.path.realpath(g["prefix"]): g["title"] for g in games if g.get("prefix") and g.get("title")}


def proc_children():
    children = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            parent = proc_parent(int(d.name))
            if parent:
                children.setdefault(parent, []).append(int(d.name))
    return children


def process_game(p, cfg, faugus):
    """(raw game name or None, wine exe stem or None) for a single process."""
    env = proc_environ(p)
    appid = env.get("SteamAppId") or env.get("STEAM_COMPAT_APP_ID")
    if appid and appid.isdigit() and appid != "0":
        name = steam_app_name(appid)
        if name:
            return name, None
    gameid = env.get("SteamGameId", "")
    if gameid.isdigit() and int(gameid) > 0xFFFFFFFF:
        name = steam_shortcut_name(int(gameid) >> 32)
        if name:
            return name, None
    prefix = env.get("WINEPREFIX")
    if prefix:
        title = faugus.get(os.path.realpath(prefix))
        if title:
            return title, None
    stem = proc_exe_stem(p)
    if stem and stem in cfg.get("exe_names", {}):
        return cfg["exe_names"][stem], None
    return None, stem if prefix else None


def detect_game(pid, cfg):
    """Return the raw game name for the process tree of pid, or None.

    Walks up from the focused window's process (Proton, launchers), then down
    (gamescope runs the game as a child of its own window).
    """
    faugus = faugus_titles()
    wine_exe = None

    def check(p):
        nonlocal wine_exe
        name, stem = process_game(p, cfg, faugus)
        wine_exe = wine_exe or stem
        return name

    p = pid
    for _ in range(30):
        if not p or p <= 1:
            break
        name = check(p)
        if name:
            return name
        p = proc_parent(p)

    # Only look below gamescope, else a focused terminal/Steam window would count as its game
    try:
        if Path(f"/proc/{pid}/comm").read_text().strip() not in ("gamescope", "gamescope-wl"):
            return wine_exe
    except OSError:
        return wine_exe
    children = proc_children()
    queue = list(children.get(pid, []))
    for _ in range(500):
        if not queue:
            break
        p = queue.pop(0)
        name = check(p)
        if name:
            return name
        queue += children.get(p, [])
    # Unknown Wine/Proton program: assume it is a game, named after its exe
    return wine_exe


def resolve_name(raw, cfg):
    """Folder name for a detected game, or None if it does not count as a game."""
    if not raw or STEAM_TOOL_RE.match(raw):
        return None
    name = clean_name(raw)
    name = cfg.get("rename", {}).get(name, name)
    not_games = {clean_name(n).casefold() for n in cfg.get("not_games", [])}
    if not name or name.casefold() in not_games:
        return None
    return name


def target_folder(cfg):
    pid = focused_pid()
    raw = detect_game(pid, cfg) if pid else None
    return resolve_name(raw, cfg) or cfg.get("desktop_folder", "Desktop"), pid, raw


# --- game mode ----------------------------------------------------------------
# While a game runs, the replay records only the game window (via the desktop
# portal), so alt-tabbing never shows the desktop. The wrapper reads GAME_STATE.

GAME_STATE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "gsr-categories/game"
# Text for the next gsr-ui notification, picked up by the gsr-notify wrapper
NOTIFY_STATE = GAME_STATE.with_name("notify")

BUS_NAME = "org.gsrcategories.Watch"
BUS_PATH = "/org/gsrcategories/Watch"
KWIN_SCRIPT_NAME = "gsr-categories-windows"
# Reports all windows (pid, app id, title) to the watcher whenever one opens/closes
KWIN_SCRIPT = f"""
function report() {{
    const wins = workspace.windowList().filter(w => w.normalWindow)
        .map(w => [w.pid, w.desktopFileName || w.resourceClass || "", w.caption || ""]);
    const size = workspace.virtualScreenSize;
    callDBus("{BUS_NAME}", "{BUS_PATH}", "{BUS_NAME}", "Windows",
             JSON.stringify({{size: [size.width, size.height], windows: wins}}));
}}
workspace.windowAdded.connect(report);
workspace.windowRemoved.connect(report);
report();
"""
BUS_XML = f"""
<node>
  <interface name="{BUS_NAME}">
    <method name="Windows"><arg type="s" name="windows" direction="in"/></method>
  </interface>
</node>
"""


def running_games(cfg, cache):
    """{pid: folder name} of all running game processes (not only the focused one).

    Unknown Wine programs don't count here: wineserver & co. would always match.
    """
    faugus = None
    alive = set()
    for n in os.listdir("/proc"):
        if not n.isdigit():
            continue
        try:
            with open(f"/proc/{n}/comm") as f:
                key = (int(n), f.read())  # comm changes on exec, so re-check then
        except OSError:
            continue
        alive.add(key)
        if key not in cache:
            if faugus is None:
                faugus = faugus_titles()
            cache[key] = resolve_name(process_game(key[0], cfg, faugus)[0], cfg)
    for key in cache.keys() - alive:
        del cache[key]
    return {key[0]: name for key, name in cache.items() if name}


def is_gamescope(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() in ("gamescope", "gamescope-wl")
    except OSError:
        return False


def game_with_window(games, windows):
    """(game name, window pid) of a game that has a window: its own, or the gamescope
    window it runs in. (None, None) if no game has one."""
    for pid, name in games.items():
        if pid in windows:
            return name, pid
        p = pid
        for _ in range(30):
            p = proc_parent(p)
            if not p or p <= 1:
                break
            if p in windows and is_gamescope(p):
                return name, p
    return None, None


# --- portal permission ----------------------------------------------------------
# KDE remembers a screen-share choice as (window app id, window title) in the portal
# permission store. Writing that entry ourselves before the first switch means KDE
# never shows the "share screen" dialog. If the format ever changes, KDE just asks.

PERMISSION_STORE = ("org.freedesktop.impl.portal.PermissionStore",
                    "/org/freedesktop/impl/portal/PermissionStore",
                    "org.freedesktop.impl.portal.PermissionStore")


def portal_token_path(game):
    tokens = CONFIG_PATH.parent / "portal-tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    return tokens / f"{game or 'default'}.token"


def _qstring(text):
    raw = text.encode("utf-16-be")
    return struct.pack(">I", len(raw)) + raw


def kde_restore_data(app_id, title, size=(0, 0)):
    """QDataStream'd QVariantMap that xdg-desktop-portal-kde stores for a window share."""
    out = struct.pack(">I", 4)
    out += _qstring("outputs") + struct.pack(">IBI", 9, 0, 0)                    # QVariantList []
    out += _qstring("region") + struct.pack(">IBiiii", 19, 0, 0, 0, -1, -1)      # QRect (null)
    out += _qstring("virtualSize") + struct.pack(">IBii", 21, 0, *size)          # QSize
    type_name = b"QList<WindowRestoreInfo>\0"
    out += _qstring("windows") + struct.pack(">IBI", 0x10000, 0, len(type_name)) + type_name
    out += struct.pack(">I", 1) + _qstring(app_id) + _qstring(title)
    return out


def create_portal_token(app_id, title, size):
    import string
    import secrets
    from gi.repository import Gio, GLib
    # Same shape as the portal's own tokens: 22 alphanumeric chars ("-" is rejected)
    token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(22))
    data = GLib.Variant("(suv)", ("KDE", 1, GLib.Variant("ay", kde_restore_data(app_id, title, size))))
    Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
        *PERMISSION_STORE, "Set",
        GLib.Variant("(sbsa{sas}v)", ("screencast", True, token, {"": ["yes"]}, data)),
        None, Gio.DBusCallFlags.NONE, 3000)
    return token


def replay_running():
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmd and cmd[0].endswith(b"gpu-screen-recorder") and b"-r" in cmd:
                return True
    return False


def restart_replay(notify_text):
    NOTIFY_STATE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_STATE.write_text(notify_text)
    subprocess.run(["gsr-ui-cli", "stop-replay"], capture_output=True, timeout=5)
    for _ in range(50):
        if not replay_running():
            break
        time.sleep(0.1)
    subprocess.run(["gsr-ui-cli", "start-replay"], capture_output=True, timeout=5)


class Watcher:
    """Daemon: switch the replay between monitor and game-window capture.

    KWin tells us instantly when windows open/close; a 1s /proc scan notices
    games starting before their window shows up.
    """

    def __init__(self):
        self.cache = {}
        self.windows = None  # {pid: (app id, title)}, None until KWin reported once
        self.screen_size = (0, 0)
        self.window_lost = None  # when the current game's window disappeared
        self.current = GAME_STATE.read_text().strip() if GAME_STATE.exists() else None
        self.resume = replay_running()  # replay was on when the game started
        self.since = None  # when the wanted mode first differed from the current one

    def evaluate(self):
        try:
            self._evaluate()
        except Exception as e:  # keep the GLib timer alive
            log(f"watch error: {e!r}")
        return True

    def _evaluate(self):
        cfg = load_config()
        games = self.running(cfg)
        now = time.monotonic()
        win = None
        if self.windows is None:
            # No window info from KWin: fall back to "a game process runs for a while"
            want = next(iter(games.values()), None)
            delay = float(cfg.get("game_mode_delay", 8)) if want else 3
        else:
            want, win = game_with_window(games, self.windows)
            delay = 0.5 if want else 1
            if want or self.current not in games.values():
                self.window_lost = None
            else:
                # Game still runs without a window (launcher closed, game window next):
                # stay in game mode for a while instead of switching back and forth
                self.window_lost = self.window_lost or now
                if now - self.window_lost < 20:
                    want = self.current
        if want == self.current:
            self.since = None
            return
        self.since = self.since or now
        if now - self.since >= delay:
            self.switch(want, self.windows.get(win) if self.windows and win else None)
            self.since = None

    def running(self, cfg):
        return running_games(cfg, self.cache) if cfg.get("game_mode", True) else {}

    def switch(self, game, window=None):
        was_running = replay_running()
        token = portal_token_path(game)
        if game and window and not token.exists():
            try:
                token.write_text(create_portal_token(*window, self.screen_size))
                log(f"created portal permission for window {window!r}")
            except Exception as e:  # KDE will ask instead
                log(f"could not create portal permission: {e!r}")
        if game and self.current is None:
            self.resume = was_running
        if game:
            GAME_STATE.parent.mkdir(parents=True, exist_ok=True)
            GAME_STATE.write_text(game + "\n")
        else:
            GAME_STATE.unlink(missing_ok=True)
        log(f"game mode: {self.current!r} -> {game!r} (replay running: {was_running})")
        self.current = game
        if was_running or (game is None and self.resume):
            restart_replay(f"Switching to Gamerecording. Game: {game}" if game
                           else "Switching to Desktop recording")

    def on_windows(self, payload):
        try:
            data = json.loads(payload)
            self.windows = {int(pid): (app, title) for pid, app, title in data["windows"]}
            self.screen_size = tuple(int(v) for v in data["size"])
        except (ValueError, TypeError, KeyError):
            return
        self.evaluate()


def kwin_load_script():
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    if not qdbus:
        return False
    path = GAME_STATE.with_name("windows.js")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(KWIN_SCRIPT)
    run = lambda *a: subprocess.run([qdbus, "org.kde.KWin", *a], capture_output=True, text=True, timeout=3).stdout.strip()
    run("/Scripting", "org.kde.kwin.Scripting.unloadScript", KWIN_SCRIPT_NAME)
    sid = run("/Scripting", "org.kde.kwin.Scripting.loadScript", str(path), KWIN_SCRIPT_NAME)
    if not sid.lstrip("-").isdigit() or int(sid) < 0:
        return False
    run(f"/Scripting/Script{sid}", "org.kde.kwin.Script.run")
    return True


def kwin_script_loaded():
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    out = subprocess.run([qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.isScriptLoaded",
                          KWIN_SCRIPT_NAME], capture_output=True, text=True, timeout=3).stdout
    return out.strip() == "true"


def watch():
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    w = Watcher()
    log(f"watch started, game mode: {w.current!r}")

    def on_call(conn, sender, path, iface, method, params, invocation):
        if method == "Windows":
            w.on_windows(params.unpack()[0])
        invocation.return_value(None)

    def on_bus(conn, name):
        node = Gio.DBusNodeInfo.new_for_xml(BUS_XML)
        conn.register_object(BUS_PATH, node.interfaces[0], on_call, None, None)

    def on_name(conn, name):
        if not kwin_load_script():
            log("could not load KWin script, falling back to process polling")

    def keep_script():
        # KWin restarts (e.g. crash) drop the script; reload it
        try:
            if not kwin_script_loaded():
                w.windows = None
                kwin_load_script()
        except (OSError, subprocess.SubprocessError):
            pass
        return True

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, on_name, None)
    GLib.timeout_add(1000, w.evaluate)
    GLib.timeout_add_seconds(30, keep_script)
    GLib.MainLoop().run()



def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        return watch()
    if len(sys.argv) > 1 and sys.argv[1] == "--portal-token":
        # Per-game portal token, so each game remembers its own window choice
        print(portal_token_path(GAME_STATE.read_text().strip() if GAME_STATE.exists() else None))
        return 0
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = Path(sys.argv[1])
    kind = sys.argv[2] if len(sys.argv) > 2 else ""
    if src.suffix.lower() not in VIDEO_EXTS or not src.is_file():
        return 0
    cfg = load_config()
    game = GAME_STATE.read_text().strip() if GAME_STATE.exists() else None
    if game and kind == "replay":
        # Game mode: the clip shows the game, wherever the focus is
        folder, pid, raw = game, None, "game mode"
    else:
        folder, pid, raw = target_folder(cfg)

    dest_dir = src.parent / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(src.stat().st_mtime))
    dest = dest_dir / f"{folder} {stamp}{src.suffix}"
    n = 2
    while dest.exists():
        dest = dest_dir / f"{folder} {stamp} ({n}){src.suffix}"
        n += 1
    shutil.move(src, dest)
    log(f"{kind or '?'} pid={pid} detected={raw!r} -> {dest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never lose a clip silently
        log(f"error: {e!r} args={sys.argv[1:]}")
        sys.exit(1)
