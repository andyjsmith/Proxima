"""The cluster task feed."""

from proxima.ui import task_feed as tf_mod

from .conftest import pump

COLUMNS = ["Start", "End", "Server", "Node", "User", "Description", "Status"]


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
