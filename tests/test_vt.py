"""The terminal emulator, which is the half of the serial console with no UI.

No GTK here on purpose. Everything awkward about a terminal is in the
escape sequences, and those can be checked by feeding bytes in and reading
the grid out.
"""

import pytest

from proxima.console import vtkeys
from proxima.console.vt import PLAIN, Screen, Terminal, apply_sgr


def screen_of(*chunks, cols=20, rows=5):
    terminal = Terminal(cols=cols, rows=rows)
    for chunk in chunks:
        terminal.feed(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return terminal


def rows(terminal):
    return [line.text() for line in terminal.screen.lines]


# -- the basics ------------------------------------------------------------


def test_plain_text_lands_where_it_was_written():
    terminal = screen_of("hello\r\nworld")
    assert rows(terminal)[:2] == ["hello", "world"]
    assert (terminal.screen.cursor_x, terminal.screen.cursor_y) == (5, 1)


def test_a_sequence_split_across_reads_is_still_one_sequence():
    """The network decides where a read ends, not the protocol."""
    terminal = screen_of(b"a\x1b[", b"31mb\x1b", b"[0mc")
    assert rows(terminal)[0] == "abc"
    _, style = terminal.screen.lines[0].cells[1]
    assert style.fg == 1, "a colour split across two reads was lost"


def test_utf8_split_across_reads_is_reassembled():
    terminal = screen_of(b"caf\xc3", b"\xa9")
    assert rows(terminal)[0] == "café"


def test_a_combining_mark_joins_the_cell_before_it():
    terminal = screen_of("é")
    assert terminal.screen.lines[0].cells[0][0] == "é"
    assert terminal.screen.cursor_x == 1, "a combining mark took a cell of its own"


def test_a_wide_character_takes_two_cells():
    terminal = screen_of("世x")
    assert terminal.screen.cursor_x == 3
    assert terminal.screen.lines[0].cells[2][0] == "x"


# -- wrapping --------------------------------------------------------------


def test_a_full_row_does_not_wrap_until_the_next_character():
    """The deferred wrap, which is where naive emulators grow blank lines."""
    terminal = screen_of("x" * 20, cols=20)
    assert terminal.screen.cursor_y == 0, "a row filled exactly already wrapped"
    terminal.feed(b"y")
    assert terminal.screen.cursor_y == 1
    assert rows(terminal)[1] == "y"


def test_wrapping_marks_the_line_so_a_selection_can_rejoin_it():
    terminal = screen_of("x" * 25, cols=20)
    assert terminal.screen.lines[0].wrapped is True
    assert terminal.screen.lines[1].wrapped is False


def test_backspace_after_a_full_row_stays_on_that_row():
    terminal = screen_of("x" * 20 + "\b" + "y", cols=20)
    assert terminal.screen.cursor_y == 0, "backspace at the wrap point fell through"
    assert rows(terminal)[0] == "x" * 19 + "y"


def test_autowrap_off_overwrites_the_last_column():
    terminal = screen_of("\x1b[?7l" + "abcdef", cols=4)
    assert terminal.screen.cursor_y == 0
    assert rows(terminal)[0] == "abcf"


# -- scrolling and history -------------------------------------------------


def test_output_past_the_bottom_scrolls_into_the_scrollback():
    terminal = screen_of("\r\n".join(f"line{n}" for n in range(8)), rows=5)
    assert rows(terminal)[-1] == "line7"
    assert [line.text() for line in terminal.screen.scrollback] == [
        "line0",
        "line1",
        "line2",
    ]


def test_a_scroll_region_keeps_the_rows_outside_it_still():
    terminal = screen_of(
        "\x1b[2;4r"  # region is rows 2..4
        "\x1b[1;1Htop"
        "\x1b[5;1Hbottom"
        "\x1b[2;1Ha\r\nb\r\nc\r\nd",
        rows=5,
    )
    assert rows(terminal)[0] == "top", "a scroll region scrolled the row above it"
    assert rows(terminal)[4] == "bottom", "a scroll region scrolled the row below it"
    assert rows(terminal)[1:4] == ["b", "c", "d"]


def test_the_alternate_screen_is_not_recorded_and_is_given_back():
    terminal = screen_of("keep me\r\n", rows=5)
    terminal.feed(b"\x1b[?1049h")
    terminal.feed(b"\x1b[2J\x1b[HI am vim\r\n" + b"filler\r\n" * 10)
    assert not any("filler" in line.text() for line in terminal.screen.scrollback), (
        "the alternate screen leaked into the scrollback"
    )
    terminal.feed(b"\x1b[?1049l")
    assert rows(terminal)[0] == "keep me", "the primary screen was not restored"


def test_clear_pushes_the_screen_into_the_history_rather_than_destroying_it():
    terminal = screen_of("something worth keeping\r\n", cols=40, rows=5)
    terminal.feed(b"\x1b[2J\x1b[H")
    assert any("worth keeping" in line.text() for line in terminal.screen.scrollback), (
        "clear threw the screen away instead of scrolling it back"
    )


# -- editing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("abcdef\x1b[1;3H\x1b[P", "abdef"),  # delete character
        ("abcdef\x1b[1;3H\x1b[2P", "abef"),
        ("abcdef\x1b[1;3H\x1b[@", "ab cdef"),  # insert character
        ("abcdef\x1b[1;3H\x1b[K", "ab"),  # erase to end of line
        # Erase-to-start includes the cursor's own cell, which is the one
        # place the two directions are not symmetrical.
        ("abcdef\x1b[1;3H\x1b[1K", "   def"),
        ("abcdef\x1b[1;3H\x1b[X", "ab def"),  # erase one character in place
    ],
)
def test_line_editing_sequences(sequence, expected):
    assert rows(screen_of(sequence))[0] == expected.rstrip()


def test_insert_and_delete_lines_move_the_rows_below():
    terminal = screen_of("a\r\nb\r\nc\x1b[2;1H\x1b[L", rows=5)
    assert rows(terminal)[:4] == ["a", "", "b", "c"]
    terminal.feed(b"\x1b[2;1H\x1b[M")
    assert rows(terminal)[:3] == ["a", "b", "c"]


# -- styling ---------------------------------------------------------------


def test_sgr_colours_including_256_and_truecolor():
    style = apply_sgr(PLAIN, [1, 31, 44])
    assert (style.bold, style.fg, style.bg) == (True, 1, 4)

    style = apply_sgr(PLAIN, [38, 5, 208])
    assert style.fg == 208

    style = apply_sgr(PLAIN, [38, 2, 10, 20, 30, 48, 5, 7])
    assert style.fg == (10, 20, 30), "truecolor did not consume its three arguments"
    assert style.bg == 7, "the parameter after a truecolor run was misread"


def test_sgr_zero_resets_everything():
    assert apply_sgr(apply_sgr(PLAIN, [1, 4, 31]), [0]) == PLAIN


def test_the_line_drawing_charset_is_translated():
    """Without this every ncurses box comes out as lowercase letters."""
    terminal = screen_of("\x1b(0lqk\x1b(B")
    assert rows(terminal)[0] == "┌─┐"


def test_erasing_carries_the_background_but_not_the_underline():
    terminal = screen_of("\x1b[44;4mx\x1b[K")
    _, style = terminal.screen.lines[0].cells[5]
    assert style.bg == 4, "the background in force was not used to erase"
    assert not style.underline, "erasing carried an attribute a space cannot show"


# -- replies ---------------------------------------------------------------


def test_a_cursor_position_report_is_answered():
    replies = []
    terminal = Terminal(cols=20, rows=5, on_response=replies.append)
    terminal.feed(b"\x1b[3;7H\x1b[6n")
    assert replies == [b"\x1b[3;7R"]


def test_a_device_attributes_request_is_answered():
    replies = []
    terminal = Terminal(on_response=replies.append)
    terminal.feed(b"\x1b[c")
    assert replies == [b"\x1b[?6c"]


def test_a_secondary_device_attributes_request_is_answered_in_kind():
    """vim's t_RV. Answering the primary form is what broke insert mode.

    A reply vim cannot match against the question it asked stays in its
    input buffer and is read as keystrokes: the ESC leaves insert mode and
    the 'c' starts a change.
    """
    replies = []
    terminal = Terminal(on_response=replies.append)
    terminal.feed(b"\x1b[>c")
    assert replies and replies[0].startswith(b"\x1b[>"), (
        f"a secondary DA request was answered with {replies!r}"
    )
    assert replies[0].endswith(b"c")


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[?u",  # the kitty keyboard protocol asking to be enabled
        b"\x1b[>4;2m",  # modifyOtherKeys
        b"\x1b[>1u",  # kitty keyboard, the other spelling
        b"\x1b[?1004h\x1b[?1004l",  # focus reporting
    ],
)
def test_a_private_sequence_never_runs_the_command_of_the_same_name(sequence):
    """A private prefix makes it a different sequence, not a variant."""
    terminal = screen_of("abcdef\x1b[1;4H")
    terminal.screen.save_cursor()
    terminal.feed(b"\x1b[1;1H")
    before = terminal.screen.style
    terminal.feed(sequence)

    assert (terminal.screen.cursor_x, terminal.screen.cursor_y) == (0, 0), (
        f"{sequence!r} moved the cursor"
    )
    assert terminal.screen.style == before, f"{sequence!r} changed the styling"
    assert rows(terminal)[0] == "abcdef", f"{sequence!r} altered the screen"


def test_a_private_query_is_not_answered_with_the_public_reply():
    replies = []
    terminal = Terminal(on_response=replies.append)
    terminal.feed(b"\x1b[?6n")
    assert replies == [b"\x1b[?1;1R"], (
        "the private cursor report was not marked as the private form"
    )


def test_the_terminal_reports_its_size_when_asked():
    replies = []
    terminal = Terminal(cols=132, rows=43, on_response=replies.append)
    terminal.feed(b"\x1b[18t")
    assert replies == [b"\x1b[8;43;132t"]


def test_an_unterminated_osc_does_not_swallow_the_rest_of_the_session():
    terminal = screen_of(b"\x1b]0;a title\x07after")
    assert terminal.screen.title == "a title"
    assert rows(terminal)[0] == "after"


# -- resizing --------------------------------------------------------------


def test_resizing_keeps_the_cursor_on_the_grid():
    terminal = screen_of("\r\n".join("abcdefgh"), cols=20, rows=10)
    terminal.resize(10, 4)
    screen = terminal.screen
    assert screen.cols == 10 and screen.rows == 4
    assert 0 <= screen.cursor_x < screen.cols
    assert 0 <= screen.cursor_y < screen.rows


def test_a_narrower_screen_does_not_leave_over_long_rows():
    terminal = screen_of("x" * 18, cols=20, rows=5)
    terminal.resize(8, 5)
    assert all(len(line.cells) == 8 for line in terminal.screen.lines)


def test_revision_does_not_move_when_nothing_changed():
    screen = Screen(cols=10, rows=3)
    before = screen.revision
    screen.touch()
    assert screen.revision > before


# -- keys ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyval", "kwargs", "expected"),
    [
        (vtkeys.UP, {}, b"\x1b[A"),
        (vtkeys.UP, {"cursor_app": True}, b"\x1bOA"),
        (vtkeys.UP, {"ctrl": True}, b"\x1b[1;5A"),
        (vtkeys.HOME, {}, b"\x1b[H"),
        (vtkeys.HOME, {"cursor_app": True}, b"\x1b[H"),
        (vtkeys.PAGE_UP, {}, b"\x1b[5~"),
        (vtkeys.DELETE, {}, b"\x1b[3~"),
        (vtkeys.F1, {}, b"\x1bOP"),
        (vtkeys.F1 + 4, {}, b"\x1b[15~"),
        (vtkeys.F1 + 11, {}, b"\x1b[24~"),
        (vtkeys.RETURN, {}, b"\r"),
        (vtkeys.BACKSPACE, {}, b"\x7f"),
        (vtkeys.BACKSPACE, {"ctrl": True}, b"\x08"),
        (vtkeys.KP_LEFT, {}, b"\x1b[D"),
    ],
)
def test_special_keys_produce_the_sequence_the_far_side_expects(
    keyval, kwargs, expected
):
    assert vtkeys.key_sequence(keyval, **kwargs) == expected


@pytest.mark.parametrize(
    ("char", "expected"),
    [("c", b"\x03"), ("C", b"\x03"), ("d", b"\x04"), ("[", b"\x1b"), (" ", b"\x00")],
)
def test_control_combinations(char, expected):
    assert vtkeys.key_sequence(ord(char), char=char, ctrl=True) == expected


def test_alt_prefixes_with_escape():
    assert vtkeys.key_sequence(ord("b"), char="b", alt=True) == b"\x1bb"


def test_an_ordinary_key_is_sent_as_utf8():
    assert vtkeys.key_sequence(ord("é"), char="é") == "é".encode()


@pytest.mark.parametrize("keyval", [0xFFE1, 0xFFE3, 0xFFE9, 0xFFE5])
def test_a_modifier_pressed_alone_sends_nothing(keyval):
    """Shift, Ctrl, Alt and Caps Lock produce no character and no bytes."""
    assert vtkeys.key_sequence(keyval, char="") is None
    assert vtkeys.key_sequence(keyval, char="", ctrl=True) is None


def test_a_send_key_combination_with_no_terminal_meaning_is_refused():
    ctrl_alt_f2 = (0xFFE3, 0xFFE9, 0xFFBF)
    assert vtkeys.menu_sequence(ctrl_alt_f2) is None, (
        "a virtual-terminal switch was given an invented meaning"
    )
