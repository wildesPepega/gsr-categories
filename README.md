# gsr-categories

Game detection for [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/), similar to Medal or SteelSeries Moments.

- **Clean per-game folders:** Clips are named after the game's official name from Steam or Faugus Launcher, not the window title. You get `Counter-Strike Global Offensive/`, not `Counter-Strike: Global Offensive - OpenGL/`.
- **One `Desktop` folder:** Everything that isn't a game goes into a single `Desktop/` folder.
- **Game mode:** While a game is running, the replay buffer records only the game window. If you alt-tab out, the clip still shows the game running in the background, never your desktop.

The folder layout works well with media servers that group by folder, such as [Fireshare](https://github.com/ShaneIsrael/fireshare):

```
~/Videos/Clips/
├── Counter-Strike 2/
│   └── Counter-Strike 2 2026-10-25 22-08-27.mp4
├── Grand Theft Auto V Enhanced/
└── Desktop/
    └── Desktop 2026-10-25 21-44-10.mp4
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/wildesPepega/gsr-categories/main/install.sh | sh
```

Then restart the replay in the GSR UI once so it uses the new hook.

Uninstall:

```sh
curl -fsSL https://raw.githubusercontent.com/wildesPepega/gsr-categories/main/install.sh | sh -s -- --uninstall
```

### Requirements

- `gpu-screen-recorder` and `gpu-screen-recorder-ui` (gsr-ui)
- `python3`
- For game mode and Wayland window detection: KDE Plasma 6, `qdbus6` and `python-gobject`. On Debian or Ubuntu the package is `python3-gi`.
- `xprop`: optional. It's the fallback window detection on other desktops, and it only sees XWayland/X11 windows.

## How it works

| Part | What it does |
|---|---|
| `~/.local/bin/gpu-screen-recorder` | A wrapper that gsr-ui runs through `PATH`. It adds `-sc gsr-categorize` so every saved video is passed to the script. In game mode it swaps the replay's capture target for the game window. Extra arguments from an existing wrapper are kept, for example a `-p` plugin. |
| `gsr-categorize <file> <type>` | Asks KWin which window is focused and follows that process tree: up for Proton and launchers, down for gamescope. It then looks up the name and moves the clip to `<save dir>/<Game>/<Game> <date time>.mp4`. |
| `gsr-gamewatch.service` | A user service. KWin reports window changes to it over D-Bus. When a game window appears, it restarts the replay as a window capture. When the window is gone, it switches back to the monitor. |
| `~/.local/bin/gsr-notify` | During a switch it replaces gsr-ui's "Replay stopped / Started replaying" with `Switching to Gamerecording. Game: …` or `Switching to Desktop recording`. |

The installer also turns off gsr-ui's own "name after application" and "organize into folders" options so the two don't fight over the file. A backup is saved as `config_ui.bak`.

### Game detection

- **Steam games:** detected by `SteamAppId`. The name comes from `appmanifest_<id>.acf`.
- **Non-Steam games added to Steam:** the name comes from `shortcuts.vdf`.
- **Faugus Launcher:** the game's `WINEPREFIX` is matched against `games.json`.
- **Other Wine/Proton programs:** named after their `.exe` and treated as games only for sorting. They don't trigger game mode. Use `exe_names` to give them a proper name.
- **Never games:** Steam tools such as Proton, the Steam Linux Runtimes and Steamworks Redistributables, plus everything in `not_games`.

Names keep their official spelling. Only characters that break folder names (`: / \ * ? " < > |`) and `™ ® ©` are removed.

### Game mode and the portal dialog

On Wayland, a single window can only be captured through the desktop portal. The first time game mode starts for a game, KDE asks you to pick the window. Choose the game, or the gamescope window if you use gamescope. The choice is saved per game in `~/.config/gsr-categories/portal-tokens/` and restored automatically next time, even after the game restarts.

Tip for CS2: put `engine_no_focus_sleep 0` in your autoexec. Otherwise the game throttles its FPS while unfocused and the background part of the clip stutters.

## Config

`~/.config/gsr-categories/config.json` is created on first run:

```json
{
  "desktop_folder": "Desktop",
  "rename": { "Counter-Strike Global Offensive": "CS GO" },
  "not_games": ["Wallpaper Engine"],
  "exe_names": { "RocketLeague": "Rocket League" },
  "game_mode": true,
  "game_mode_delay": 8
}
```

- `rename`: maps a folder name to the name you want instead.
- `not_games`: names that always go to the desktop folder.
- `exe_names`: maps a Wine `.exe` name (without `.exe`) to a game name.
- `game_mode`: set to `false` to always record the monitor.
- `game_mode_delay`: only used if KWin window events aren't available.

Every saved clip and every mode switch is logged to `~/.local/state/gsr-categories/log.txt`.

## Limitations

- Game mode needs KDE Plasma 6 (KWin scripting). Clip sorting also works elsewhere through `xprop`, but only for XWayland/X11 windows.
- When the mode switches, the replay buffer restarts, so footage from just before a game starts or ends isn't in the next clip.
- Separate regular recordings keep their configured capture target. Only the replay switches to game mode.
- If a game is minimized instead of just covered, KWin may stop rendering it, and the clip freezes for that part.

## License

MIT
