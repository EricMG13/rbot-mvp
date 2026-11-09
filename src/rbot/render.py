"""The static HTML report: one offline file, no JavaScript, fonts and logo inlined."""

import base64
import html
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from rbot.markdown import inline, to_html

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
_GAPS_HEADING = re.compile(r"##\s+Gaps\s*")
_ITEM = re.compile(r"\s*(?:[-*]|\d+[.)])\s+(.*)")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


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


def _brand() -> str:
    for name, mime in LOGOS:
        path = Path.cwd() / "brand" / name
        if path.is_file():
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return f'<img class="logo" src="data:{mime};base64,{data}" alt="">'
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


def _gap_items(md: str) -> list[str]:
    items: list[str] = []
    in_gaps = in_fence = False
    for line in md.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif line.startswith("## "):
            in_gaps = _GAPS_HEADING.fullmatch(line.rstrip()) is not None
        elif in_gaps and (m := _ITEM.fullmatch(line)):
            items.append(m.group(1).strip())
    return items


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
        + "".join(sections)
        + _sources(work)
        + _gaps(gaps)
        + "</main>"
    )
    values = {"STYLES": _styles(), "BODY": body, "LICENSE": _license()}
    shell = (ASSETS / "shell.html").read_text(encoding="utf-8")
    out = _PLACEHOLDER.sub(lambda m: values[m.group(1)], shell)  # one pass: no rescans
    target = work / "report.html"
    target.write_text(out, encoding="utf-8", newline="\n")
    return target
