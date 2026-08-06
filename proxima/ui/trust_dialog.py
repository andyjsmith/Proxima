"""Asking whether a server's certificate should be trusted.

Two questions with the same shape and very different weight, so they are
one dialog with two moods:

  * A server seen for the first time. Ordinary, expected, and the answer is
    usually yes -- but the fingerprint is put in front of the user so that
    "usually" is their call rather than the program's.
  * A server whose certificate has changed since it was pinned. Sometimes a
    renewal, sometimes somebody in the middle, and nothing on this end can
    tell which. So the default button is Cancel, the wording says what the
    bad case would mean, and accepting takes a deliberate click.

The fingerprint is selectable and in a monospace face, because the only way
to check it is against what the server says about itself -- the Proxmox web
UI under Certificates, or `openssl x509 -fingerprint -sha256`, both of which
print it in exactly this form.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..theme import decorate as theme_decorate

TRUST = 1
CANCEL = 2


class TrustCertificateDialog(Gtk.Dialog):
    """Show a certificate and ask. Returns TRUST or CANCEL through run()."""

    def __init__(self, parent, host, port, info, mismatch=False, expected=""):
        super().__init__(
            title="Certificate Changed" if mismatch else "Unknown Certificate",
            transient_for=parent,
            modal=True,
        )
        self.info = info or {}

        self.add_button("Cancel", CANCEL)
        accept = self.add_button(
            "Trust the New Certificate" if mismatch else "Trust This Server", TRUST
        )
        if mismatch:
            # No suggested-action styling and Cancel is the default: this is
            # the one case where the safe answer should be the easy one.
            accept.get_style_context().add_class("destructive-action")
            self.set_default_response(CANCEL)
        else:
            accept.get_style_context().add_class("suggested-action")
            self.set_default_response(TRUST)

        self.set_default_size(560, -1)
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(10)

        content.pack_start(self._heading(host, port, mismatch), False, False, 0)
        content.pack_start(self._facts(expected, mismatch), False, False, 0)
        theme_decorate(self)
        self.show_all()

    # -- contents ------------------------------------------------------

    @staticmethod
    def _heading(host, port, mismatch):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(
            "dialog-warning-symbolic" if mismatch else "channel-secure-symbolic",
            Gtk.IconSize.DIALOG,
        )
        icon.set_valign(Gtk.Align.START)
        box.pack_start(icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(xalign=0.0)
        where = GLib.markup_escape_text(f"{host}:{port}")
        if mismatch:
            title.set_markup(
                f"<b>The certificate for {where} is not the one you trusted.</b>"
            )
        else:
            title.set_markup(f"<b>{where} has not been seen before.</b>")
        title.set_line_wrap(True)
        text.pack_start(title, False, False, 0)

        detail = Gtk.Label(xalign=0.0)
        detail.set_line_wrap(True)
        if mismatch:
            detail.set_text(
                "This happens when a certificate is renewed or replaced -- "
                "and it is also what somebody intercepting the connection "
                "would look like. Nothing here can tell the two apart, so "
                "check the fingerprint against the server before accepting."
            )
        else:
            detail.set_text(
                "Proxmox signs its own certificate, so there is no authority "
                "to vouch for it. Check the fingerprint below against the "
                "server -- Datacenter > Certificates in the Proxmox web "
                "interface shows the same value -- and it will be remembered "
                "for next time."
            )
        text.pack_start(detail, False, False, 0)
        box.pack_start(text, True, True, 0)
        return box

    def _facts(self, expected, mismatch):
        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        grid.set_margin_top(4)

        rows = [("SHA-256", self.info.get("sha256", "unknown"))]
        if mismatch and expected:
            rows.append(("Trusted before", expected))
        for label, value in (
            ("Subject", self.info.get("subject")),
            ("Issuer", self.info.get("issuer")),
            ("Expires", self.info.get("not_after")),
        ):
            if value:
                rows.append((label, value))

        for row, (label, value) in enumerate(rows):
            caption = Gtk.Label(label=label, xalign=1.0)
            caption.get_style_context().add_class("dim")
            caption.set_valign(Gtk.Align.START)
            grid.attach(caption, 0, row, 1, 1)

            shown = Gtk.Label(label=value, xalign=0.0)
            shown.set_selectable(True)
            shown.set_line_wrap(True)
            shown.set_line_wrap_mode(2)  # WRAP_CHAR: a fingerprint has no words
            shown.set_hexpand(True)
            if label.startswith("SHA-256") or label == "Trusted before":
                shown.get_style_context().add_class("mono")
            grid.attach(shown, 1, row, 1, 1)
        return grid


def ask(parent, host, port, info, mismatch=False, expected=""):
    """Put the question and wait. True if the user accepted."""
    dialog = TrustCertificateDialog(
        parent, host, port, info, mismatch=mismatch, expected=expected
    )
    try:
        return dialog.run() == TRUST
    finally:
        dialog.destroy()
