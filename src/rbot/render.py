"""The static HTML report: one offline file, no JavaScript, fonts and logo inlined."""

import base64
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from rbot.handoff import FENCE, H2, split_lines
from rbot.markdown import esc, inline, item_text, to_html

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


def _gaps_lines(md: str) -> Iterator[str]:
    """Lines of the ``## Gaps`` section outside code fences, split as the checker splits."""
    in_gaps = in_fence = False
    for line in split_lines(md):
        if FENCE.match(line):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif heading := H2.match(line):
            in_gaps = heading.group(1) == "Gaps"
        elif in_gaps:
            yield line


def _gap_items(md: str) -> list[str]:
    """Gap items: list items (with continuation lines, as markdown._list) and paragraph runs."""
    items: list[list[str]] = []
    open_item = False  # the last item is a list item, which indented lines continue
    for line in _gaps_lines(md):
        text = item_text(line)
        if not line.strip() or line.lstrip().startswith("#"):
            open_item = False
            items.append([])
        elif text is not None:
            items.append([text.strip()])
            open_item = True
        elif items and items[-1] and (not open_item or line.startswith((" ", "\t"))):
            items[-1].append(line.strip())
        else:
            items.append([line.strip()])
            open_item = False
    joined = [" ".join(parts) for parts in items if parts]
    if len(joined) == 1 and _NO_GAPS.fullmatch(joined[0]):
        return []
    return joined


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
    tmp = work / "report.html.tmp"
    tmp.write_text(out, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target
