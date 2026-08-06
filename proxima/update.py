"""Is there a newer Proxima than this one?

Asks GitHub for the latest release and compares it with the running
version. Deliberately small and deliberately quiet:

  * It only ever reads. Nothing here downloads or installs anything -- a
    client that rewrites its own installation is a much bigger promise than
    "there is a new version", and the download is one click away in a
    browser where the user can see what they are getting.
  * A source checkout never checks automatically. The version there is
    whatever pyproject says, which is routinely older than the tree it
    describes, so the answer would be noise.
  * Failure is silence. No network, a rate limit, GitHub being down: none of
    those are the user's problem, and none of them are worth a dialog. The
    reason goes to the log.

The stdlib does the HTTP, for the same reason the API client does: the app
has no pip dependencies to spend.
"""

import json
import logging
import re
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

REPO = "andyjsmith/Proxima"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

TIMEOUT = 10

# A version this client cannot place is one it must not act on.
UNKNOWN = "0.0.0+unknown"

_NUMBERS = re.compile(r"\d+")


def parse_version(text):
    """(numbers, is_release) for comparison. Bad input sorts oldest.

    Only two things need deciding -- which of two releases is newer, and
    whether a prerelease loses to the release of the same number -- so this
    is a comparator, not a version parser. 'v1.2.0' and '1.2.0' are the same
    thing; '1.2.0-rc1' is less than '1.2.0'; build metadata after '+' is not
    part of the version and is dropped.
    """
    text = str(text or "").strip().lstrip("vV")
    text = text.split("+", 1)[0]
    core, _, suffix = text.partition("-")
    numbers = tuple(int(found) for found in _NUMBERS.findall(core)[:4])
    if not numbers:
        return (), False
    # Pad, so 1.2 and 1.2.0 compare equal rather than by length.
    numbers = (numbers + (0, 0, 0, 0))[:4]
    return numbers, not suffix


def is_newer(candidate, current):
    """Whether `candidate` is a version worth telling somebody about."""
    # A build that could not read its own version reports 0.0.0+unknown,
    # which parses as a real 0.0.0 and would therefore be "behind" every
    # release ever made. Not knowing is not the same as being out of date.
    if str(current).strip() == UNKNOWN:
        return False
    left, left_release = parse_version(candidate)
    right, right_release = parse_version(current)
    if not left or not right:
        return False
    if left != right:
        return left > right
    # Same numbers: the release beats the prerelease of it.
    return left_release and not right_release


def latest_release(timeout=TIMEOUT):
    """The newest published release, or None if it could not be found out.

    GitHub's 'latest' excludes prereleases and drafts already, which is the
    behaviour wanted here: a release candidate should not be offered to
    somebody running a stable build.
    """
    request = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"proxima ({REPO})",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.info("could not check for updates: %s", exc)
        return None

    tag = payload.get("tag_name") or payload.get("name") or ""
    if not tag:
        log.info("the latest release has no tag name")
        return None
    return {
        "version": str(tag).lstrip("vV"),
        "tag": str(tag),
        "name": payload.get("name") or str(tag),
        "notes": (payload.get("body") or "").strip(),
        "url": payload.get("html_url") or RELEASES_PAGE,
        "published": payload.get("published_at") or "",
    }


def should_check(config, automatic=True):
    """Whether an automatic check is wanted right now.

    A manual check from the Help menu ignores all of this: asking is the
    whole point of clicking it.
    """
    if not automatic:
        return True
    if not config.get("check_updates", True):
        return False
    from . import __version__, bundle

    if not bundle.is_bundled():
        log.debug("source checkout: not checking for updates")
        return False
    if __version__ == UNKNOWN:
        log.info("this build does not know its own version; not checking")
        return False
    return True


def check(config, on_result, automatic=True):
    """Look for a newer release off-thread.

    on_result(release_or_None, up_to_date) is called from the worker, so a
    UI caller has to hop back to the main thread itself. 'up_to_date' tells
    "asked, and there is nothing newer" apart from "could not ask", which a
    manual check has to be able to say out loud.
    """
    from . import __version__

    if not should_check(config, automatic):
        return False

    def worker():
        release = latest_release()
        if release is None:
            on_result(None, False)
            return
        newer = is_newer(release["version"], __version__)
        log.info(
            "latest release is %s, running %s%s",
            release["version"],
            __version__,
            " -- update available" if newer else "",
        )
        on_result(release if newer else None, True)

    threading.Thread(target=worker, daemon=True, name="update-check").start()
    return True
