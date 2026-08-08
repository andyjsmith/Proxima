"""One node, one tab: its summary and its shell.

The same two-sided stack a guest's tab is, and for the same reason -- the tab
belongs to the node, so the shell can come and go underneath it without the
tab moving or the figures on the summary being lost.

What a node's tab deliberately does not have is a picture of its console.
A guest's summary shows the last frame of its screen because that is what a
guest *is* from here; a node's shell is a terminal somebody opened a moment
ago, and a thumbnail of a prompt says nothing worth the space. The summary
uses the whole width for the figures instead.
"""

from .guest_tab import CONSOLE, SUMMARY, ViewTab


class NodeTab(ViewTab):
    """A node's summary and its shell, one showing at a time."""

    def __init__(self, node_key, summary, view=SUMMARY):
        super().__init__(summary, view=view)
        self.node_key = node_key

    def follow_node_state(self, node):
        """An offline node has no shell worth looking at.

        Unlike a guest, a node coming back does *not* pull the console
        forward: the shell is not what a node's tab is for, and a session
        that dropped when the node went away is dead in any case -- there is
        nothing on that side but a closed terminal until somebody asks for a
        new one.
        """
        if node is None or node.online:
            return
        if self.view == CONSOLE:
            self.show_view(SUMMARY)
