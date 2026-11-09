"""The static HTML report: one offline file, no JavaScript, fonts and logo inlined."""

import base64
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from rbot.charts import ChartMark, ChartSpec, render_svg
from rbot.handoff import section_units
from rbot.markdown import esc, inline, to_html

ASSETS = Path(__file__).parent / "assets"
DASH = "—"
# (file, family, weight) for the weights report.css uses.
FONTS = (
    ("RedHatDisplay-SemiBold", "Red Hat Display", 600),
    ("RedHatText-Regular", "Red Hat Text", 400),
    ("RedHatText-SemiBold", "Red Hat Text", 600),
    ("RedHatMono-Medium", "Red Hat Mono", 500),
)
LOGOS = (("logo.svg", "image/svg+xml"), ("logo.png", "image/png"))
_PLACEHOLDER = re.compile(r"/\*(STYLES|BODY|LICENSE)\*/")
_NO_GAPS = re.compile(r"(none|no gaps|n/a)\.?", re.IGNORECASE)
LOGO_MAX_BYTES = 512 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _esc(value: object) -> str:
    return esc(str(value))


def _lookup(context: Mapping[str, object], dotted: str) -> str:
    """A context value by dotted key, flat or nested; missing or empty renders as a dash."""
    value: object = context.get(dotted)
    if value is None:
        node: object = context
        for part in dotted.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        value = node
    return DASH if value is None or str(value).strip() == "" else str(value)


def _font_faces() -> str:
    rules = []
    for name, family, weight in FONTS:
        data = base64.b64encode((ASSETS / "fonts" / f"{name}.woff2").read_bytes()).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
            f"font-display:swap;src:url(data:font/woff2;base64,{data}) format('woff2')}}"
        )
    return "\n".join(rules)


def _styles() -> str:
    parts = [(ASSETS / "tokens.css").read_text(encoding="utf-8"), _font_faces()]
    parts.append((ASSETS / "report.css").read_text(encoding="utf-8"))
    return "\n".join(parts)


def _license() -> str:
    text = (ASSETS / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    return text.replace("--", DASH)  # "--" is illegal inside an HTML comment


def _logo_bytes(path: Path) -> bytes | None:
    """The logo file's bytes, or None for a symlink, an oversized file or a non-image."""
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        return None
    if path.suffix == ".png":
        return data if data.startswith(PNG_MAGIC) else None
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:5].lower()
    return data if head.startswith((b"<svg", b"<?xml")) else None


def _brand() -> str:
    for name, mime in LOGOS:
        data = _logo_bytes(Path.cwd() / "brand" / name)
        if data is not None:
            uri = base64.b64encode(data).decode("ascii")
            return f'<img class="logo" src="data:{mime};base64,{uri}" alt="">'
    return '<span class="wordmark">RBOT</span>'


def _cover(context: Mapping[str, object], modules: Sequence[str]) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    toc = "".join(f'<li><a href="#{_esc(m)}">{_esc(m)}</a></li>' for m in modules)
    facts = (
        ("As of", _lookup(context, "run.as_of")),
        ("Pathway", _lookup(context, "run.pathway")),
        ("Generated", generated),
    )
    dl = "".join(f"<dt>{k}</dt><dd>{_esc(v)}</dd>" for k, v in facts)
    contents = f'<ol class="toc">{toc}</ol>' if toc else ""
    return (
        f'<header class="cover">{_brand()}<h1>{_esc(_lookup(context, "identity.issuer_name"))}</h1>'
        f'<dl class="facts">{dl}</dl>{contents}</header>'
    )


_RATIO_ROWS = (
    ("gross_leverage", "Gross leverage"),
    ("net_leverage", "Net leverage"),
    ("interest_cover", "Interest cover"),
    ("liquidity", "Liquidity"),
)
_CHART_ROWS = _RATIO_ROWS[:2]
_EBITDA_NOTE = "Leverage on reported EBITDA where EBITDA As Defined is not reported."


def _gap_title(calc: Mapping[str, Any], period: str, name: str) -> str:
    """The calculator's gap text for a null ratio, else a plain statement that it is missing."""
    prefix = f"{period}: {name}"
    return next((g for g in calc["gaps"] if g.startswith(prefix)), f"{name} not available")


def _metric_cell(calc: Mapping[str, Any], period: str, name: str) -> str:
    raw = calc["ratios"][period][name]
    if raw is None:
        return f'<td class="num" title="{_esc(_gap_title(calc, period, name))}">{DASH}</td>'
    if name == "liquidity":
        text = f"{calc['currency']} {Decimal(raw):,f}{calc['unit']}"
    else:
        text = f"{Decimal(raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)}x"
    return f'<td class="num">{_esc(text)}</td>'


def _leverage_chart(calc: Mapping[str, Any]) -> str:
    periods = tuple(calc["periods"])
    marks = tuple(
        ChartMark(label, p, None if (v := calc["ratios"][p][name]) is None else Decimal(v))
        for name, label in _CHART_ROWS
        for p in periods
    )
    svg = render_svg(
        ChartSpec("line", "x", periods, marks), chart_id="key-metrics-leverage", title="Leverage"
    )
    legend = "".join(
        f'<span><span class="sw sw{i}"></span>{label}</span>'
        for i, (_, label) in enumerate(_CHART_ROWS, 1)
    )
    return f'<figure class="chart-figure">{svg}<div class="legend">{legend}</div></figure>'


def _metrics(work: Path) -> str:
    """Key metrics table and leverage chart from work/calculation.json; empty without it."""
    path = work / "calculation.json"
    if not path.is_file():
        return ""
    calc = json.loads(path.read_text(encoding="utf-8"))
    periods: list[str] = calc["periods"]
    head = "".join(f'<th class="num" scope="col">{_esc(p)}</th>' for p in periods)
    body = "".join(
        f'<tr><th scope="row">{label}</th>'
        + "".join(_metric_cell(calc, p, name) for p in periods)
        + "</tr>"
        for name, label in _RATIO_ROWS
    )
    table = (
        f'<table><thead><tr><th scope="col">Metric</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )
    ebitda = any(calc["ratios"][p]["basis"] == "ebitda" for p in periods)
    note = f'<p class="muted">{_EBITDA_NOTE}</p>' if ebitda else ""
    return (
        f'<section id="key-metrics"><h2>Key metrics</h2>{table}{note}'
        f"{_leverage_chart(calc)}</section>"
    )


def _gap_items(md: str) -> list[str]:
    """Gap items: the shared units of ``## Gaps`` minus the "none" placeholders."""
    return [u for u in section_units(md, "Gaps") if not _NO_GAPS.fullmatch(u)]


def _sources(work: Path) -> str:
    rows = []
    root = work / "source-text"
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for folder in folders:
        pages = sum(1 for _ in folder.glob("p*.txt"))
        rows.append(f"<tr><td>{_esc(folder.name)}</td><td>{pages}</td></tr>")
    table = (
        "<table><thead><tr><th>File</th><th>Pages</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else '<p class="muted">No source files recorded.</p>'
    )
    return f'<section id="sources"><h2>Sources</h2>{table}</section>'


def _gaps(found: list[tuple[str, str]]) -> str:
    if not found:
        body = '<p class="muted">No gaps reported.</p>'
    else:
        lis = "".join(f'<li><span class="tag">{_esc(m)}</span>{inline(t)}</li>' for m, t in found)
        body = f'<ul class="gaps">{lis}</ul>'
    return f'<section id="gaps"><h2>Gaps</h2>{body}</section>'


def render(work: Path, context: Mapping[str, object], modules: Sequence[str]) -> Path:
    """Write ``work/report.html`` from the accepted handoffs, in pathway order."""
    sections: list[str] = []
    gaps: list[tuple[str, str]] = []
    present: list[str] = []
    for module in modules:
        path = work / "handoffs" / f"{module}.md"
        if not path.is_file():
            continue
        md = path.read_text(encoding="utf-8")
        present.append(module)
        sections.append(
            f'<section id="{_esc(module)}" class="module"><h2>{_esc(module)}</h2>'
            f"{to_html(md, shift=2)}</section>"
        )
        gaps.extend((module, item) for item in _gap_items(md))
    body = (
        '<main class="doc">'
        + _cover(context, present)
        + _metrics(work)
        + "".join(sections)
        + _sources(work)
        + _gaps(gaps)
        + "</main>"
    )
    values = {"STYLES": _styles(), "BODY": body, "LICENSE": _license()}
    shell = (ASSETS / "shell.html").read_text(encoding="utf-8")
    out = _PLACEHOLDER.sub(lambda m: values[m.group(1)], shell)  # one pass: no rescans
    target = work / "report.html"
    tmp = work / "report.html.tmp"
    tmp.write_text(out, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target
