"""Static SVG line chart for the report: a ``ChartSpec`` goes in, an SVG string comes out.

Edited from final-workflow `src/deploy_vii/charts.py` at fda70c29: the geometry helpers (`_n`,
`_nice`, `_bounds`, `_ticks`, `_frame`, `_axis_y`, `_na_slot`, `_line`, `_na`, `render_svg`) are
kept, the SVG is built as escaped strings (no ``xml.etree``), and provenance hooks, table twins
and every kind but the line chart are dropped. Colour is never written here: elements carry
token classes (``k1``..``k5``, ``f1``..``f5``, ``ax``) that ``report.css`` maps to ``--rb-*``
variables. A ``None`` value draws an ``n/a`` slot, never a zero.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from html import escape

_NS = "http://www.w3.org/2000/svg"
_CHART_ID = re.compile(r"[A-Za-z0-9_-]+")
_SERIES_CLASSES = 5
_EDGE = 14.0
_WIDTH, _HEIGHT = 320, 160
_LEFT, _RIGHT, _TOP, _BOTTOM = 40.0, 48.0, 14.0, 26.0


@dataclass(frozen=True)
class ChartMark:
    """One data point: ``value`` is exact; ``None`` means not available."""

    series: str
    category: str
    value: Decimal | None


@dataclass(frozen=True)
class ChartSpec:
    """A line chart: ``categories`` (periods, in order) by the series named in ``marks``."""

    kind: str
    unit: str
    categories: tuple[str, ...]
    marks: tuple[ChartMark, ...]

    @property
    def series(self) -> list[str]:
        seen: list[str] = []
        for mark in self.marks:
            if mark.series not in seen:
                seen.append(mark.series)
        return seen


def _n(value: float) -> str:
    return f"{value:.2f}"


def _el(tag: str, text: str | None = None, **attrs: str) -> str:
    """One element; every attribute value and the text are escaped."""
    parts = "".join(
        f' {name.rstrip("_").replace("_", "-")}="{escape(value, quote=True)}"'
        for name, value in attrs.items()
    )
    if text is None:
        return f"<{tag}{parts}/>"
    return f"<{tag}{parts}>{escape(text, quote=True)}</{tag}>"


def _series_class(spec: ChartSpec, series: str) -> int:
    return spec.series.index(series) % _SERIES_CLASSES + 1


def _nice(maximum: float) -> float:
    if maximum <= 0:
        return 1.0
    power = 10.0 ** math.floor(math.log10(maximum))
    ratio = maximum / power
    step = (
        1 if ratio <= 1 else 2 if ratio <= 2 else 2.5 if ratio <= 2.5 else 5 if ratio <= 5 else 10
    )
    return step * power


def _bounds(values: list[float], headroom: float = 1.12) -> tuple[float, float]:
    high = _nice(max([*values, 0.0]) * headroom)
    low = -_nice(abs(min([*values, 0.0])) * headroom) if min(values) < 0 else 0.0
    return low, high


def _ticks(low: float, high: float, count: int) -> list[float]:
    step = _nice((high - low) / count)
    first = math.ceil(low / step - 1e-9)
    ticks = []
    value = first * step
    while value <= high + 1e-9:
        ticks.append(round(value, 6))
        value += step
    return ticks


def _tick_text(spec: ChartSpec, value: float, ticks: list[float]) -> str:
    """A tick label at one precision for the whole axis: the fewest places that tell ticks apart."""
    places = next(p for p in range(6) if all(abs(t - round(t, p)) < 1e-9 for t in ticks))
    return f"{value:.{places}f}{spec.unit}"


def _mark_text(spec: ChartSpec, value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)}{spec.unit}"


def _frame(body: str, chart_id: str, title: str) -> str:
    return (
        f'<svg xmlns="{_NS}" viewBox="0 0 {_WIDTH} {_HEIGHT}" width="{_WIDTH}" height="{_HEIGHT}" '
        f'class="chart" role="img" aria-labelledby="{chart_id}-title">'
        f"{_el('title', title, id=f'{chart_id}-title')}{body}</svg>"
    )


def _axis_y(
    spec: ChartSpec, ticks: list[float], y_of: Callable[[float], float], left: float, right: float
) -> str:
    out = []
    for tick in ticks:
        y = _n(y_of(tick))
        out.append(
            _el(
                "line",
                x1=_n(left),
                x2=_n(right),
                y1=y,
                y2=y,
                class_="grid zero" if tick == 0 else "grid",
            )
        )
        out.append(
            _el(
                "text",
                _tick_text(spec, tick, ticks),
                x=_n(left - 6),
                y=_n(y_of(tick) + 4),
                text_anchor="end",
                class_="ax",
            )
        )
    return "".join(out)


def _na_slot(x: float, y: float) -> str:
    return _el("text", "n/a", x=_n(x), y=_n(y), text_anchor="middle", class_="ax na")


def _marker(index: int, x: float, y: float, cls: int, label: str) -> str:
    css = f"pt k{cls} f{cls}"
    if index % 2 == 1:
        shape = _el("rect", x=_n(x - 4), y=_n(y - 4), width="8", height="8", class_=css)
    else:
        shape = _el("circle", cx=_n(x), cy=_n(y), r="4", class_=css)
    return f"<g>{_el('title', label)}{shape}</g>"


def _line(spec: ChartSpec) -> str:
    categories = spec.categories
    points = {(m.series, m.category): m for m in spec.marks}
    values = [float(m.value) for m in spec.marks if m.value is not None]
    low, high = _bounds(values, 1.15)
    ticks = _ticks(low, high, 3)
    span = _WIDTH - _LEFT - _RIGHT

    def x_of(i: int) -> float:
        inner = span - 2 * _EDGE  # keep end points clear of the axis labels
        step = inner / 2 if len(categories) == 1 else i * inner / (len(categories) - 1)
        return _LEFT + _EDGE + step

    def y_of(v: float) -> float:
        return _TOP + (_HEIGHT - _TOP - _BOTTOM) * (1 - (v - low) / (high - low))

    out = [_axis_y(spec, ticks, y_of, _LEFT, _WIDTH - _RIGHT)]
    out += [
        _el("text", c, x=_n(x_of(i)), y=str(_HEIGHT - 8), text_anchor="middle", class_="ax")
        for i, c in enumerate(categories)
    ]
    for series in spec.series:
        cls = _series_class(spec, series)
        path, pen, last = "", False, None
        for i, category in enumerate(categories):
            mark = points.get((series, category))
            if mark is None or mark.value is None:
                pen = False
                continue
            y = y_of(float(mark.value))
            path += f"{'L' if pen else 'M'}{_n(x_of(i))} {_n(y)}"
            pen, last = True, (i, mark.value, y)
        if path:
            out.append(_el("path", d=path, class_=f"ln k{cls}"))
        if last is not None:
            i, value, y = last
            out.append(
                _el(
                    "text",
                    _mark_text(spec, value),
                    x=_n(x_of(i) + 9),
                    y=_n(y + 4),
                    class_=f"lb f{cls}",
                )
            )
    for si, series in enumerate(spec.series):
        cls = _series_class(spec, series)
        for i, category in enumerate(categories):
            mark = points.get((series, category))
            if mark is None:
                continue
            if mark.value is None:
                out.append(_na_slot(x_of(i), _TOP + 2))
                continue
            label = f"{series}, {category}: {_mark_text(spec, mark.value)}"
            out.append(_marker(si, x_of(i), y_of(float(mark.value)), cls, label))
    return "".join(out)


def _na() -> str:
    """The chart with no value to draw: a dashed box and one ``n/a``."""
    box = _el(
        "rect", x="8", y="8", width=str(_WIDTH - 16), height=str(_HEIGHT - 16), class_="na-box"
    )
    return box + _el(
        "text", "n/a", x=_n(_WIDTH / 2), y=_n(_HEIGHT / 2), text_anchor="middle", class_="ax na"
    )


def render_svg(spec: ChartSpec, *, chart_id: str, title: str) -> str:
    """The chart as an SVG string; ``chart_id`` is restricted to ``[A-Za-z0-9_-]+``."""
    if not _CHART_ID.fullmatch(chart_id):
        raise ValueError(f"chart_id must match [A-Za-z0-9_-]+: {chart_id!r}")
    if spec.kind != "line":
        raise ValueError(f"unsupported chart kind: {spec.kind!r}")
    drawable = any(m.value is not None for m in spec.marks)
    return _frame(_line(spec) if drawable else _na(), chart_id, title)
