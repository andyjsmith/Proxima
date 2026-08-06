"""The compact density stylesheet, layered on top of Adwaita.

This is application CSS at PRIORITY_APPLICATION, so it trims padding without
overriding the theme's own colours -- which is what lets one sheet serve
both light and dark without either looking wrong.
"""

COMPACT_CSS = """
/* Trim the worst of the default padding without changing the look. */
* { font-size: 12px; }

button            { min-height: 0; min-width: 0; padding: 3px 10px; }
entry             { min-height: 0; padding: 3px 6px; }
combobox button   { padding: 2px 6px; }

notebook > header                     { min-height: 0; padding: 0; }
notebook > header > tabs > tab        { min-height: 0; padding: 4px 12px; }

headerbar         { min-height: 34px; padding: 0 4px; }
headerbar button  { min-height: 24px; }

/* With the in-application titlebar on, the menu bar lives inside it. A
   GtkMenuBar brings its own background and border, which in a header bar
   reads as a strip pasted across the frame; these strip that back so the
   menus sit on the titlebar itself. Item padding is restated because the
   menu bar is now the tallest thing up there. */
headerbar menubar            { background: none; border: none; padding: 0; }
headerbar menubar > menuitem { padding: 4px 9px; border-radius: 4px; }
headerbar menubar > menuitem:hover {
    background-color: alpha(currentColor, 0.12);
}

toolbar           { padding: 2px; }
statusbar         { min-height: 0; padding: 0; }
menuitem          { min-height: 0; padding: 3px 8px; }

paned > separator { min-width: 1px; min-height: 1px; }
scrollbar slider  { min-width: 9px; min-height: 9px; }

/* -- application specific ------------------------------------------- */

/* The status strip is a plain box, so its height is exactly this padding
   plus the text. GtkStatusbar could not be trimmed this far. */
.statusbar-box {
    padding: 1px 6px;
    border-top: 1px solid alpha(currentColor, 0.15);
}
.statusbar-box label  { padding: 0; margin: 0; }
.statusbar-box image  { margin: 0 1px; }

/* The clipboard and audio indicators are also switches. A real button gets
   the hover feedback and the pointer handling right by itself; it just has
   to be trimmed to the size of its icon so the strip stays one line high. */
.status-toggle {
    padding: 0 2px;
    margin: 0;
    min-height: 16px;
    min-width: 16px;
    border: none;
    background: none;
    box-shadow: none;
}
.status-toggle:hover {
    background: alpha(currentColor, 0.12);
    border-radius: 3px;
}

/* Header row of a closable pane, e.g. the task list. */
.pane-header {
    padding: 1px 2px 1px 8px;
    border-top: 1px solid alpha(currentColor, 0.15);
    border-bottom: 1px solid alpha(currentColor, 0.10);
}
.pane-header button { padding: 0 4px; min-height: 18px; min-width: 18px; }

/* Shown over a console whose connection has ended. Opaque, because it sits
   on top of the last frame the guest sent. */
.console-status {
    background-color: rgba(28, 31, 36, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 8px;
    padding: 20px 28px;
    color: #ffffff;
}
.console-status label       { color: #ffffff; }
.console-status-title       { font-size: 15px; }
.console-status button      { padding: 4px 14px; }

/* The floating strip over a fullscreen console. Fixed dark colours rather
   than theme ones: it sits on top of the guest's own screen, not on the
   application background, so it has to read the same either way. */
.fullscreen-bar {
    background-color: rgba(28, 31, 36, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-top: none;
    border-radius: 0 0 6px 6px;
    padding: 2px 6px;
    color: #ffffff;
}
.fullscreen-bar label            { color: #ffffff; }
.fullscreen-bar button           { color: #ffffff; padding: 2px 8px; }
.fullscreen-bar button:hover     { background-color: rgba(255,255,255,0.16); }
.fullscreen-bar button:checked   { background-color: rgba(255,255,255,0.28); }
.fullscreen-bar separator        { background-color: rgba(255,255,255,0.24); }

.dim             { opacity: 0.7; }
.mono             { font-family: "JetBrains Mono", "Cascadia Mono",
                                 "Consolas", monospace; }
.status-running   { color: #26a269; }
.status-stopped   { opacity: 0.6; }
.status-paused    { color: #c88800; }
.status-error     { color: #e01b24; }

/* Tab labels carry their own close button; keep it from bloating the tab. */
.tab-close        { padding: 0; min-height: 16px; min-width: 16px; }

/* The menu bar when it shares the toolbar's row rather than having one of
   its own. It arrives with a background, a border and its own padding, all
   of which belong to a bar that is alone on a line. */
.toolbar-menus menubar,
.toolbar-menus menubar > menuitem {
    background: transparent;
    border: none;
    box-shadow: none;
}
.toolbar-menus menubar { padding: 0; margin: 0; }

/* A button showing an icon beside a word. GTK's own image-spacing is two
   pixels, which reads as the two being one glyph. Scoped to a class so
   icon-only buttons -- the toolbar, the tab close buttons -- keep the
   tight fit they are drawn for. */
.labelled-icon image { margin-right: 6px; }

/* The summary page's key column. */
.summary-key      { opacity: 0.7; }
.summary-value    { font-family: "JetBrains Mono", "Cascadia Mono",
                                 "Consolas", monospace; }
"""

# Not dimming an unfocused window is deliberately NOT done in CSS.
#
# It was, once: a sheet that restated every widget's normal colours for the
# :backdrop state. Two things were wrong with it. Application-priority CSS
# outranks every theme rule regardless of selector specificity, so a line as
# innocent as "button:backdrop { border-color: @borders }" gave a border to
# flat toolbar buttons that should have none and overrode the theme's own
# disabled styling -- an insensitive button looked live. And it could only
# ever be a guess at what some other theme does.
#
# theme.keep_active() clears the BACKDROP state flag instead, which is the
# actual input to all of those theme rules. Nothing has to be restated, so
# nothing can be restated wrongly. See keep_active() for the mechanism.

# Row height in a GtkTreeView comes from cell renderer padding, not CSS.
ROW_YPAD = 1


def dark_overrides():
    """Small tweaks that only make sense against a dark base theme."""
    return """
    .status-running { color: #57e389; }
    .status-paused  { color: #f8e45c; }
    .status-error   { color: #ff7b63; }
    """


def build_css(dark=False):
    return COMPACT_CSS + (dark_overrides() if dark else "")
