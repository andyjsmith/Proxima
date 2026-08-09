"""Up to four console panes, side by side, in one window.

Two decisions shape this file.

**The layout is built once and never rebuilt.** All four notebooks and the
GtkPaneds that arrange them exist from the start; splitting and unsplitting
only show and hide them. Rearranging the tree instead would mean reparenting
live notebooks, and reparenting a SpiceDisplay destroys and recreates the
GdkWindow underneath a running connection -- the same trap that stops the
fullscreen code from moving consoles around.

    root (horizontal)
    +- left  (vertical): pane 0 over pane 2
    +- right (vertical): pane 1 over pane 3

One pane is pane 0 alone. Two is 0 | 1. Three adds 2 under 0. Four is a 2x2
grid. A paned whose children are both hidden is hidden itself, so the
survivors get the whole width rather than a stripe of empty space.

**Dragging is GTK's own.** Every notebook shares a group name, which is all
GtkNotebook needs to let tabs be dragged from one to another, with the
insertion feedback and the drop handling it already implements. Nothing here
reimplements drag and drop.

The one thing that does not follow from that: an empty notebook draws no tab
strip, so there would be nothing to drop onto. Splitting therefore *moves*
the console that was in front into the new pane rather than opening an empty
one, and a pane whose last tab is dragged away hides itself again.

**One button, three arrangements.** No split, side by side, one above the
other, and round again. Each is built from a single pane outwards, so an
arrangement is exactly what its name says rather than that plus whatever a
dragged tab left behind. Dragging can still make three panes or a 2x2 grid;
that is not a step on the cycle, and cycling out of one collapses to a
single pane first.

**Which pane is in charge is visible, and clicks decide it.** Split, "the
tab in front" stops being a single thing -- every pane has one -- so the tab
the window is actually acting on is drawn in bold. Clicking anywhere in a
pane puts it in charge, which is deliberate in a way that hovering is not:
a SPICE console takes the keyboard focus when the pointer merely crosses
it, so following the focus would change which guest the toolbar acts on
while the pointer was on its way to a button.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk

MAX_PANES = 4

# Shared by every notebook here, and by nothing else: it is what allows a
# tab to be dragged between panes, and what stops one being dropped into
# some other notebook in the application.
GROUP_NAME = "proxima-consoles"

# The three arrangements one button cycles through. A fourth state exists
# and is not on the cycle: dragging tabs about can produce three panes or a
# 2x2 grid, which is a perfectly good thing to want and not something to
# name a step for. Cycling out of one goes back to a single pane first, so
# the button always does something predictable rather than trying to guess
# which of four panes was meant.
SPLIT_NONE = "none"
SPLIT_SIDE_BY_SIDE = "side-by-side"  # pane 0 | pane 1
SPLIT_STACKED = "stacked"  # pane 0 over pane 2
SPLIT_OTHER = "other"

# Which notebook each arrangement puts the second console in.
_MODE_TARGETS = {SPLIT_SIDE_BY_SIDE: 1, SPLIT_STACKED: 2}

# What the button does next, from wherever it is now.
NEXT_SPLIT_MODE = {
    SPLIT_NONE: SPLIT_SIDE_BY_SIDE,
    SPLIT_SIDE_BY_SIDE: SPLIT_STACKED,
    SPLIT_STACKED: SPLIT_NONE,
    SPLIT_OTHER: SPLIT_NONE,
}


class SplitView(Gtk.Box):
    """A fixed 2x2 skeleton of console notebooks, most of them hidden."""

    __gsignals__ = {
        # (notebook, page widget, page index) -- forwarded from whichever
        # notebook emitted it, so the window does not have to track four.
        "page-switched": (GObject.SignalFlags.RUN_FIRST, None, (object, object, int)),
        # The number of visible panes changed.
        "panes-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A different pane is the one the window should be acting on.
        "pane-activated": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.notebooks = []
        for _pane in range(MAX_PANES):
            notebook = Gtk.Notebook()
            notebook.set_scrollable(True)
            notebook.set_group_name(GROUP_NAME)
            notebook.connect("switch-page", self._on_switch_page)
            notebook.connect("page-added", self._on_pages_changed)
            notebook.connect("page-removed", self._on_pages_changed)
            notebook.set_no_show_all(True)
            self.notebooks.append(notebook)

        self.left = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.left.pack1(self.notebooks[0], True, False)
        self.left.pack2(self.notebooks[2], True, False)

        # no-show-all on both sides: the window's own show_all() must not be
        # able to reveal a pane that has nothing in it, and once pane 0 can
        # be hidden that applies to the left as much as to the right.
        self.left.set_no_show_all(True)

        self.right = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.right.pack1(self.notebooks[1], True, False)
        self.right.pack2(self.notebooks[3], True, False)
        self.right.set_no_show_all(True)

        self.root = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.root.pack1(self.left, True, False)
        self.root.pack2(self.right, True, False)
        self.pack_start(self.root, True, True, 0)

        # Pane 0 is where consoles land, so it is the one showing at rest.
        self.left.show()
        self.notebooks[0].show()
        self.active = self.notebooks[0]
        self._suppress = False
        self._watch_clicks()

    # -- structure -----------------------------------------------------

    @property
    def primary(self):
        return self.notebooks[0]

    def visible_notebooks(self):
        return [n for n in self.notebooks if n.get_visible()]

    def pane_count(self):
        return len(self.visible_notebooks())

    def _first_hidden(self):
        for notebook in self.notebooks:
            if not notebook.get_visible():
                return notebook
        return None

    def _sync_visibility(self):
        """Show or hide the containers around whatever has pages in it.

        A pane with nothing in it goes away, which is how a pane closes --
        close or drag out its last tab and it stops taking up room.

        Pane 0 is only special when the whole window is empty. It is where
        consoles land by default, so it has to exist for one to land in; but
        an empty pane 0 next to a pane that still has tabs is not a default,
        it is a hole taking up half the window.
        """
        changed = False
        empty = not any(n.get_n_pages() for n in self.notebooks)
        for index, notebook in enumerate(self.notebooks):
            wanted = notebook.get_n_pages() > 0 or (index == 0 and empty)
            if notebook.get_visible() != wanted:
                notebook.set_visible(wanted)
                changed = True

        for paned, pair in ((self.left, (0, 2)), (self.right, (1, 3))):
            wanted = any(self.notebooks[i].get_visible() for i in pair)
            if paned.get_visible() != wanted:
                paned.set_visible(wanted)
                changed = True

        if changed:
            self._place_dividers()
            if self.active is not None and not self.active.get_visible():
                self.active = self._first_visible()
            self.emit("panes-changed")
        # Outside the check: splitting shows the new pane itself, on its way
        # to putting a page in it, so by the time this runs there is often
        # nothing left to change -- and the marks would never be redrawn.
        self.mark_active()

    def _first_visible(self):
        """Somewhere to aim at, for when the pane in use has just gone."""
        for notebook in self.notebooks:
            if notebook.get_visible():
                return notebook
        return self.primary

    # -- which pane the window is acting on -----------------------------

    def set_active(self, notebook):
        """Aim the window at a pane. True if that changed anything."""
        if notebook is None or notebook is self.active:
            return False
        self.active = notebook
        self.mark_active()
        self.emit("pane-activated")
        return True

    def _watch_clicks(self):
        """Make a click anywhere in a pane the way to put it in charge.

        Clicking is deliberate and hovering is not, which is the whole
        reason this is a gesture and not the window's focus. A SPICE console
        takes the focus when the pointer merely crosses it -- that is how
        the guest gets the keyboard -- so following the focus meant the
        toolbar changed guest under the pointer on the way to a button.

        One gesture for the whole view, and the pane worked out from where
        the press landed. A gesture per notebook only sees presses GTK
        routes to a widget inside that notebook, and much of a summary page
        is labels and blank space with no window of their own to be routed
        to -- so clicking the background of a summary did nothing, while
        clicking a button in it worked.

        In the capture phase, because whatever was clicked still wants the
        click: this sees it on the way down and only takes note of it.
        """
        self._click_gesture = Gtk.GestureMultiPress.new(self)
        self._click_gesture.set_button(0)  # any button, middle and right too
        self._click_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._click_gesture.connect("pressed", self._on_pressed)

    def _on_pressed(self, _gesture, _presses, x, y):
        notebook = self.pane_at(x, y)
        if notebook is not None:
            self.set_active(notebook)

    def pane_at(self, x, y):
        """Which visible pane covers a point, in this view's coordinates."""
        for notebook in self.visible_notebooks():
            origin = notebook.translate_coordinates(self, 0, 0)
            if origin is None:
                continue
            left, top = origin
            allocation = notebook.get_allocation()
            inside = (
                left <= x < left + allocation.width
                and top <= y < top + allocation.height
            )
            if inside:
                return notebook
        return None

    def mark_active(self):
        """Say on the tabs themselves which console the window is acting on.

        Only when there is more than one pane. With one, the tab in front is
        the one in front and saying so twice is noise.
        """
        split = self.pane_count() > 1
        for notebook in self.notebooks:
            current = notebook.get_nth_page(notebook.get_current_page())
            for page in notebook.get_children():
                label = notebook.get_tab_label(page)
                if hasattr(label, "set_current"):
                    label.set_current(
                        split and notebook is self.active and page is current
                    )

    def _place_dividers(self):
        """Give a pane that has just appeared half of what it splits."""
        width = max(self.root.get_allocated_width(), 400)
        height = max(self.left.get_allocated_height(), 300)
        if self.right.get_visible():
            self.root.set_position(width // 2)
        for paned, visible_pair in ((self.left, (0, 2)), (self.right, (1, 3))):
            top, bottom = (self.notebooks[i] for i in visible_pair)
            if top.get_visible() and bottom.get_visible():
                paned.set_position(height // 2)

    def split_mode(self):
        """Which of the named arrangements is on screen, if any."""
        visible = [i for i, n in enumerate(self.notebooks) if n.get_visible()]
        if len(visible) < 2:
            return SPLIT_NONE
        if visible == [0, 1]:
            return SPLIT_SIDE_BY_SIDE
        if visible == [0, 2]:
            return SPLIT_STACKED
        return SPLIT_OTHER

    def next_split_mode(self):
        return NEXT_SPLIT_MODE[self.split_mode()]

    def set_split_mode(self, mode):
        """Arrange the panes as `mode` says. True if anything moved.

        Always from one pane outwards, whatever was on screen before: the
        arrangement then matches its name exactly, rather than being that
        name plus whatever a dragged tab left behind.
        """
        if mode == SPLIT_NONE:
            if self.pane_count() <= 1:
                return False
            self.gather()
            return True

        target = _MODE_TARGETS.get(mode)
        if target is None:
            return False

        page = self.current_page()
        self.gather()
        # Only worth doing with something to leave behind: moving the one
        # console there is out of pane 0 empties it, and an empty pane
        # hides itself, so the "split" would be one pane in a new place.
        if page is None or self.primary.get_n_pages() < 2:
            return False
        return self.split(page, self.notebooks[target]) is not None

    def split(self, page, target=None):
        """Move a page into a pane of its own. Returns the new notebook.

        The page moves rather than a blank pane opening, because an empty
        notebook has no tab strip and so nothing could be dragged into it.
        """
        source = self.notebook_of(page)
        if source is None:
            return None
        if target is None:
            target = self._first_hidden()
        if target is None or target is source:
            return None  # already at four, or already there

        label = source.get_tab_label(page)
        title = getattr(label, "title", None)
        self._suppress = True
        try:
            source.remove(page)
            # A fresh label: the old one belongs to the page that was just
            # removed and carries a close callback bound to it, which is
            # still correct, so it is reused when there is one.
            target.append_page(
                page,
                label if label is not None else Gtk.Label(label=title or "console"),
            )
            target.set_tab_reorderable(page, True)
            target.set_tab_detachable(page, True)
        finally:
            self._suppress = False
        # Active before the sync, not after: the sync is what redraws the
        # marks, and it can only mark the right tab if it knows which pane
        # the page has just gone to.
        self.active = target
        self._present(target, page)
        target.set_current_page(target.page_num(page))
        self._sync_visibility()
        return target

    def gather(self):
        """Bring every console back into pane 0, closing the other panes."""
        for notebook in self.notebooks[1:]:
            for page in list(notebook.get_children()):
                label = notebook.get_tab_label(page)
                self._suppress = True
                try:
                    notebook.remove(page)
                    self.primary.append_page(
                        page, label if label is not None else Gtk.Label(label="console")
                    )
                    self.primary.set_tab_reorderable(page, True)
                    self.primary.set_tab_detachable(page, True)
                finally:
                    self._suppress = False
                self._present(self.primary, page)
        self.active = self.primary
        self._sync_visibility()

    def _present(self, notebook, page):
        """Make a page and its pane visible.

        Not notebook.show_all(): the notebooks carry no-show-all so that the
        window's own show_all() cannot reveal the three empty panes, and
        show_all() on a widget with that flag returns without doing
        anything -- including to its children. The page is shown directly
        instead, which is the part that actually has to happen: a notebook
        does not count an invisible child as a page at all, and reports no
        current page when every child is hidden.
        """
        page.show_all()
        label = notebook.get_tab_label(page)
        if label is not None:
            label.show_all()
        notebook.show()
        # The paned around it as well: both are hideable, and a shown
        # notebook inside a hidden container is still not on screen.
        for paned, pair in ((self.left, (0, 2)), (self.right, (1, 3))):
            if notebook in (self.notebooks[i] for i in pair):
                paned.show()

    # -- pages ---------------------------------------------------------

    def notebook_of(self, page):
        for notebook in self.notebooks:
            if notebook.page_num(page) >= 0:
                return notebook
        return None

    def all_pages(self):
        """Every page in every pane, pane by pane and then in tab order."""
        pages = []
        for notebook in self.notebooks:
            pages.extend(notebook.get_children())
        return pages

    def total_pages(self):
        return sum(n.get_n_pages() for n in self.notebooks)

    def append(self, page, label, notebook=None):
        """Add a page to a pane -- the active one unless told otherwise."""
        notebook = notebook or self.active or self.primary
        # _present shows whichever pane this lands in, so a hidden pane 0 is
        # a fine target: it comes back for the console being put into it.
        index = notebook.append_page(page, label)
        notebook.set_tab_reorderable(page, True)
        notebook.set_tab_detachable(page, True)
        self._present(notebook, page)
        notebook.set_current_page(index)
        self._sync_visibility()
        return index

    def insert(self, page, label, notebook, position):
        index = notebook.insert_page(page, label, position)
        notebook.set_tab_reorderable(page, True)
        notebook.set_tab_detachable(page, True)
        self._present(notebook, page)
        self._sync_visibility()
        return index

    def remove_page(self, page):
        """Take a page out of whichever pane holds it."""
        notebook = self.notebook_of(page)
        if notebook is None:
            return False
        notebook.remove_page(notebook.page_num(page))
        self._sync_visibility()
        return True

    def detach(self, page):
        """Remove a page but keep the widget alive, for a pop-out."""
        notebook = self.notebook_of(page)
        if notebook is None:
            return False
        notebook.remove(page)
        self._sync_visibility()
        return True

    def focus_page(self, page):
        notebook = self.notebook_of(page)
        if notebook is None:
            return False
        notebook.set_current_page(notebook.page_num(page))
        self.active = notebook
        return True

    def current_page(self):
        """The page in front, in the pane that was last used."""
        notebook = self.active or self.primary
        if not notebook.get_visible():
            # Pane 0 can be the hidden one now, so falling back to it is not
            # enough -- that would report "no page" while a console is on
            # screen in another pane.
            notebook = self._first_visible()
        index = notebook.get_current_page()
        if index < 0:
            return None
        return notebook.get_nth_page(index)

    def set_show_tabs(self, shown):
        for notebook in self.notebooks:
            notebook.set_show_tabs(shown)

    # -- signals -------------------------------------------------------

    def _on_switch_page(self, notebook, page, index):
        if self._suppress:
            return
        self.active = notebook
        # Before the signal: whoever handles it may read the tabs, and a
        # stale mark on the tab that was in front is exactly the thing this
        # is for.
        self.mark_active()
        self.emit("page-switched", notebook, page, index)

    def _on_pages_changed(self, _notebook, _child, _index):
        if self._suppress:
            return
        # A tab dragged into another pane empties the one it left, and a
        # pane with nothing in it should not keep taking up half the window.
        self._sync_visibility()
