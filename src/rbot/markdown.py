"""A small Markdown subset to HTML. Model-written text goes through here, so escaping is the point.

Supported: ``#``-``####`` headings, paragraphs, ``-`` and ``1.`` lists, pipe tables, ``**bold**``,
``*italic*``, ``code`` spans and fenced code blocks. Links render as their text only. Raw HTML is
never passed through: every text run is escaped with ``html.escape(quote=True)``.
"""

import html
import re

from rbot.handoff import FENCE, split_lines

_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\*(?P<em>[^*\s](?:[^*]*[^*\s])?)\*"
    r"|\[(?P<link>[^\][]{1,500})\]\([^)]{0,2000}\)"
)
_HEADING = re.compile(r"(#{1,4})\s+(.*)")
_BULLET = re.compile(r"[-*]\s+(.*)")
_NUMBER = re.compile(r"\d+[.)]\s+(.*)")
_RULE_CELL = re.compile(r":?-+:?")
_UNSAFE = re.compile("[\ud800-\udfff\u202a-\u202e\u2066-\u2069]")


def esc(text: str) -> str:
    """Escape a text run; lone surrogates and bidi override/isolate characters become U+FFFD."""
    return html.escape(_UNSAFE.sub("\ufffd", text), quote=True)


def item_text(line: str) -> str | None:
    """The text of a ``-`` or ``1.`` list item line, or None when the line is not an item."""
    m = _BULLET.match(line.strip()) or _NUMBER.match(line.strip())
    return m.group(1) if m else None


def _heading_text(text: str) -> str:
    body = text.rstrip()
    core = body.rstrip("#")
    if core != body and (not core or core[-1].isspace()):
        body = core.rstrip()
    return body


def inline(text: str) -> str:
    """Inline markup for one run of text."""
    out: list[str] = []
    last = 0
    for m in _INLINE.finditer(text):
        out.append(esc(text[last : m.start()]))
        if m.group("code") is not None:
            out.append(f"<code>{esc(m.group('code'))}</code>")
        elif m.group("bold") is not None:
            out.append(f"<strong>{inline(m.group('bold'))}</strong>")
        elif m.group("em") is not None:
            out.append(f"<em>{inline(m.group('em'))}</em>")
        else:
            out.append(inline(m.group("link")))
        last = m.end()
    out.append(esc(text[last:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    row = line.strip()
    row = row.removeprefix("|").removesuffix("|")
    return [c.strip() for c in row.split("|")]


def _aligns(line: str) -> list[bool] | None:
    """Per column, True when right-aligned (``--:``); None when the line is no separator row."""
    row = line.strip().removeprefix("|").removesuffix("|")
    cells = [c.strip() for c in row.split("|")]
    if not all(_RULE_CELL.fullmatch(c) for c in cells):
        return None
    return [c.endswith(":") and not c.startswith(":") for c in cells]


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        lines[i].lstrip().startswith("|")
        and i + 1 < len(lines)
        and _aligns(lines[i + 1]) is not None
    )


def _cell(tag: str, text: str, right: bool) -> str:
    attr = ' class="num"' if right else ""
    return f"<{tag}{attr}>{inline(text)}</{tag}>"


def _table(lines: list[str], i: int) -> tuple[str, int]:
    head = _cells(lines[i])
    width = len(head)
    right = ((_aligns(lines[i + 1]) or []) + [False] * width)[:width]
    i += 2
    body: list[str] = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        row = (_cells(lines[i]) + [""] * width)[:width]
        body.append(
            "<tr>" + "".join(_cell("td", c, r) for c, r in zip(row, right, strict=True)) + "</tr>"
        )
        i += 1
    cells = "".join(_cell("th", c, r) for c, r in zip(head, right, strict=True))
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{''.join(body)}</tbody></table>", i


def _code(lines: list[str], i: int) -> tuple[str, int]:
    i += 1
    code: list[str] = []
    while i < len(lines) and not FENCE.match(lines[i]):
        code.append(lines[i])
        i += 1
    return f"<pre><code>{esc(chr(10).join(code))}</code></pre>", i + 1


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
        FENCE.match(line)
        or _HEADING.match(line.strip())
        or _BULLET.match(line.strip())
        or _NUMBER.match(line.strip())
        or _is_table_start(lines, i)
    )


def to_html(md: str, shift: int = 0) -> str:
    """Convert the Markdown subset to HTML. ``shift`` pushes heading levels down (capped at h6)."""
    lines = split_lines(md)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
        elif FENCE.match(line):
            block, i = _code(lines, i)
            out.append(block)
        elif (h := _HEADING.match(stripped)) is not None:
            level = min(len(h.group(1)) + shift, 6)
            out.append(f"<h{level}>{inline(_heading_text(h.group(2)))}</h{level}>")
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
