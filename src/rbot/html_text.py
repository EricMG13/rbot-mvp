"""Stdlib HTML to line-text converter (O2 Task 1, R1).

``preparation.prepare`` accepts only text, PDF and XLSX, and ``text_content`` makes one citable
span per ``str.splitlines()`` line with locator ``line:N``. A citation must sit inside one span, so
the line rule below decides which quotes a model can cite. ``line:N`` locators number the
converted text, not the HTML source, and empty lines are dropped so no empty span is citable.

Visible-text semantics follow ``cases/goldens/check.py`` (``_Visible``): ``SKIP`` elements and
``display:none`` elements are dropped, ``VOID`` tags are never pushed, ``<body>`` ends an unclosed
head and entities are unescaped. The constants are copied here because ``src`` must not import from
``cases/``; a test compares them with ``check.py``'s.

Pure: reads no file, no environment, no network. An unexpected exception propagates.
"""

from __future__ import annotations

import codecs
import html.parser
import re
from collections import Counter
from dataclasses import dataclass

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
SKIP = {"script", "style", "head", "ix:header"}
BLOCK = {
    "address",
    "blockquote",
    "body",
    "br",
    "caption",
    "center",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
HIDDEN = re.compile(r"display\s*:\s*none", re.IGNORECASE)
ZERO_WIDTH_CHARS = "\u200b‌‍⁠﻿"
_ZERO_WIDTH = dict.fromkeys(map(ord, ZERO_WIDTH_CHARS))

# The line rule (Q1, ruled by the controller: "no div"): tags that start and end a line. Every
# other BLOCK tag, including div, emits a space. Chosen over tr+p for 83 vs 81 misses but a longest
# line of 108,282 vs 961,460 characters and finer locators (21,911 vs 17,056 lines); see
# work/context-plan/reviews/o2-line-rule-measurement.md. A different ruling is a one-line change here.
LINE_TAGS = frozenset({"p", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6"})

_SNIFF_BYTES = 4096
_CHARSET = re.compile(rb"""(?:charset|encoding)\s*=\s*["']?\s*([A-Za-z0-9_.:\-]+)""", re.IGNORECASE)
# codecs.lookup(...).name -> the name reported in ConvertedHtml.encoding
_ACCEPTED = {
    "utf-8": "utf-8",
    "cp1252": "windows-1252",
    "iso8859-1": "iso-8859-1",
    "ascii": "ascii",
}


class UndecodableHtml(ValueError):
    """The bytes cannot be decoded strictly with an accepted encoding, or contain NUL."""


@dataclass(frozen=True)
class ConvertedHtml:
    text: str
    encoding: str
    line_count: int


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_ZERO_WIDTH)).strip()


class _Lines(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.open_names: Counter[str] = Counter()  # open tag name -> count, keeps end tags linear
        self.skipping = 0

    def _break(self, tag: str) -> None:
        if tag in LINE_TAGS:
            self.parts.append("\n")
        elif tag in BLOCK:
            self.parts.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.handle_endtag("head")  # an unclosed head ends at body
        self._break(tag)
        if tag in VOID:
            return
        hidden = tag in SKIP or any(k == "style" and HIDDEN.search(v or "") for k, v in attrs)
        self.stack.append((tag, hidden))
        self.open_names[tag] += 1
        self.skipping += hidden

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._break(tag)

    def handle_endtag(self, tag: str) -> None:
        self._break(tag)
        if tag in VOID:
            return
        if not self.open_names[tag]:
            return
        while True:
            name, hidden = self.stack.pop()
            self.open_names[name] -= 1
            self.skipping -= hidden
            if name == tag:
                return

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            # Newlines inside text are plain whitespace, never a line break.
            self.parts.append(re.sub(r"\s+", " ", data))


def decode_html(raw: bytes) -> tuple[str, str]:
    """The strictly decoded text and the encoding name; raises ``UndecodableHtml``."""
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError as exc:
            raise UndecodableHtml(f"invalid UTF-8 after BOM: {exc}") from exc
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    match = _CHARSET.search(raw[:_SNIFF_BYTES])
    if match is None:
        raise UndecodableHtml("not valid UTF-8 and declares no charset in the first 4096 bytes")
    declared = match.group(1).decode("ascii")
    try:
        codec = codecs.lookup(declared).name
    except LookupError as exc:
        raise UndecodableHtml(f"declared charset {declared!r} is not a known codec") from exc
    encoding = _ACCEPTED.get(codec)
    if encoding is None:
        raise UndecodableHtml(f"declared charset {declared!r} is not accepted")
    try:
        return raw.decode(codec), encoding
    except UnicodeDecodeError as exc:
        raise UndecodableHtml(f"bytes are not valid {encoding}: {exc}") from exc


def convert_html(raw: bytes) -> ConvertedHtml:
    """Convert HTML bytes to newline-terminated visible-text lines (empty lines dropped)."""
    decoded, encoding = decode_html(raw)
    if "\x00" in decoded:
        raise UndecodableHtml("decoded text contains NUL")
    parser = _Lines()
    parser.feed(decoded)
    parser.close()
    lines = [line for line in (_normalise(x) for x in "".join(parser.parts).split("\n")) if line]
    text = "".join(f"{line}\n" for line in lines)
    return ConvertedHtml(text=text, encoding=encoding, line_count=len(lines))
