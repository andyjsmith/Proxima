# The in-application titlebar (GtkHeaderBar)

A trial feature, off by default, behind the `use_header_bar` setting
(Preferences → Appearance → "Draw the titlebar in the application").

This file exists so the feature can be removed cleanly if it is not wanted.
Every change made for it is listed below; deleting exactly these leaves no
dead code behind.

## What it does

With the setting on, `MainWindow` calls `Gtk.Window.set_titlebar()` with a
`Gtk.HeaderBar` instead of letting the window manager draw the frame. The
titlebar is then drawn by GTK with the application's own theme, in the same
colours, and follows light/dark automatically.

The header bar carries the window title, the connection summary as its
subtitle, and two buttons (Refresh, Preferences). Nothing was moved off the
toolbar or the status bar. That is deliberate: a header bar that also
rearranged the rest of the chrome would be much harder to take back out
again, and the layout is the part most likely to be judged on taste.

## Why it needs a restart

GTK decides between client-side and server-side decorations when a window is
created and does not revisit it. Calling `set_titlebar()` on a window that is
already on screen leaves it in a mixed state. The setting is therefore read
once, in `MainWindow.__init__`, before anything else is built.

## Known trade-offs on Windows

- The maximise button is a GTK button, so Windows 11's snap-layouts flyout
  (hovering the real maximise button) does not appear. Dragging to an edge
  still snaps.
- No native drop shadow, and the resize border is GTK's, which is thinner
  than the system one.
- `proxima/theme/native_chrome.py` (the DWM dark-titlebar calls) becomes
  redundant for the main window, since there is no system titlebar left to
  recolour. It is *not* disabled: dialogs still use server-side decorations
  and still need it, and it is a no-op when there is no system frame.

## Every change made for this feature

| Where | What |
| --- | --- |
| `proxima/config.py` | `"use_header_bar": False` in `DEFAULTS`, with its comment |
| `proxima/ui/main_window.py` | `MainWindow._build_header_bar()` — the whole method |
| `proxima/ui/main_window.py` | In `__init__`, the `self.header_bar = None` / `if config.get("use_header_bar")` block, placed before `root` is built |
| `proxima/ui/main_window.py` | In `_update_connection_label()`, the `if self.header_bar is not None:` block that sets the subtitle |
| `proxima/ui/main_window.py` | In `open_settings()`, the `elif` branch reporting "Restart required to change the titlebar" |
| `proxima/ui/settings_dialog.py` | The `_check(...)` for `"use_header_bar"` in `_appearance_page()`, and the row index of the "Installed GTK themes" note (3 with it, 2 without) |
| `tools/smoke_test.py` | The "in-application titlebar" section |
| `docs/header-bar.md` | This file |

## To remove it

1. Delete each row in the table above.
2. Change the themes note in `_appearance_page()` back to `grid.attach(note, 0, 2, 2, 1)`.
3. `grep -rn "use_header_bar\|header_bar" proxima/ tools/` should come back
   empty.

Nothing else references the setting, and no other code branches on it, so
there is no behaviour to unpick beyond that.
