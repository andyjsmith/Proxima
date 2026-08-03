"""Client metadata smuggled into a guest's notes.

Proxmox has no concept of folders and no per-guest storage for third-party
settings, but every guest has a free-text description. This carves a
delimited block out of it:

    =====BEGIN PROXIMA=====
    {"folder": ["Production", "Customer A"]}
    =====END PROXIMA=====

Rules that matter for not destroying somebody's notes:

  * The block is found wherever it happens to be, not only at the end, so
    text typed after it survives.
  * Writing replaces the block in place if one exists, and appends otherwise.
  * A malformed or truncated block is treated as absent rather than being
    reinterpreted, so a half-written note is never silently rewritten into
    something else.
"""

import json
import re

BEGIN = "=====BEGIN PROXIMA====="
END = "=====END PROXIMA====="

_BLOCK = re.compile(
    re.escape(BEGIN) + r"\s*(?P<body>.*?)\s*" + re.escape(END), re.DOTALL
)


def parse(notes):
    """Return (metadata dict, notes with the block removed)."""
    text = notes or ""
    match = _BLOCK.search(text)
    if match is None:
        return {}, text.strip()

    try:
        data = json.loads(match.group("body") or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        # Leave a corrupt block alone: the user text around it is still
        # theirs, and guessing at the contents is worse than ignoring them.
        data = {}

    remainder = _tidy(text[: match.start()] + text[match.end() :])
    return data, remainder


def render(metadata):
    body = json.dumps(metadata, sort_keys=True)
    return f"{BEGIN}\n{body}\n{END}"


def _tidy(text):
    """Collapse the blank lines left behind by removing a block."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def update(notes, metadata):
    """Write metadata into notes, preserving everything else.

    An empty metadata dict removes the block entirely rather than leaving an
    inert marker behind.
    """
    text = notes or ""
    match = _BLOCK.search(text)

    if not metadata:
        if match is None:
            return text.strip()
        return _tidy(text[: match.start()] + text[match.end() :])

    block = render(metadata)
    if match is not None:
        return _tidy(text[: match.start()] + block + text[match.end() :])
    return (text.strip() + "\n\n" + block).strip() if text.strip() else block


# -- folders ------------------------------------------------------------


def folder_of(notes):
    """The folder path stored in a guest's notes, as a tuple."""
    metadata, _ = parse(notes)
    path = metadata.get("folder")
    if not isinstance(path, list):
        return ()
    return tuple(str(part) for part in path if str(part).strip())


def with_folder(notes, path):
    """Notes with the folder set, or cleared when path is empty."""
    metadata, _ = parse(notes)
    if path:
        metadata["folder"] = [str(part) for part in path]
    else:
        metadata.pop("folder", None)
    return update(notes, metadata)


# -- per-guest settings -------------------------------------------------
#
# Unlike the client-side console preferences, these belong to the guest
# rather than to this machine: they say how *this VM* should be consoled,
# and everyone who opens it should get the same answer. That is why they
# live on the server, in the same notes block as the folder.
#
# Clipboard has no direction here because spice-gtk has none: SpiceGtkSession
# exposes a single boolean "auto-clipboard", which is bidirectional or
# nothing. "bidirectional" is accepted when reading so that a stored value
# from a future version that does know directions still means "on".

SETTINGS_DEFAULTS = {
    "clipboard": "enabled",
    "audio": "enabled",
    "protocol": "default",
}

_CLIPBOARD_ALIASES = {
    "bidirectional": "enabled",
    "on": "enabled",
    "off": "disabled",
    "true": "enabled",
    "false": "disabled",
}


def settings_of(notes):
    """The Proxmox Manager settings for a guest, with defaults filled in."""
    metadata, _ = parse(notes)
    return normalise_settings(metadata.get("settings"))


def normalise_settings(stored):
    """A stored settings dict, cleaned up and completed from the defaults."""
    settings = dict(SETTINGS_DEFAULTS)
    if not isinstance(stored, dict):
        return settings
    for name, default in SETTINGS_DEFAULTS.items():
        value = stored.get(name)
        if value is None:
            continue
        value = str(value).strip().lower()
        if name == "clipboard":
            value = _CLIPBOARD_ALIASES.get(value, value)
        settings[name] = value
    return settings


def with_settings(notes, settings):
    """Notes with the settings block rewritten.

    Values that match the default are dropped rather than written out, so a
    guest left alone never grows a settings block at all and one reset to
    the defaults loses it again. Notes are user-visible text; this keeps the
    footprint in them to what somebody actually chose.
    """
    metadata, _ = parse(notes)
    settings = normalise_settings(settings)
    trimmed = {
        name: value
        for name, value in settings.items()
        if value != SETTINGS_DEFAULTS[name]
    }
    if trimmed:
        metadata["settings"] = trimmed
    else:
        metadata.pop("settings", None)
    return update(notes, metadata)
