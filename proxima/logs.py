"""Where Proxima writes down what it did.

A packaged Windows build is a GUI subsystem executable, which means it has
no stdout and no stderr at all: every diagnostic the app prints goes
nowhere, and a fault that only happens in a packaged build is invisible by
construction. That is what this file exists to fix, so it has to hold up
under the conditions that make it necessary:

  * Nothing here may raise. A log directory that cannot be created is a
    reason to log to one file less, never a reason for the app not to start,
    so every step falls back and the last fallback is "no file handler".
  * One file per run rather than a rotating one. Runs are the unit a bug
    report is about ("I opened it, clicked the VM, nothing happened"), and a
    size-rotated file cuts across them. Old runs are pruned on the way in.
  * GLib's own messages are routed in as well. The most useful line in a
    SPICE fault is usually GSpice's, and it never passed through Python.

Deliberately dependency-free and importable before gi, because it is set up
before anything else so that anything else can be logged.
"""

import contextlib
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Runs to keep, this one included.
KEEP_RUNS = 5

PREFIX = "proxima-"
SUFFIX = ".log"

ENV_DIR = "PROXIMA_LOG_DIR"
ENV_LEVEL = "PROXIMA_LOG_LEVEL"

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"

# Set by setup(), so the About box and --diagnose can say where it went.
_log_file = None
_configured = False
_verbose = False


def verbose():
    """Whether this run was asked for debug-level detail."""
    return _verbose


def log_dir():
    """Where this platform expects an application to keep its logs.

    An explicit override comes first, which is what keeps a test run and a
    portable install out of the real one.
    """
    override = os.environ.get(ENV_DIR)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "Proxima" / "logs"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Proxima"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
        return Path(base) / "proxima" / "logs"
    return Path.home() / ".proxima" / "logs"


def current_log_file():
    """The file this run is writing to, or None if none could be opened."""
    return _log_file


def _writable_dir():
    """The log directory, created, or None if nowhere will take it.

    Falls back in the order the user would want asked about: the proper
    place, then next to the settings, then the system temp directory. A
    machine where all three fail has bigger problems than logging.
    """
    from .config import config_dir

    for candidate in (log_dir(), config_dir() / "logs", Path(tempfile.gettempdir())):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Being able to name a directory is not the same as being able to
            # write in it -- an installed-for-everyone directory is the usual
            # way to find that out the hard way.
            probe = candidate / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return None


def prune(directory, keep=KEEP_RUNS):
    """Delete all but the newest `keep` run logs. Returns how many went."""
    try:
        # By name as well as by time: the name carries a fixed-width
        # timestamp, so it breaks the tie when several runs share an mtime
        # -- which is what happens on a filesystem with a coarse clock, and
        # what would otherwise make "the newest five" arbitrary.
        files = sorted(
            (path for path in Path(directory).glob(f"{PREFIX}*{SUFFIX}")),
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for path in files[max(keep, 0) :]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _level(explicit=None):
    name = explicit or os.environ.get(ENV_LEVEL) or "INFO"
    resolved = logging.getLevelName(str(name).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def setup(level=None, to_console=True):
    """Configure the root logger. Safe to call twice; the second does nothing.

    Returns the path being written to, or None when only the console is
    getting anything.
    """
    global _log_file, _configured, _verbose
    if _configured:
        return _log_file
    _configured = True
    _verbose = _level(level) <= logging.DEBUG

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # sys.stderr is None in a GUI-subsystem build, and a StreamHandler over
    # None fails on the first record rather than at construction.
    if to_console and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(_level(level))
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        root.addHandler(console)

    directory = _writable_dir()
    if directory is not None:
        prune(directory, KEEP_RUNS - 1)  # this run is about to add one
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"{PREFIX}{stamp}{SUFFIX}"
        try:
            handler = logging.FileHandler(path, encoding="utf-8", delay=False)
        except OSError:
            pass
        else:
            # The file always gets everything: the level switch is about how
            # much is worth watching go by, not about what is worth keeping
            # when something has already gone wrong.
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(FILE_FORMAT))
            root.addHandler(handler)
            _log_file = path

    logging.captureWarnings(True)
    _install_excepthooks()
    return _log_file


def log_environment():
    """One block at the top of every run, so a report carries its context."""
    from . import APP_NAME, __version__, bundle

    log = logging.getLogger("proxima")
    log.info("%s %s starting", APP_NAME, __version__)
    log.info(
        "python %s on %s (%s)",
        sys.version.split()[0],
        sys.platform,
        "packaged build" if bundle.is_bundled() else "source checkout",
    )
    log.info("log file: %s", _log_file or "none")


def _install_excepthooks():
    """Send anything that escapes to the log instead of to a dead stderr."""
    log = logging.getLogger("proxima")

    previous = sys.excepthook

    def hook(kind, value, trace):
        log.critical("unhandled exception", exc_info=(kind, value, trace))
        with contextlib.suppress(Exception):
            previous(kind, value, trace)

    sys.excepthook = hook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook


# --------------------------------------------------------------------------
# GLib
#
# GTK, GStreamer and spice-gtk all log through GLib rather than through
# Python, and in a packaged build those messages go to a stderr that does not
# exist. Redirecting them is what makes "GSpice-WARNING: ..." survive to the
# log file, which is usually the line that says what actually went wrong.
# --------------------------------------------------------------------------

# GLogLevelFlags -> logging level. The flags are a bitfield, checked from the
# most serious down.
_GLIB_LEVELS = (
    (0b000100, logging.CRITICAL),  # ERROR (fatal)
    (0b001000, logging.CRITICAL),  # CRITICAL
    (0b010000, logging.WARNING),  # WARNING
    (0b100000, logging.INFO),  # MESSAGE
    (0b1000000, logging.INFO),  # INFO
    (0b10000000, logging.DEBUG),  # DEBUG
)


def glib_debug_domains():
    """Which GLib domains may log below WARNING, from G_MESSAGES_DEBUG.

    Installing a writer function replaces GLib's own filtering, and GLib's
    own filtering is the only thing standing between this log file and
    every registry key GIO reads at startup -- roughly a thousand lines of
    it on Windows. So the rule is reimplemented: 'all', or a space-separated
    list of domains, and nothing else gets to be chatty.
    """
    setting = (os.environ.get("G_MESSAGES_DEBUG") or "").strip()
    if setting == "all":
        return True
    return set(setting.split())


def bridge_glib(verbose=False):
    """Route GLib's log domains into logging. Call once, after gi imports."""
    try:
        from gi.repository import GLib
    except Exception:  # pragma: no cover -- no gi means nothing to bridge
        return False

    allowed = True if verbose else glib_debug_domains()

    def chatty_ok(domain):
        return allowed is True or domain in allowed

    def writer(level, fields, _n_fields=None, _user_data=None):
        try:
            mapped = _glib_level(level)
            domain = _field(fields, "GLIB_DOMAIN") or "GLib"
            if mapped < logging.WARNING and not chatty_ok(domain):
                return GLib.LogWriterOutput.HANDLED
            message = _field(fields, "MESSAGE") or ""
            logging.getLogger(f"glib.{domain}").log(mapped, "%s", message)
        except Exception:  # pragma: no cover -- a log writer must not raise
            pass
        return GLib.LogWriterOutput.HANDLED

    try:
        GLib.log_set_writer_func(writer)
    except Exception:
        return False
    return True


def _glib_level(level):
    for flag, mapped in _GLIB_LEVELS:
        if level & flag:
            return mapped
    return logging.INFO


def _field(fields, name):
    """Read one structured field out of a GLib log entry.

    GLogField.value is a void*, and PyGObject hands it over as the integer
    address rather than as text -- so the message has to be read out of
    memory. A length of -1 means the usual nul-terminated UTF-8; anything
    else is a counted buffer.
    """
    import ctypes

    for field in fields:
        if field.key != name:
            continue
        value = field.value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, str):
            return value
        if not isinstance(value, int) or not value:
            return None
        with contextlib.suppress(Exception):
            length = field.length
            if length is None or length < 0:
                raw = ctypes.c_char_p(value).value or b""
            else:
                raw = ctypes.string_at(value, length)
            return raw.decode("utf-8", "replace")
        return None
    return None
