"""Telling somebody there is a newer Proxima.

The release notes are shown as they were written. GitHub release bodies are
Markdown and this is a GTK label, so the handful of marks that would
otherwise read as noise are turned into something plain -- headings lose
their hashes, list bullets become bullets. It is deliberately not a Markdown
renderer: the notes are read, not published.
"""

import logging
import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..theme import decorate as theme_decorate
from . import desktop

log = logging.getLogger(__name__)

# Enough of Markdown to stop it reading as source code.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_EMPHASIS = re.compile(r"(\*\*|__|\*|`)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def plain_notes(text, limit=8000):
    """Release notes as readable text rather than as Markdown source."""
    lines = []
    for raw in (text or "").splitlines():
        line = _HEADING.sub("", raw)
        line = _BULLET.sub(r"\1• ", line)
        line = _LINK.sub(r"\1", line)
        line = _EMPHASIS.sub("", line)
        lines.append(line.rstrip())
    joined = "\n".join(lines).strip()
    if len(joined) > limit:
        joined = (
            joined[:limit].rstrip() + "\n\n(truncated -- see the full notes online)"
        )
    return joined or "No release notes were published."


class UpdateDialog(Gtk.Dialog):
    """What is new, and a way to go and get it."""

    DOWNLOAD = 1
    LATER = 2

    def __init__(self, parent, release, current_version, on_setting=None):
        super().__init__(title="Update Available", transient_for=parent)
        self.release = release
        self.on_setting = on_setting or (lambda enabled: None)

        self.add_button("Not Now", self.LATER)
        download = self.add_button("Download", self.DOWNLOAD)
        download.get_style_context().add_class("suggested-action")
        self.set_default_response(self.DOWNLOAD)
        self.set_default_size(560, 460)

        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(10)

        heading = Gtk.Label(xalign=0.0)
        heading.set_markup(
            f"<b>{GLib.markup_escape_text(release['name'])}</b>\n"
            f"<small>You have {GLib.markup_escape_text(current_version)}</small>"
        )
        content.pack_start(heading, False, False, 0)

        notes = Gtk.Label(label=plain_notes(release.get("notes")), xalign=0.0)
        notes.set_line_wrap(True)
        notes.set_selectable(True)
        notes.set_valign(Gtk.Align.START)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(notes)
        content.pack_start(scroller, True, True, 0)

        self.remember = Gtk.CheckButton(label="Check for updates automatically")
        self.remember.set_active(True)
        self.remember.set_tooltip_text("Also in Preferences -> Behaviour")
        content.pack_start(self.remember, False, False, 0)

        self.connect("response", self._on_response)
        theme_decorate(self)
        self.show_all()

    def _on_response(self, _dialog, response):
        self.on_setting(self.remember.get_active())
        url = self.release.get("url")
        self.destroy()
        if response == self.DOWNLOAD and url:
            desktop.open_uri(url)
