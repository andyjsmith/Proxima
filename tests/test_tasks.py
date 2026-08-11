"""The cluster task feed, and following a task to its end."""

import pytest

from proxima.api import ProxmoxError
from proxima.api.client import ProxmoxAPI, task_upid
from proxima.ui import task_feed as tf_mod

from .conftest import key_for, pump, pump_until

COLUMNS = ["Start", "End", "Server", "Node", "User", "Description", "Status"]
RUNNING = key_for(100)
STOPPED = key_for(102)


# -- wait_for_task ---------------------------------------------------------
# Exercised against the real implementation, with only the two HTTP calls
# replaced: the point of it is the polling and the reading of exitstatus,
# neither of which needs a server.


class StubClient(ProxmoxAPI):
    def __init__(self, statuses, log=(), fail_log=False):
        self._statuses = list(statuses)
        self._log = list(log)
        self._fail_log = fail_log
        self.status_calls = 0

    def task_status(self, node, upid):
        self.status_calls += 1
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def task_log(self, node, upid, limit=200):
        if self._fail_log:
            raise ProxmoxError("no log for you")
        return self._log


def test_a_task_that_ends_ok_reports_success():
    client = StubClient([{"status": "stopped", "exitstatus": "OK"}])
    outcome = client.wait_for_task("n1", "UPID:x")
    assert outcome.ok, f"a clean task read as failed: {outcome}"
    assert outcome.message == "", "a successful task produced a message to show"


def test_a_task_is_polled_until_it_stops():
    client = StubClient(
        [
            {"status": "running"},
            {"status": "running"},
            {"status": "stopped", "exitstatus": "OK"},
        ]
    )
    assert client.wait_for_task("n1", "UPID:x", poll=0).ok
    assert client.status_calls == 3, f"stopped asking after {client.status_calls} polls"


def test_a_failed_task_reports_its_exitstatus_and_the_log_tail():
    client = StubClient(
        [{"status": "stopped", "exitstatus": "start failed: QEMU exited with code 1"}],
        log=["", "loading config", "kvm: -drive: Could not open '/dev/x'"],
    )
    outcome = client.wait_for_task("n1", "UPID:x")
    assert not outcome.ok
    assert "start failed" in outcome.message, outcome.message
    # The exitstatus alone rarely says enough to act on; the log is the why.
    assert "Could not open" in outcome.message, outcome.message
    assert "" not in outcome.message.split("\n\n")[1].split("\n"), (
        "blank log lines were kept"
    )


def test_a_failed_task_still_reports_when_its_log_cannot_be_read():
    client = StubClient(
        [{"status": "stopped", "exitstatus": "it went wrong"}], fail_log=True
    )
    outcome = client.wait_for_task("n1", "UPID:x")
    assert not outcome.ok
    assert outcome.message == "it went wrong", outcome.message


def test_a_task_that_never_finishes_gives_up_and_says_so():
    client = StubClient([{"status": "running"}])
    outcome = client.wait_for_task("n1", "UPID:x", timeout=0, poll=0)
    assert not outcome.ok
    assert "still running" in outcome.message, outcome.message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("UPID:pve:0001:...", "UPID:pve:0001:..."),
        (None, None),
        ("", None),
        ({"data": "x"}, None),
        ("not a upid", None),
    ],
)
def test_only_a_real_upid_is_followed(value, expected):
    """Some writes do the work inline and answer with nothing to follow."""
    assert task_upid(value) == expected


def test_the_feed_starts_hidden_and_does_not_poll(window, api):
    assert not window.task_feed.get_visible(), "task feed is visible by default"
    assert "tasks" not in api.calls, "task feed polled while collapsed"


def test_opening_the_feed_populates_it(window):
    window.task_feed.open()
    pump(0.8)
    rows = len(window.task_feed.store)
    assert rows == 3, f"task feed shows {rows} tasks, expected 3"

    statuses = [window.task_feed.store[i][tf_mod.COL_STATUS] for i in range(rows)]
    starts = [window.task_feed.store[i][tf_mod.COL_START] for i in range(rows)]
    assert all("-" in v for v in starts), f"task start times lack dates: {starts}"
    assert "running" in statuses and "OK" in statuses, (
        f"task feed statuses wrong: {statuses}"
    )
    assert "running" in window.task_feed.title_label.get_text(), (
        f"task feed title lacks the running count: "
        f"{window.task_feed.title_label.get_text()!r}"
    )


def test_the_description_column_expands(window):
    columns = [c.get_title() for c in window.task_feed.view.get_columns()]
    assert columns == COLUMNS
    assert window.task_feed.view.get_columns()[5].get_expand(), (
        "the Description column does not expand"
    )


def test_closing_the_feed_stops_the_polling(window):
    window.task_feed.open()
    pump(0.5)
    window.task_feed.close()
    pump(0.2)
    assert not window.task_feed.get_visible(), "task feed still visible after close"
    assert window.task_feed._source is None, "task feed kept polling after closing"


# -- reporting a failure to the user ---------------------------------------


@pytest.fixture
def failing_tasks(window, api):
    """Every followed task fails, with the dialog stubbed out.

    The dialog is modal and would block the test in dialog.run(), so it is
    replaced by something that records what it was asked to say.
    """
    shown = []
    real_dialog = window._error_dialog
    window._error_dialog = lambda title, message: shown.append((title, message))
    api.task_failure = "start failed: QEMU exited with code 1"
    yield shown
    api.task_failure = None
    window._error_dialog = real_dialog


def test_a_start_whose_task_fails_says_why(window, failing_tasks):
    window._run_action(STOPPED, "start", confirm=False)
    pump_until(lambda: bool(failing_tasks), 6, step=0.2)

    assert failing_tasks, "a failed start reported nothing at all"
    title, message = failing_tasks[-1]
    assert "failed" in title.lower(), f"dialog titled {title!r}"
    assert "QEMU exited" in message, f"the reason was not reported: {message!r}"
    assert "log line" in message, f"the task log was not included: {message!r}"
    assert "failed" in window.status_label_main.get_text(), (
        f"the status bar says {window.status_label_main.get_text()!r}"
    )


def test_a_failed_start_stops_the_spinner_and_the_console_panel(window, failing_tasks):
    """The whole point: no more spinning for 45 seconds and then silence."""
    window._run_action(STOPPED, "start", confirm=False)
    pump_until(lambda: bool(failing_tasks), 6, step=0.2)
    pump(0.3)
    assert STOPPED not in window._busy, "the row kept spinning after the task failed"
    assert STOPPED not in window._pending_actions, (
        "the console still claims the guest is starting"
    )


def test_a_task_that_succeeds_says_nothing(window, api):
    shown = []
    real_dialog = window._error_dialog
    window._error_dialog = lambda title, message: shown.append((title, message))
    try:
        window._run_action(STOPPED, "start", confirm=False)
        pump_until(lambda: any(c[:1] == ("wait_for_task",) for c in api.calls), 6)
        pump(0.4)
        assert not shown, f"a successful start put up a dialog: {shown}"
    finally:
        window._error_dialog = real_dialog
