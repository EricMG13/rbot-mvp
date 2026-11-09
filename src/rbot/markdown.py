"""A small Markdown subset to HTML. Model-written text goes through here, so escaping is the point.

Supported: ``#``-``####`` headings, paragraphs, ``-`` and ``1.`` lists, pipe tables, ``**bold**``,
``*italic*``, ``code`` spans and fenced code blocks. Links render as their text only. Raw HTML is
never passed through: every text run is escaped with ``html.escape(quote=True)``.
"""

import html
import re

_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\*(?P<em>[^*\s](?:[^*]*[^*\s])?)\*"
    r"|\[(?P<link>[^\]]+)\]\([^)]*\)"
)
_HEADING = re.compile(r"(#{1,4})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"[-*]\s+(.*)")
_NUMBER = re.compile(r"\d+[.)]\s+(.*)")
_SEPARATOR = re.compile(r"\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def inline(text: str) -> str:
    """Inline markup for one run of text."""
    out: list[str] = []
    last = 0
    for m in _INLINE.finditer(text):
        out.append(_esc(text[last : m.start()]))
        if m.group("code") is not None:
            out.append(f"<code>{_esc(m.group('code'))}</code>")
        elif m.group("bold") is not None:
            out.append(f"<strong>{inline(m.group('bold'))}</strong>")
        elif m.group("em") is not None:
            out.append(f"<em>{inline(m.group('em'))}</em>")
        else:
            out.append(inline(m.group("link")))
        last = m.end()
    out.append(_esc(text[last:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    row = line.strip()
    row = row.removeprefix("|").removesuffix("|")
    return [c.strip() for c in row.split("|")]


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        lines[i].lstrip().startswith("|")
        and i + 1 < len(lines)
        and "-" in lines[i + 1]
        and _SEPARATOR.fullmatch(lines[i + 1].strip()) is not None
    )


def _table(lines: list[str], i: int) -> tuple[str, int]:
    head = _cells(lines[i])
    width = len(head)
    i += 2
    body: list[str] = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        row = (_cells(lines[i]) + [""] * width)[:width]
        body.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
        i += 1
    thead = "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>"
    return f"<table>{thead}<tbody>{''.join(body)}</tbody></table>", i


def _code(lines: list[str], i: int) -> tuple[str, int]:
    i += 1
    code: list[str] = []
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code.append(lines[i])
        i += 1
    return f"<pre><code>{_esc(chr(10).join(code))}</code></pre>", i + 1


def _item_match(line: str, ordered: bool) -> re.Match[str] | None:
    return (_NUMBER if ordered else _BULLET).match(line.strip())


def _list(lines: list[str], i: int, ordered: bool) -> tuple[str, int]:
    items: list[str] = []
    while i < len(lines):
        m = _item_match(lines[i], ordered)
        if m is None:
            break
        parts = [m.group(1)]
        i += 1
        while i < len(lines) and lines[i].startswith((" ", "\t")) and lines[i].strip():
            if _item_match(lines[i], ordered):
                break
            parts.append(lines[i].strip())
            i += 1
        items.append(f"<li>{inline(' '.join(parts))}</li>")
    tag = "ol" if ordered else "ul"
    return f"<{tag}>{''.join(items)}</{tag}>", i


def _starts_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    return bool(
        line.strip().startswith("```")
        or _HEADING.match(line)
        or _BULLET.match(line.strip())
        or _NUMBER.match(line.strip())
        or _is_table_start(lines, i)
    )


def to_html(md: str, shift: int = 0) -> str:
    """Convert the Markdown subset to HTML. ``shift`` pushes heading levels down (capped at h6)."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
        elif stripped.startswith("```"):
            block, i = _code(lines, i)
            out.append(block)
        elif (h := _HEADING.match(stripped)) is not None:
            level = min(len(h.group(1)) + shift, 6)
            out.append(f"<h{level}>{inline(h.group(2))}</h{level}>")
            i += 1
        elif _is_table_start(lines, i):
            block, i = _table(lines, i)
            out.append(block)
        elif _BULLET.match(stripped) or _NUMBER.match(stripped):
            block, i = _list(lines, i, ordered=_NUMBER.match(stripped) is not None)
            out.append(block)
        else:
            para = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines, i):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "".join(out)
