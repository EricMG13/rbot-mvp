"""Turn every source file in a case folder into per-page UTF-8 text files."""

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from rbot.html_text import UndecodableHtml, convert_html
from rbot.xlsx import sheet_pages

SUPPORTED = (".pdf", ".htm", ".html", ".txt", ".md", ".xlsx", ".csv")
HTML_CHUNK = 3000
TEXT_CHUNK = 12000


class SourceError(Exception):
    """A source file cannot be read as text."""


@dataclass(frozen=True)
class SourceText:
    name: str
    pages: int


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _pdf_pages(raw: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise SourceError("encrypted PDF")
        return [page.extract_text(extraction_mode="layout") for page in reader.pages]
    except PyPdfError as error:
        raise SourceError(f"unreadable PDF: {error}") from error


def _html_pages(raw: bytes) -> list[str]:
    try:
        return _chunks(convert_html(raw).text, HTML_CHUNK)
    except UndecodableHtml as error:
        raise SourceError(f"undecodable HTML: {error}") from error


def _xlsx_pages(raw: bytes) -> list[str]:
    try:
        return sheet_pages(raw)
    except (ValueError, KeyError, zipfile.BadZipFile) as error:
        raise SourceError(f"unreadable workbook: {error!r}") from error


def _text_pages(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SourceError(f"not UTF-8: {error}") from error
    return _chunks(re.sub(r"\r\n?", "\n", text), TEXT_CHUNK)


def extract_pages(path: Path) -> list[str]:
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_pages(raw)
    if suffix in (".htm", ".html"):
        return _html_pages(raw)
    if suffix == ".xlsx":
        return _xlsx_pages(raw)
    return _text_pages(raw)


def write_source_text(sources: Path, out: Path) -> tuple[list[SourceText], list[str]]:
    done: list[SourceText] = []
    skipped: list[str] = []
    for path in sorted(p for p in sources.iterdir() if p.is_file()):
        if path.suffix.lower() not in SUPPORTED:
            skipped.append(path.name)
            continue
        pages = extract_pages(path)
        folder = out / path.name
        folder.mkdir(parents=True, exist_ok=True)
        for number, text in enumerate(pages, 1):
            with (folder / f"p{number:04d}.txt").open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        done.append(SourceText(name=path.name, pages=len(pages)))
    return done, skipped
