"""Locating the two base themes the app ships against.

Only Adwaita and Fluent are supported. Adwaita is compiled into GTK so it is
always present; Fluent has to be installed as a theme directory, and its
variant naming is not consistent between builds (Fluent, Fluent-Dark,
Fluent-dark-compact, ...), so it is resolved by scanning rather than guessed.
"""

import os
import sys
from pathlib import Path

SUPPORTED = ("Adwaita", "Fluent")


def theme_roots():
    """Every directory GTK might load a theme from, deduplicated, in order.

    On MSYS2 there are three competing notions of "home": Python's
    expanduser, MSYS2's $HOME (often a POSIX path like /home/andy), and
    GLib's g_get_home_dir() which GTK actually uses and which prefers
    USERPROFILE. They do not always agree, so check all of them.
    """
    candidates = [
        os.environ.get("GTK_THEME_DIR"),
        Path.home() / ".themes",
        Path.home() / ".local" / "share" / "themes",
    ]

    for var in ("USERPROFILE", "HOME"):
        value = os.environ.get(var)
        if value:
            candidates.append(Path(value) / ".themes")
            candidates.append(Path(value) / ".local" / "share" / "themes")

    if os.environ.get("XDG_DATA_HOME"):
        candidates.append(Path(os.environ["XDG_DATA_HOME"]) / "themes")

    if os.environ.get("MSYSTEM_PREFIX"):
        candidates.append(
            Path(os.environ["MSYSTEM_PREFIX"]) / "share" / "themes")

    candidates += [
        Path(sys.prefix) / "share" / "themes",
        Path("/ucrt64/share/themes"),
        Path("/mingw64/share/themes"),
        Path("/usr/share/themes"),
        Path("/usr/local/share/themes"),
    ]

    seen = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = Path(candidate).expanduser()
        except Exception:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _theme_names_in(root):
    """Theme directory names under root that GTK3 can actually use.

    Some themes ship only variant stylesheets (gtk-dark.css) rather than a
    plain gtk.css, so accept any CSS inside a gtk-3.0 directory.
    """
    names = set()
    try:
        entries = list(root.iterdir())
    except OSError:
        return names

    for entry in entries:
        gtk3 = entry / "gtk-3.0"
        if not gtk3.is_dir():
            continue
        try:
            if any(child.suffix == ".css" for child in gtk3.iterdir()):
                names.add(entry.name)
        except OSError:
            continue
    return names


def installed_themes():
    """Every GTK3 theme present on this machine."""
    names = set()
    for root in theme_roots():
        if root.is_dir():
            names |= _theme_names_in(root)
    names.add("Adwaita")     # compiled into GTK, has no directory
    return sorted(names)


def _pick_variant(names, family, dark):
    """Choose the best-matching variant of a theme family.

    Fluent ships as Fluent, Fluent-Dark, Fluent-round-Dark and so on. Prefer
    an explicitly dark or light name when one exists, prefer a compact
    variant when there is a choice, and otherwise take the plain family name
    and let gtk-application-prefer-dark-theme do the work.
    """
    family_lower = family.lower()
    matches = [n for n in names if n.lower().startswith(family_lower)]
    if not matches:
        return None

    def score(name):
        lowered = name.lower()
        suffix = lowered[len(family_lower):]
        points = 0
        is_dark = "dark" in suffix
        is_light = "light" in suffix
        if dark and is_dark:
            points += 100
        elif not dark and (is_light or not is_dark):
            points += 100
        elif dark and not is_dark:
            points += 10        # usable via prefer-dark-theme
        if "compact" in suffix:
            points += 20
        # Prefer the plainest name at equal footing.
        points -= len(suffix)
        return points

    return max(matches, key=score)


def resolve_theme(preferred, dark, names=None):
    """Map a supported theme name plus a dark flag to a real GTK theme name.

    Returns (gtk_theme_name, was_available). Falls back to Adwaita, which is
    always compiled in, if the requested family is not installed.
    """
    names = installed_themes() if names is None else names

    if preferred == "Adwaita":
        # Adwaita's dark variant is selected via the settings flag, not by
        # name; Adwaita-dark exists but is the legacy spelling.
        return "Adwaita", True

    resolved = _pick_variant(names, preferred, dark)
    if resolved:
        return resolved, True
    return "Adwaita", False


def diagnose():
    """Print exactly where we looked and what we found."""
    print("\n--- theme discovery ---")
    for var in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "MSYSTEM_PREFIX",
                "GTK_THEME_DIR"):
        print(f"  {var:<16} = {os.environ.get(var, '(unset)')}")
    print(f"  {'Path.home()':<16} = {Path.home()}")
    print(f"  {'sys.prefix':<16} = {sys.prefix}")
    print("\n  search roots:")
    for root in theme_roots():
        if not root.is_dir():
            print(f"    [missing] {root}")
            continue
        found = sorted(_theme_names_in(root))
        print(f"    [ok]      {root}")
        print(f"                {', '.join(found) if found else '(none usable)'}")
    print(f"\n  installed: {', '.join(installed_themes())}")
    for family in SUPPORTED:
        for dark in (False, True):
            name, ok = resolve_theme(family, dark)
            state = "" if ok else "  (not installed, falling back)"
            print(f"  {family} {'dark' if dark else 'light':<5} -> {name}{state}")
    print("--- end ---\n")
