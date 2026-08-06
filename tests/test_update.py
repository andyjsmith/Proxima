"""Update checking, and the log file the app writes.

Neither of these touches the network or the real log directory: the version
comparison and the release-note flattening are pure, and the log setup is
pointed at a temporary directory.
"""

import logging

import pytest

from proxima import logs, update
from proxima.ui.update_dialog import plain_notes

# newer, older, what the case is about
VERSIONS = [
    ("0.2.0", "0.1.0", "a plain bump"),
    ("1.0.0", "0.9.9", "crossing a major"),
    ("0.1.10", "0.1.9", "ten is after nine, not before it"),
    ("v0.2.0", "0.1.0", "a leading v is not part of the version"),
    ("0.2.0", "0.2.0-rc1", "the release beats its own candidate"),
]

NOT_NEWER = [
    ("0.1.0", "0.1.0", "the same version"),
    ("0.1.0", "0.2.0", "an older release"),
    ("0.2", "0.2.0", "the same version, written shorter"),
    ("0.2.0-rc1", "0.2.0", "a candidate for something already released"),
    ("", "0.1.0", "no version at all"),
    ("garbage", "0.1.0", "something unparseable"),
    ("0.2.0", "0.0.0+unknown", "a build that does not know what it is"),
]


@pytest.mark.parametrize(
    ("candidate", "current"),
    [(a, b) for a, b, _ in VERSIONS],
    ids=[note for _, _, note in VERSIONS],
)
def test_a_newer_release_is_offered(candidate, current):
    assert update.is_newer(candidate, current)


@pytest.mark.parametrize(
    ("candidate", "current"),
    [(a, b) for a, b, _ in NOT_NEWER],
    ids=[note for _, _, note in NOT_NEWER],
)
def test_nothing_else_is_offered(candidate, current):
    assert not update.is_newer(candidate, current)


def test_build_metadata_is_not_part_of_the_version():
    assert update.parse_version("0.1.0+g1234")[0] == update.parse_version("0.1.0")[0]


def test_a_source_checkout_never_checks_by_itself():
    """The version in a checkout is whatever pyproject says, which is
    routinely older than the tree -- so the answer would be noise."""
    config = {"check_updates": True}
    assert not update.should_check(config, automatic=True), (
        "a source checkout would have nagged about its own version"
    )
    # Asking by hand is a different question, and it gets an answer.
    assert update.should_check(config, automatic=False)


def test_the_setting_switches_the_automatic_check_off():
    assert not update.should_check({"check_updates": False}, automatic=True)


def test_release_notes_are_flattened_rather_than_shown_as_markdown():
    notes = plain_notes(
        "## What's New\n"
        "- **Faster** consoles\n"
        "* A [link](https://example.com) to somewhere\n"
        "`code`\n"
    )
    assert "##" not in notes
    assert "**" not in notes
    assert "`" not in notes
    assert "• Faster consoles" in notes
    assert "• A link to somewhere" in notes
    assert "https://example.com" not in notes, "the raw URL was left in the text"


def test_empty_notes_still_say_something():
    assert plain_notes("").strip()


def test_long_notes_are_cut_rather_than_filling_the_screen():
    flattened = plain_notes("x" * 20000, limit=100)
    assert len(flattened) < 200
    assert "truncated" in flattened


# -- logging ------------------------------------------------------------


def test_the_log_directory_is_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv(logs.ENV_DIR, str(tmp_path / "somewhere"))
    assert logs.log_dir() == tmp_path / "somewhere"


def test_only_the_last_few_runs_are_kept(tmp_path):
    """A log per run, and a directory that does not grow for ever."""
    for index in range(9):
        path = tmp_path / f"{logs.PREFIX}2026010{index}-000000{logs.SUFFIX}"
        path.write_text(f"run {index}", encoding="utf-8")
    # Something else in there is not ours to delete.
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")

    logs.prune(tmp_path, keep=5)

    remaining = sorted(p.name for p in tmp_path.glob(f"{logs.PREFIX}*"))
    assert len(remaining) == 5, remaining
    # The five kept are the newest five, by the timestamp in the name.
    assert remaining[0].endswith("20260104-000000.log")
    assert (tmp_path / "notes.txt").exists(), "pruning ate a file that was not a log"


def test_a_long_run_ages_out_as_one_run_not_as_many(tmp_path):
    """A run that rolled its file over is still one run.

    Counting the rolled chunks as runs of their own would push four real
    runs out of a five-run directory the first time somebody left a console
    open long enough to fill 10 MB.
    """
    for index in range(3):
        stamp = f"2026010{index}-000000"
        (tmp_path / f"{logs.PREFIX}{stamp}{logs.SUFFIX}").write_text("x")
        (tmp_path / f"{logs.PREFIX}{stamp}{logs.SUFFIX}.1").write_text("x")

    logs.prune(tmp_path, keep=2)

    kept = sorted(p.name for p in tmp_path.iterdir())
    assert len(kept) == 4, kept
    assert all("20260100" not in name for name in kept), (
        f"the oldest run was not removed whole: {kept}"
    )
    assert f"{logs.PREFIX}20260102-000000{logs.SUFFIX}.1" in kept, (
        "a surviving run lost its rolled-over chunk"
    )


def test_the_file_handler_rolls_over_rather_than_growing_for_ever(tmp_path):
    """The cap that stops a fortnight-long session filling a disk."""
    import logging.handlers

    assert logs.MAX_BYTES <= 16 * 1024 * 1024, "the per-run cap is too generous"
    handler = logging.handlers.RotatingFileHandler(
        tmp_path / f"{logs.PREFIX}20260101-000000{logs.SUFFIX}",
        encoding="utf-8",
        maxBytes=200,
        backupCount=logs.KEEP_CHUNKS,
    )
    logger = logging.getLogger("proxima.test.rollover")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for index in range(200):
            logger.error("a line that takes up room, number %d", index)
    finally:
        logger.removeHandler(handler)
        handler.close()

    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == logs.KEEP_CHUNKS + 1, files
    assert all(p.stat().st_size < 2000 for p in tmp_path.iterdir()), (
        "the log grew past its cap"
    )


def test_pruning_an_unwritable_directory_does_not_raise(tmp_path):
    assert logs.prune(tmp_path / "does-not-exist") == 0


def test_glib_levels_map_onto_python_levels():
    # The flags are a bitfield; the mapping has to pick the most serious bit
    # set, not the first one it happens to look at.
    assert logs._glib_level(0b001000) == logging.CRITICAL
    assert logs._glib_level(0b010000) == logging.WARNING
    assert logs._glib_level(0b100000) == logging.INFO
    assert logs._glib_level(0b10000000) == logging.DEBUG
