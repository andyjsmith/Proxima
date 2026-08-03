#!/usr/bin/env python3
"""Ask a real server whether anyone is on a VM's SPICE console.

The "another client is already connected" feature rests entirely on one API
call, and the only thing that can settle whether it works here is the
server's own answer. This prints that answer verbatim.

    python3 tools/check_spice_session.py            list running VMs
    python3 tools/check_spice_session.py 100        check VM 100

Saved connections are reused, so it needs no arguments beyond the VMID.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxima import secrets
from proxima.api import ProxmoxError
from proxima.api.connection import Connection
from proxima.api.models import parse_spice_clients
from proxima.config import Config


def connect_all(config):
    connections = []
    for entry in config.get("connections") or []:
        connection = Connection.from_config(entry, secrets.decode)
        if not connection.host:
            continue
        try:
            connection.connect()
        except Exception as exc:
            print(f"  {connection.label}: could not log in - {exc}")
            continue
        connections.append(connection)
    return connections


def main():
    config = Config.load()
    if not config.get("connections"):
        print(
            "No saved connections. Connect once in the app first, with 'Save' ticked."
        )
        return 2

    print("Logging in...")
    connections = connect_all(config)
    if not connections:
        print("No server could be reached.")
        return 2

    wanted = sys.argv[1] if len(sys.argv) > 1 else None

    for connection in connections:
        api = connection.api
        print(f"\n=== {connection.label} (as {api.username}) ===")
        try:
            guests = api.guests()
        except ProxmoxError as exc:
            print(f"  could not list guests: {exc}")
            continue

        running = [g for g in guests if g.kind == "qemu" and g.status == "running"]
        if wanted is None:
            print("  Running VMs (pass one of these VMIDs to check it):")
            for guest in running:
                print(f"    {guest.vmid:<6} {guest.name}  on {guest.node}")
            continue

        target = next((g for g in running if str(g.vmid) == str(wanted)), None)
        if target is None:
            print(f"  VM {wanted} is not running here.")
            continue

        print(f"  VM {target.vmid} ({target.name}) on node {target.node}")
        print("  --- raw reply to 'info spice' ---")
        try:
            text = api.qemu_monitor(target.node, target.vmid, "info spice")
        except ProxmoxError as exc:
            print(f"  REFUSED: {exc}")
            print(f"  HTTP status: {getattr(exc, 'status', None)}")
            print("\n  => The monitor cannot be used with this login.")
            print(
                "     Presence detection cannot work; it needs the "
                "VM.Monitor privilege."
            )
            continue
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue

        for line in text.splitlines():
            print(f"  | {line}")
        print("  --- end ---")

        parsed = parse_spice_clients(text)
        if parsed is None:
            print("\n  => The reply was not recognised as 'info spice' output.")
            print(
                "     Presence detection is reading this as 'cannot "
                "tell' and letting the console open."
            )
            print("     Send the block above and the parser can be taught this format.")
        else:
            count, addresses = parsed
            print(f"\n  => Parsed as {count} connected client(s).")
            if addresses:
                print(f"     Addresses: {', '.join(addresses)}")
            if count:
                print("     Presence detection would prompt before opening.")
            else:
                print("     Presence detection would open without asking.")
                print(
                    "     If a console IS open right now, QEMU is not "
                    "reporting it and the feature cannot work."
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
