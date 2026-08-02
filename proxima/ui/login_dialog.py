"""Connection dialog.

Login runs on a worker thread so a slow or unreachable host cannot freeze the
UI, which is exactly the case where you most want a responsive Cancel.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

from ..api import ProxmoxError, AuthError, TwoFactorRequired
from ..api.connection import Connection, split_host
from ..theme import decorate as theme_decorate
from .. import secrets

REALMS = [
    ("pam", "pam (Linux PAM)"),
    ("pve", "pve (Proxmox VE)"),
]


class LoginDialog(Gtk.Dialog):
    """Collects a host and credentials and returns a connected ProxmoxAPI."""

    def __init__(self, parent, config):
        super().__init__(title="Connect to Proxmox VE", transient_for=parent,
                         modal=True)
        self.config = config
        self.connection = None
        self.api = None
        self._busy = False

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.connect_button = self.add_button("Connect", Gtk.ResponseType.OK)
        self.connect_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(420, -1)

        content = self.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)

        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        content.pack_start(grid, False, False, 0)

        self.host_entry = self._row(grid, 0, "Server", config.get("host", ""),
                                    placeholder="pve.example.com  or  10.0.0.5:8006")
        self.user_entry = self._row(grid, 1, "Username",
                                    config.get("username", "root"))

        grid.attach(self._label("Realm"), 0, 2, 1, 1)
        self.realm_combo = Gtk.ComboBoxText()
        for realm, description in REALMS:
            self.realm_combo.append(realm, description)
        self.realm_combo.set_active_id(config.get("realm", "pam"))
        grid.attach(self.realm_combo, 1, 2, 1, 1)

        grid.attach(self._label("Password"), 0, 3, 1, 1)
        self.password_entry = Gtk.Entry(visibility=False,
                                        input_purpose=Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_activates_default(True)
        self.password_entry.set_hexpand(True)
        grid.attach(self.password_entry, 1, 3, 1, 1)

        grid.attach(self._label("TFA code"), 0, 4, 1, 1)
        self.otp_entry = Gtk.Entry(placeholder_text="if TFA is enabled")
        self.otp_entry.set_activates_default(True)
        grid.attach(self.otp_entry, 1, 4, 1, 1)

        self.verify_check = Gtk.CheckButton(label="Verify TLS certificate")
        self.verify_check.set_active(bool(config.get("verify_ssl", False)))
        self.verify_check.set_tooltip_text(
            "Off by default: Proxmox uses a self-signed certificate")
        content.pack_start(self.verify_check, False, False, 0)

        self.save_check = Gtk.CheckButton(label="Save connection")
        self.save_check.set_active(True)
        self.save_check.set_tooltip_text(
            "Reconnect on startup. The password is stored "
            + ("encrypted for your Windows account."
               if secrets.is_secure()
               else "obfuscated, not encrypted, in the settings file."))
        content.pack_start(self.save_check, False, False, 0)

        self.spinner = Gtk.Spinner()
        self.message = Gtk.Label(xalign=0.0)
        self.message.set_line_wrap(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(self.spinner, False, False, 0)
        row.pack_start(self.message, True, True, 0)
        content.pack_start(row, False, False, 0)

        self.connect("response", self._on_response)
        theme_decorate(self)
        self.show_all()
        self.spinner.hide()

        if self.host_entry.get_text():
            self.password_entry.grab_focus()

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=1.0)
        label.get_style_context().add_class("dim")
        return label

    def _row(self, grid, row, label, value, placeholder=""):
        grid.attach(self._label(label), 0, row, 1, 1)
        entry = Gtk.Entry(text=value or "")
        entry.set_hexpand(True)
        entry.set_activates_default(True)
        if placeholder:
            entry.set_placeholder_text(placeholder)
        grid.attach(entry, 1, row, 1, 1)
        return entry

    def _set_busy(self, busy, text=""):
        self._busy = busy
        self.connect_button.set_sensitive(not busy)
        for widget in (self.host_entry, self.user_entry, self.password_entry,
                       self.otp_entry, self.realm_combo, self.verify_check):
            widget.set_sensitive(not busy)
        self.save_check.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self._set_message(text)

    def _set_message(self, text, error=False):
        if not text:
            self.message.set_text("")
            return
        escaped = GLib.markup_escape_text(text)
        if error:
            self.message.set_markup(
                f"<span foreground='#e01b24'>{escaped}</span>")
        else:
            self.message.set_markup(f"<span alpha='70%'>{escaped}</span>")

    @staticmethod
    def _split_host(value):
        return split_host(value)

    # -- flow ----------------------------------------------------------

    def _on_response(self, _dialog, response):
        if response != Gtk.ResponseType.OK:
            return
        if self._busy:
            return
        self.stop_emission_by_name("response")

        host_value = self.host_entry.get_text().strip()
        if not host_value:
            self._set_message("Enter a server address.", True)
            self.host_entry.grab_focus()
            return

        host, port = self._split_host(host_value)
        username = self.user_entry.get_text().strip()
        realm = self.realm_combo.get_active_id() or "pam"
        password = self.password_entry.get_text()
        otp = self.otp_entry.get_text().strip()
        verify = self.verify_check.get_active()

        self._set_busy(True, f"Connecting to {host}...")

        def worker():
            connection = Connection(host, port=port, username=username,
                                    realm=realm, verify_ssl=verify,
                                    save=self.save_check.get_active())
            try:
                connection.connect(password=password, otp=otp or None)
            except TwoFactorRequired as need:
                GLib.idle_add(self._need_tfa, connection, username, realm,
                              need)
                return
            except (AuthError, ProxmoxError) as exc:
                GLib.idle_add(self._failed, str(exc))
                return
            except Exception as exc:
                GLib.idle_add(self._failed, f"{type(exc).__name__}: {exc}")
                return
            GLib.idle_add(self._succeeded, connection)

        threading.Thread(target=worker, daemon=True,
                         name="proxmox-login").start()

    def _need_tfa(self, connection, username, realm, need):
        self._set_busy(False)
        otp = self.otp_entry.get_text().strip()
        if not otp:
            self._set_message("Two-factor code required.", True)
            self.otp_entry.grab_focus()
            return False

        # A code was supplied but the first attempt still came back asking
        # for one, which means it has to be posted against the partial
        # ticket rather than the password.
        self._set_busy(True, "Verifying code...")

        def worker():
            try:
                connection.api.login_tfa(username, need.partial_ticket, otp,
                                         realm=realm)
            except (AuthError, ProxmoxError) as exc:
                GLib.idle_add(self._failed, str(exc))
                return
            connection.state = "connected"
            GLib.idle_add(self._succeeded, connection)

        threading.Thread(target=worker, daemon=True,
                         name="proxmox-login-tfa").start()
        return False

    def _failed(self, message):
        self._set_busy(False)
        self._set_message(message, True)
        self.password_entry.grab_focus()
        self.password_entry.select_region(0, -1)
        return False

    def _succeeded(self, connection):
        self.connection = connection
        self.api = connection.api
        # Remember the last-used values so the next dialog starts there.
        self.config["host"] = connection.id
        self.config["username"] = connection.username
        self.config["realm"] = connection.realm
        self.config["verify_ssl"] = connection.verify_ssl
        self.config.save()
        self._set_busy(False)
        self.response(Gtk.ResponseType.APPLY)
        return False


def run_login(parent, config):
    """Show the dialog; returns a connected Connection or None."""
    dialog = LoginDialog(parent, config)
    result = None
    while True:
        response = dialog.run()
        if response == Gtk.ResponseType.APPLY:
            result = dialog.connection
            break
        if response in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT,
                        Gtk.ResponseType.NONE):
            break
    dialog.destroy()
    return result
