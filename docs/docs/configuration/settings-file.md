---
title: Settings file
sidebar_position: 3
---

# Settings file

Everything Proxima remembers lives in one JSON file: preferences, saved
connections, pinned certificates, per guest console settings and the window
layout.

|              |                                                                                |
| ------------ | ------------------------------------------------------------------------------ |
| Windows      | `%APPDATA%\Proxima\settings.json`                                              |
| Linux        | `$XDG_CONFIG_HOME/proxima/settings.json`, or `~/.config/proxima/settings.json` |
| macOS        | `~/.config/proxima/settings.json`                                              |
| Any platform | `PROXIMA_CONFIG_DIR` overrides all of the above                                |

`PROXIMA_CONFIG_DIR` is what makes a portable install possible: point it at a
directory beside the executable and nothing touches the user profile.

The file is written to a temporary file and renamed, so a crash mid save cannot
truncate it. Unrecognised keys are dropped on the next save, and a file that
will not parse is replaced with the defaults.

:::warning[Manual editing]

If you must edit the file manually, ensure Proxima is closed. The running program holds it in memory and rewrites it whenever anything changes.

:::

## Keys

### Connection

| Key                         |                                                                                                               |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `host`, `username`, `realm` | What the Connect dialog opens with.                                                                           |
| `connections`               | Saved servers, reconnected at startup. Each holds a host, port, username, realm and encoded password.         |
| `save_credentials`          | Whether a ticket is kept. Passwords are never stored in plain text.                                           |
| `trusted_certs`             | `host:port` to the certificate fingerprint you approved. Delete an entry to be asked about that server again. |

### Appearance

| Key                | Default      |                                                             |
| ------------------ | ------------ | ----------------------------------------------------------- |
| `color_mode`       | `system`     | `system`, `light` or `dark`.                                |
| `use_system_theme` | `false`      | Hand the interface to your desktop's theme.                 |
| `use_header_bar`   | `false`      | Draw the titlebar in the application. Read once at startup. |
| `font_backend`     | `fontconfig` | `fontconfig`, `win32` or `default`. Read once at startup.   |
| `font_name`        | `""`         | Empty means the theme's font.                               |
| `antialias`        | `grayscale`  | `grayscale`, `subpixel`, `none`, `default`.                 |
| `hint_style`       | `slight`     | `slight`, `full`, `medium`, `none`.                         |
| `hint_metrics`     | `false`      |                                                             |

### Console

| Key                       | Default |                                                      |
| ------------------------- | ------- | ---------------------------------------------------- |
| `enable_audio`            | `true`  | SPICE audio, globally.                               |
| `sw_decoders`             | `false` | Demote hardware video decoders. Read at startup.     |
| `auto_resize`             | `true`  | Ask the guest to match the window.                   |
| `scale_to_fit`            | `false` |                                                      |
| `console_scale`           | `100`   | 100, 125, 150, 175 or 200.                           |
| `prefer_vnc`              | `false` | Force VNC everywhere.                                |
| `fullscreen_all_monitors` | `false` | Give the guest a display per monitor in full screen. |
| `spice_session_check`     | `true`  | Ask QEMU whether anyone else is watching.            |
| `usb_autoprompt`          | `true`  | Offer a device when it is plugged in.                |

### Confirmations, startup and naming

| Key                                                 | Default |                                                        |
| --------------------------------------------------- | ------- | ------------------------------------------------------ |
| `confirm_stop`, `confirm_shutdown`, `confirm_reset` | `true`  |                                                        |
| `confirm_pause`                                     | `false` |                                                        |
| `check_updates`                                     | `true`  | Ask GitHub for the latest release at startup.          |
| `restore_session`                                   | `true`  |                                                        |
| `session_consoles`, `session_expanded`              | `[]`    | What was open and expanded last time. Written on exit. |
| `tab_title_format`                                  | `name`  | `name`, `id` or `both`.                                |
| `tree_name_format`                                  | `name`  | `name` or `id`.                                        |
| `templates_last`                                    | `true`  |                                                        |
| `tree_view`                                         | `node`  | `node`, `folder` or `tag`.                             |

### Layout and polling

| Key                             | Default   |                                                                    |
| ------------------------------- | --------- | ------------------------------------------------------------------ |
| `window_width`, `window_height` | 1280, 800 | The unmaximised size, recorded separately from the maximised flag. |
| `window_maximized`              | `false`   |                                                                    |
| `sidebar_width`                 | 280       |                                                                    |
| `sidebar_visible`               | `true`    |                                                                    |
| `poll_idle_seconds`             | 6         |                                                                    |
| `poll_active_seconds`           | 2         |                                                                    |
| `poll_active_for`               | 15        |                                                                    |
| `task_refresh_seconds`          | 5         |                                                                    |

### guest_prefs

Per guest console settings, keyed by `<node>/<kind>/<vmid>`, for example
`pve1/qemu/101`. Only keys that differ from the global default are written.

| Key                                            |                                                                            |
| ---------------------------------------------- | -------------------------------------------------------------------------- |
| `scale_to_fit`, `auto_resize`, `console_scale` | Overrides of the global console settings.                                  |
| `codec_index`, `compression_index`             | Video codec and image compression, as positions in the View menu.          |
| `disable_effects`                              | Guest desktop effects asked away: `wallpaper`, `font-smooth`, `animation`. |
| `shared_folder`, `shared_folder_ro`            | The folder shared with this guest from this machine.                       |
| `audio`, `microphone`, `smartcard`             | The local half of the per guest switches, as `enabled` or `disabled`.      |
| `font_size`                                    | Serial console font size.                                                  |

Deleting a guest's entry resets it to the globals. The server half of a guest's
settings is not here. It is in the guest's notes. See
[Per guest settings](per-guest-settings.md).

### config_version

Bumped when a stored setting needs rewriting rather than merely defaulting. It
lets an old settings file be migrated on load. Leave it alone.

## Logs

Every run writes one log file. **Help > Open Log Folder** is the short way to
find it.

|              |                                                                  |
| ------------ | ---------------------------------------------------------------- |
| Windows      | `%LOCALAPPDATA%\Proxima\logs`                                    |
| Linux        | `$XDG_STATE_HOME/proxima/logs`, or `~/.local/state/proxima/logs` |
| macOS        | `~/Library/Logs/Proxima`                                         |
| Any platform | `PROXIMA_LOG_DIR` overrides all of the above                     |

The last five runs are kept, and a single run is capped at 10 MB with one
previous chunk retained. A window left open for a fortnight keeps its recent
past without growing forever.

`PROXIMA_LOG_LEVEL` sets the level for a run, and `--debug` logs everything.

On Windows the log file is the only record of a run, because the program has no
console output there at all. Faults from the console and video layers land in
the same file, which is usually where a SPICE problem explains itself.
