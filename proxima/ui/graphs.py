"""Line charts, drawn with cairo, for the figures a node reports over time.

Proxmox keeps a round-robin history per node -- CPU, memory, network -- and
its web UI draws it. There is no charting library in the dependency list and
there is not going to be one: GTK 3 has no chart widget, but it does have a
drawing area and cairo, which is all a line over a time axis needs. The same
reasoning as rfb.py and vt.py -- the thing itself, written out, rather than
something to install.

Nothing here knows what it is drawing. A Series is a name, a colour and a
list of (timestamp, value) points; the axis labels come from a formatter the
caller passes in. That keeps bytes, percentages and load averages on the same
widget without any of them being a special case.

Colours are fixed rather than taken from the theme: a series has to keep its
identity between the line and the legend, and be legible on both a light and
a dark background. Everything else -- text, grid, the frame -- is derived
from the widget's own foreground colour, so it follows the theme.
"""

import math
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, Pango, PangoCairo

# Chosen to stay apart on both backgrounds, and to keep their usual meanings:
# blue for the first thing, green for the second, orange for traffic out.
SERIES_COLOURS = [
    (0.21, 0.52, 0.89),  # blue
    (0.18, 0.76, 0.49),  # green
    (0.96, 0.47, 0.00),  # orange
    (0.57, 0.25, 0.67),  # purple
]

# Room for the y-axis labels, the title, and the time labels underneath.
PAD_LEFT = 58
PAD_RIGHT = 10
PAD_TOP = 22
PAD_BOTTOM = 20

MIN_HEIGHT = 128


class Series:
    """One line: what it is called, what colour it is, and its points.

    Points are (unix time, value) and may carry None for a sample the server
    had no data for. A gap is drawn as a gap -- joining across it would
    invent a straight line through a period nobody measured.
    """

    __slots__ = ("colour", "name", "points")

    def __init__(self, name, points, colour=None):
        self.name = name
        self.points = list(points or ())
        self.colour = colour or SERIES_COLOURS[0]

    @property
    def values(self):
        return [value for _at, value in self.points if value is not None]


def percent_label(value):
    return f"{value * 100:.0f}%"


def bytes_label(value):
    """A byte count, short enough to sit in an axis gutter."""
    value = float(value or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(value) < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}T"


def rate_label(value):
    """Bytes per second, which is what the network series carries."""
    return bytes_label(value) + "/s"


def plain_label(value):
    return f"{value:.2f}" if abs(value) < 10 else f"{value:.0f}"


def nice_maximum(value, binary=False):
    """Round a peak up to something worth writing on an axis.

    1, 2, 2.5 or 5 times a power of ten -- the same ladder every plotting
    library climbs, because those are the numbers whose quarters are also
    readable.

    Bytes climb a different ladder. A decimal-rounded ceiling of 100000000000
    is written "93.1G" by a formatter that divides by 1024, which is a worse
    label than the raw peak would have been; rounding in the same base the
    label is written in gives "96.0G" and quarters that land on whole units.
    """
    if value <= 0:
        return 1.0
    base = 1024 if binary else 10
    exponent = math.floor(math.log(value, base))
    unit = float(base**exponent)
    steps = (
        (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024) if binary else (1, 2, 2.5, 5, 10)
    )
    for step in steps:
        if value <= step * unit:
            return step * unit
    return base * unit


class TimeSeriesGraph(Gtk.DrawingArea):
    """One plot: a title, a legend, a time axis and any number of lines."""

    __gtype_name__ = "ProximaTimeSeriesGraph"

    def __init__(
        self,
        title="",
        formatter=plain_label,
        height=MIN_HEIGHT,
        maximum=None,
        fill=True,
        binary=False,
    ):
        super().__init__()
        self.title = title
        self.formatter = formatter or plain_label
        # Whether the axis is counted in 1024s, which is what the byte and
        # rate formatters write. See nice_maximum.
        self.binary = binary
        # A fixed ceiling, for a series whose scale is known in advance: a
        # percentage graph that rescales to its own peak makes 3% CPU look
        # identical to 90%.
        self.maximum = maximum
        self.fill = fill
        self.series = []
        self._hover = None  # x position of the pointer, while it is over us

        self.set_size_request(-1, max(MIN_HEIGHT, int(height)))
        self.set_hexpand(True)
        self.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self._on_draw)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)

    # -- data ------------------------------------------------------------

    def set_series(self, series):
        self.series = list(series or ())
        self.queue_draw()

    def has_data(self):
        return any(s.values for s in self.series)

    # -- pointer ----------------------------------------------------------

    def _on_motion(self, _widget, event):
        self._hover = event.x
        self.queue_draw()
        return False

    def _on_leave(self, *_args):
        self._hover = None
        self.queue_draw()
        return False

    # -- geometry ---------------------------------------------------------

    def _span(self):
        """The time range covered, as (first, last). Empty data gives None."""
        stamps = [
            at for s in self.series for at, value in s.points if value is not None
        ]
        if not stamps:
            return None
        first, last = min(stamps), max(stamps)
        return (first, last if last > first else first + 1)

    def _ceiling(self):
        if self.maximum is not None:
            return float(self.maximum)
        peak = max((max(s.values) for s in self.series if s.values), default=0.0)
        return nice_maximum(peak, binary=self.binary)

    # -- drawing ----------------------------------------------------------

    def _colours(self):
        """Text and grid, taken from the theme so both modes work."""
        colour = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        text = (colour.red, colour.green, colour.blue)
        return text, colour.alpha

    def _on_draw(self, _widget, context):
        allocation = self.get_allocation()
        width, height = allocation.width, allocation.height
        text, alpha = self._colours()

        layout = PangoCairo.create_layout(context)
        layout.set_font_description(Pango.FontDescription("Sans 8"))

        plot_x = PAD_LEFT
        plot_y = PAD_TOP
        plot_w = max(1, width - PAD_LEFT - PAD_RIGHT)
        plot_h = max(1, height - PAD_TOP - PAD_BOTTOM)

        if self.title:
            title_layout = PangoCairo.create_layout(context)
            title_layout.set_font_description(Pango.FontDescription("Sans Bold 8"))
            title_layout.set_text(self.title, -1)
            context.set_source_rgba(*text, alpha * 0.85)
            context.move_to(PAD_LEFT, 2)
            PangoCairo.show_layout(context, title_layout)

        self._draw_legend(context, layout, text, alpha, plot_x + plot_w)

        span = self._span()
        if span is None:
            context.set_source_rgba(*text, alpha * 0.5)
            layout.set_text("No data yet", -1)
            text_w, text_h = layout.get_pixel_size()
            context.move_to(
                plot_x + (plot_w - text_w) / 2, plot_y + (plot_h - text_h) / 2
            )
            PangoCairo.show_layout(context, layout)
            return False

        ceiling = self._ceiling()
        self._draw_grid(
            context, layout, text, alpha, plot_x, plot_y, plot_w, plot_h, ceiling
        )
        self._draw_time_axis(
            context, layout, text, alpha, plot_x, plot_y, plot_w, plot_h, span
        )

        for series in self.series:
            self._draw_series(
                context, series, plot_x, plot_y, plot_w, plot_h, span, ceiling
            )

        self._draw_hover(
            context, layout, text, alpha, plot_x, plot_y, plot_w, plot_h, span
        )
        return False

    def _draw_legend(self, context, layout, text, alpha, right):
        """Series names along the top, right aligned, swatch first."""
        named = [s for s in self.series if s.name]
        if len(named) < 2 and not (named and self.title):
            return
        x = right
        for series in reversed(named):
            layout.set_text(series.name, -1)
            label_w, _label_h = layout.get_pixel_size()
            x -= label_w
            context.set_source_rgba(*text, alpha * 0.75)
            context.move_to(x, 3)
            PangoCairo.show_layout(context, layout)
            x -= 6
            context.set_source_rgb(*series.colour)
            context.rectangle(x - 8, 8, 8, 3)
            context.fill()
            x -= 14

    def _draw_grid(self, context, layout, text, alpha, x, y, w, h, ceiling):
        context.set_line_width(1)
        for step in range(5):
            line_y = y + h - (h * step / 4)
            context.set_source_rgba(*text, alpha * 0.12)
            context.move_to(x, math.floor(line_y) + 0.5)
            context.line_to(x + w, math.floor(line_y) + 0.5)
            context.stroke()

            # Every other label, so a short graph does not stack its numbers.
            if step % 2 == 0 or h > 90:
                layout.set_text(self.formatter(ceiling * step / 4), -1)
                label_w, label_h = layout.get_pixel_size()
                context.set_source_rgba(*text, alpha * 0.6)
                context.move_to(x - 6 - label_w, line_y - label_h / 2)
                PangoCairo.show_layout(context, layout)

    def _draw_time_axis(self, context, layout, text, alpha, x, y, w, h, span):
        first, last = span
        # Dates once the range is long enough that the clock alone is
        # ambiguous, which is the point a "week" graph becomes unreadable.
        pattern = "%H:%M" if (last - first) <= 2 * 86400 else "%d %b"
        for step in range(3):
            at = first + (last - first) * step / 2
            label = time.strftime(pattern, time.localtime(at))
            layout.set_text(label, -1)
            label_w, _label_h = layout.get_pixel_size()
            position = x + w * step / 2
            if step == 0:
                offset = 0
            elif step == 2:
                offset = -label_w
            else:
                offset = -label_w / 2
            context.set_source_rgba(*text, alpha * 0.6)
            context.move_to(position + offset, y + h + 4)
            PangoCairo.show_layout(context, layout)

    def _points(self, series, x, y, w, h, span, ceiling):
        """Data points as runs of screen coordinates, split at every gap."""
        first, last = span
        runs = []
        current = []
        for at, value in series.points:
            if value is None:
                if current:
                    runs.append(current)
                    current = []
                continue
            px = x + w * (at - first) / (last - first)
            py = y + h - h * max(0.0, min(1.0, float(value) / ceiling))
            current.append((px, py))
        if current:
            runs.append(current)
        return runs

    def _draw_series(self, context, series, x, y, w, h, span, ceiling):
        runs = self._points(series, x, y, w, h, span, ceiling)
        if not runs:
            return

        if self.fill and len(self.series) == 1:
            for run in runs:
                if len(run) < 2:
                    continue
                context.move_to(run[0][0], y + h)
                for px, py in run:
                    context.line_to(px, py)
                context.line_to(run[-1][0], y + h)
                context.close_path()
                context.set_source_rgba(*series.colour, 0.18)
                context.fill()

        context.set_source_rgb(*series.colour)
        context.set_line_width(1.4)
        for run in runs:
            if len(run) == 1:
                # A single sample has no line to draw, so it is a dot rather
                # than nothing at all.
                px, py = run[0]
                context.arc(px, py, 1.4, 0, 2 * math.pi)
                context.fill()
                continue
            context.move_to(*run[0])
            for px, py in run[1:]:
                context.line_to(px, py)
            context.stroke()

    def _draw_hover(self, context, layout, text, alpha, x, y, w, h, span):
        """A rule under the pointer and what every series read there."""
        if self._hover is None or not (x <= self._hover <= x + w):
            return
        first, last = span
        at = first + (last - first) * (self._hover - x) / w

        readings = []
        for series in self.series:
            best = None
            for stamp, value in series.points:
                if value is None:
                    continue
                if best is None or abs(stamp - at) < abs(best[0] - at):
                    best = (stamp, value)
            if best is not None:
                readings.append((series, best))
        if not readings:
            return

        stamp = readings[0][1][0]
        px = x + w * (stamp - first) / (last - first)
        context.set_source_rgba(*text, alpha * 0.35)
        context.set_line_width(1)
        context.move_to(math.floor(px) + 0.5, y)
        context.line_to(math.floor(px) + 0.5, y + h)
        context.stroke()

        lines = [time.strftime("%d %b %H:%M", time.localtime(stamp))]
        for series, (_stamp, value) in readings:
            name = f"{series.name}: " if series.name else ""
            lines.append(f"{name}{self.formatter(value)}")
        layout.set_text("\n".join(lines), -1)
        box_w, box_h = layout.get_pixel_size()

        # Flipped to the other side of the rule when it would run off the
        # right-hand edge, which is where the most recent samples are and so
        # where the pointer usually is.
        box_x = px + 8
        if box_x + box_w + 8 > x + w:
            box_x = px - box_w - 14
        box_y = min(y + 4, y + h - box_h - 8)

        context.set_source_rgba(*text, 0.08 + alpha * 0.06)
        context.rectangle(box_x - 4, box_y - 2, box_w + 8, box_h + 4)
        context.fill()
        context.set_source_rgba(*text, alpha * 0.9)
        context.move_to(box_x, box_y)
        PangoCairo.show_layout(context, layout)
