---
title: Connecting to a server
sidebar_position: 2
---

# Connecting to a server

Open **File > Connect** (Ctrl+N), or right click empty space in the tree and
choose **Connect**.

![Connect dialog](/img/screenshots/connect_dialog.png)

| Field           |                                                                                                                                                                            |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Server          | `pve.example.com`, `10.0.0.5:8006`, or a full pasted `https://host:8006/#v1:0:...`. Port 8006 is assumed if not provided. Bracketed IPv6 also works: `[2001:db8::5]:8006`. |
| Username        | Without the realm. `root`, not `root@pam`.                                                                                                                                 |
| Realm           | `pam` for Linux PAM accounts, `pve` for Proxmox VE accounts.                                                                                                               |
| Password        | Sent over TLS to the same endpoint used by the web interface.                                                                                                              |
| TFA code        | (Optional) Only if the account has two factor authentication.                                                                                                              |
| Save connection | Reconnect this server the next time Proxima starts. Connection details, including credentials, will be saved to the local computer.                                        |

## Multiple servers

Multiple independent Proxmox VE server can be connected to at once. Each server is a root row in the tree with its own state.

**File > Disconnect** lists the connected servers, and a server can be disconnected from there or by right clicking a server row.

## Certificate pinning

Proxmox uses a self-signed certificate by default. Proxima warns you whenever a certificate is presented that you have not trusted before and is not in your CA store.

1. The first connection to a server shows you the certificate: subject, issuer,
   validity and the SHA-256 fingerprint. **Datacenter > Certificates** in the
   Proxmox web interface shows the same fingerprint, and so does
   `openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256`.
2. Accepting the certificate will pin it to that server locally so you won't be asked to verify again.
3. If that server later presents a different certificate, you will be warned and presented with the new certificate.

Two consequences of pinning:

- **The hostname is not checked while a certificate is pinned.** A Proxmox
  certificate often has a CN that does not match the address you reach it on.
  Under pinning the fingerprint is the identity, so checking the name as well
  would reject the exact certificate you approved.
- **Pins are only applied to self-signed/untrusted certificates.** A valid certificate (trusted by your existing CA store) is verified normally and is not pinned.

If you need to reset a trusted certificate, delete its entry from `trusted_certs` in the [settings file](../configuration/settings-file.md).

### A changed certificate

A renewal and an interception look identical from this end, so the connection
stops and reports the fingerprint it expected against the one it got. After a
genuine renewal, remove that server's entry from `trusted_certs` and connect
again to approve the new certificate.

## Saved connections

Credentials for saved connections are stored locally. How it is stored depends on the platform, and the tooltip on the `Save connection` checkbox will say which applies.

|                 |                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------ |
| Windows         | DPAPI (`CryptProtectData`), encrypted against your Windows account.                        |
| Linux and macOS | Obfuscated, not encrypted. Anyone who can read the settings file can recover the password. |

The settings file also holds the username, realm, host and port for each saved
connection. See [The settings file](../configuration/settings-file.md) for its
location and how to remove a connection by hand.

## After connecting

If **Restore the last session** is on in **Preferences > Behaviour**, the
consoles that were open when the window last closed will reopen and the tree returns
to the way it was expanded.
