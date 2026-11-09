import re
from decimal import Decimal

import pytest

from rbot.charts import ChartMark, ChartSpec, render_svg


def spec(gross: list[str | None], net: list[str | None]) -> ChartSpec:
    periods = [f"fy-{2023 + i}" for i in range(len(gross))]
    marks = [
        ChartMark(series, p, None if v is None else Decimal(v))
        for series, values in (("Gross", gross), ("Net", net))
        for p, v in zip(periods, values, strict=True)
    ]
    return ChartSpec(kind="line", unit="x", categories=tuple(periods), marks=tuple(marks))


def svg_of(s: ChartSpec) -> str:
    return render_svg(s, chart_id="lev", title="Leverage")


def test_line_chart_points() -> None:
    out = svg_of(spec(["5.8700", "6.0100"], ["4.8400", "4.5100"]))
    assert len(re.findall(r'class="pt k1[ "]', out)) == 2
    assert len(re.findall(r'class="pt k2[ "]', out)) == 2
    assert len(re.findall(r'class="ln k[12]"', out)) == 2
    assert "6.01x" in out and "4.51x" in out


def test_null_is_na_slot() -> None:
    out = svg_of(spec(["5.8700", None], ["4.8400", "4.5100"]))
    assert out.count('class="ax na"') == 1
    assert len(re.findall(r'class="pt k1[ "]', out)) == 1
    assert len(re.findall(r'class="pt k2[ "]', out)) == 2
    assert re.search(r'<path d="M[^"]*" class="ln k1"', out)


def test_all_null_is_na_chart() -> None:
    out = svg_of(spec([None, None], [None, None]))
    assert 'class="na-box"' in out and ">n/a<" in out and 'class="pt' not in out


def test_svg_safe() -> None:
    out = svg_of(spec(["5.8700", "6.0100"], ["4.8400", None]))
    assert out.startswith("<svg") and 'role="img"' in out and "<title" in out
    low = out.lower()
    for bad in ("<script", "href", "xlink", "foreignobject", "url(", "javascript:"):
        assert bad not in low
    assert not re.search(r"\son[a-z]+\s*=", low)


def test_hostile_text_is_escaped() -> None:
    s = spec(["1.0000"], ["2.0000"])
    hostile = ChartSpec(
        kind="line",
        unit="x",
        categories=('<script>"onload=x"',),
        marks=tuple(ChartMark(m.series, '<script>"onload=x"', m.value) for m in s.marks),
    )
    out = render_svg(hostile, chart_id="lev", title='T"<b>')
    assert "<script" not in out and "<b>" not in out and '"onload' not in out
    assert "&lt;script&gt;" in out and "&quot;" in out


@pytest.mark.parametrize("char", ["\u202e", "\u2066", "\ud800"])
def test_chart_text_uses_the_report_sanitiser(char: str) -> None:
    label = f"fy{char}x"
    marks = (ChartMark("G", label, Decimal("1")), ChartMark("G", "fy-b", Decimal("2")))
    out = render_svg(ChartSpec("line", "x", (label, "fy-b"), marks), chart_id="a", title=f"T{char}")
    assert char not in out and "fy\ufffdx" in out
    out.encode("utf-8")  # a lone surrogate would raise here


@pytest.mark.parametrize("bad", ["", "a b", 'a"b', "a<b", "a/b"])
def test_chart_id_validated(bad: str) -> None:
    with pytest.raises(ValueError, match="chart_id"):
        render_svg(spec(["1.0000"], ["2.0000"]), chart_id=bad, title="t")


def test_unsupported_kind_refused() -> None:
    s = spec(["1.0000"], ["2.0000"])
    with pytest.raises(ValueError, match="kind"):
        render_svg(ChartSpec("waterfall", "x", s.categories, s.marks), chart_id="a", title="t")


def test_single_period_and_negative_values() -> None:
    out = svg_of(spec(["-1.5000"], ["2.0000"]))
    assert len(re.findall(r'class="pt k[12]', out)) == 2
    assert 'class="grid zero"' in out
