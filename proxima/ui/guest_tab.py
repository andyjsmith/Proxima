"""One guest, one tab, two views.

A tab belongs to a guest rather than to a console: it holds the console and
a summary of the guest, and flips between them. That is what makes a tab
survive a guest being powered off -- the console underneath it is gone, but
the tab is not, and it has something to show.

Which view a tab is on is the tab's own business. Flipping one leaves every
other tab where it was.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

CONSOLE = "console"
SUMMARY = "summary"


def console_of(page):
    """The console behind a notebook page, whatever kind of page it is.

    Consoles are also put into the notebook directly in a few places (a
    pop-out being returned, the tests), so a page is either a tab with a
    console in it or a console itself.
    """
    if isinstance(page, GuestTab):
        return page.console
    return page if hasattr(page, "protocol") else None


def tab_of(page):
    return page if isinstance(page, GuestTab) else None


class GuestTab(Gtk.Stack):
    """A guest's console and its summary, one showing at a time."""

    def __init__(self, guest_key, summary):
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_transition_duration(100)

        self.guest_key = guest_key
        self.summary = summary
        self.console = None
        # Something has to occupy the console side until a console exists,
        # or the stack has nothing to switch to and the flip does nothing.
        self._empty = Gtk.Box()
        self.add_named(self._empty, CONSOLE)
        self.add_named(summary, SUMMARY)
        self.set_visible_child_name(SUMMARY)

        # Set when the view was chosen by hand. The guest changing power
        # state clears it: an explicit choice is about the guest as it is
        # now, not a standing instruction to ignore it booting.
        self.chosen = False
        self._last_running = None

    # -- views ---------------------------------------------------------

    @property
    def view(self):
        return self.get_visible_child_name() or SUMMARY

    def show_view(self, name, by_user=False):
        if name == CONSOLE and self.console is None:
            # Nothing to show yet; the summary is the honest answer.
            name = SUMMARY
        if by_user:
            self.chosen = True
        if self.view == name:
            return
        if name == SUMMARY:
            # Grab the last frame before it goes, so the summary has
            # something better to show than a placeholder.
            self.capture_preview()
        self.set_visible_child_name(name)
        if name == CONSOLE:
            self.focus_console()

    def toggle(self, by_user=True):
        self.show_view(SUMMARY if self.view == CONSOLE else CONSOLE, by_user=by_user)

    def follow_guest_state(self, guest):
        """Powering off shows the summary; coming back shows the console.

        A guest whose power state changed overrides a view the user picked
        for how it used to be.
        """
        if guest is None:
            return
        running = guest.running and not guest.template
        wanted = CONSOLE if running else SUMMARY
        if self._last_running is not None and self._last_running == running:
            # Nothing changed, so a hand-picked view stands.
            if self.chosen:
                return
        else:
            self.chosen = False
        self._last_running = running
        self.show_view(wanted)

    # -- the console ---------------------------------------------------

    def set_console(self, console):
        """Put a console in the tab, replacing whatever was there."""
        showing_console = self.view == CONSOLE
        old = self.console
        if old is not None and old.get_parent() is self:
            self.remove(old)
        elif self._empty.get_parent() is self:
            self.remove(self._empty)
        self.console = console
        if console is not None:
            self.add_named(console, CONSOLE)
            console.show_all()
        else:
            self.add_named(self._empty, CONSOLE)
        if showing_console:
            self.set_visible_child_name(CONSOLE)
        return old

    def take_console(self):
        """Hand the console out -- to a pop-out window -- and keep the tab."""
        console = self.console
        if console is not None and console.get_parent() is self:
            self.remove(console)
        self.console = None
        self._empty.show_all()
        self.add_named(self._empty, CONSOLE)
        self.set_visible_child_name(SUMMARY)
        return console

    def focus_console(self):
        grab = getattr(self.console, "grab_focus_display", None)
        if grab is not None:
            grab()

    def release_console(self):
        release = getattr(self.console, "release_input", None)
        if release is not None:
            release()

    # -- preview -------------------------------------------------------

    def capture_preview(self):
        """Photograph the console, for the summary to show while it is away.

        Best effort by design: a console that was never realised, or one
        that is offline, simply has no picture and the summary says so.
        """
        console = self.console
        if console is None or not console.get_realized():
            return None
        window = console.get_window()
        if window is None:
            return None
        width, height = console.get_allocated_width(), console.get_allocated_height()
        if width < 16 or height < 16:
            return None
        try:
            pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
        except Exception:
            return None
        if pixbuf is None:
            return None
        self.summary.set_preview(pixbuf)
        return pixbuf
