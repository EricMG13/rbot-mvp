"""OOXML workbook reader: stored cell lexemes only, no formula calculation, macros or links.

Provenance: rewritten from Final-workflow src/deploy_vii/preparation.py at fda70c29
(xlsx_content, xml_part, _cell_address, _sheet_status, NS, REL, MAX_EXPANDED_BYTES). Listed as
new code, not as an edited copy: the source is a 607-line module this file only partly reuses.
The pydantic Span and Coverage are frozen dataclasses with the same fields.
"""

import importlib
import io
import posixpath
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote
from zipfile import ZipFile

# The stdlib parser is loaded by name: one suppression instead of an import plus a call rule per
# use (the suppression count may only fall). The parse is made safe by xml_part,
# which refuses any DOCTYPE/ENTITY declaration and NUL byte before parsing; no external entity or
# expansion can occur without a declaration.
ET: Any = importlib.import_module("xml.etree.ElementTree")  # nosec B405
Element = Any

MAX_EXPANDED_BYTES = 200 * 1024 * 1024
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
TRUE = {"1", "true"}


@dataclass(frozen=True)
class Span:
    locator: str
    text: str
    formula: str | None = None
    cached_value: str | None = None
    hidden: bool = False
    data_type: str | None = None
    number_format: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Coverage:
    locator: str
    status: Literal["retained", "unreadable", "empty"]
    limitation: str | None = None


@dataclass(frozen=True)
class Extraction:
    spans: tuple[Span, ...]
    coverage: tuple[Coverage, ...]
    limitations: tuple[str, ...] = ()


def xml_part(archive: ZipFile, path: str) -> Element:
    # Reject alternate encodings before the declaration check: UTF-16 NUL bytes
    # must not hide an entity declaration from this boundary.
    raw = archive.read(path).decode("utf-8-sig", errors="strict")
    if "\x00" in raw or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not supported")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f"malformed XML in {path}: {error}") from error


def _cell_address(stated: str | None, row_number: int, previous: int) -> tuple[str, int]:
    """A cell's address and 1-based column index: the stated `r`, or, when the cell has none (or
    an empty one), the column after the row's previous cell (`previous`) in row `row_number`
    (ECMA-376 18.3.1.4)."""
    if not stated:
        column, letters = previous + 1, ""
        while column:
            column, rest = divmod(column - 1, 26)
            letters = chr(65 + rest) + letters
        return f"{letters}{row_number}", previous + 1
    index = 0
    for char in stated:
        if char.isalpha():
            index = index * 26 + ord(char.upper()) - 64
    return stated, index


def _sheet_status(spans: list[Span]) -> Literal["retained", "empty"]:
    """Retained when a cell holds non-blank text or a formula; metadata is no content."""
    content = any(
        span.data_type != "metadata" and (span.text.strip() or span.formula) for span in spans
    )
    return "retained" if content else "empty"


def _texts(node: Element, *paths: str) -> str:
    return "".join(t.text or "" for path in paths for t in node.findall(path, NS))


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    return [_texts(item, "s:t", "s:r/s:t") for item in xml_part(archive, "xl/sharedStrings.xml")]


def _styles(archive: ZipFile) -> list[str]:
    if "xl/styles.xml" not in archive.namelist():
        return []
    style = xml_part(archive, "xl/styles.xml")
    formats = {
        item.attrib["numFmtId"]: item.attrib["formatCode"]
        for item in style.findall("s:numFmts/s:numFmt", NS)
    }
    return [
        formats.get(item.get("numFmtId", "0"), "builtin:" + item.get("numFmtId", "0"))
        for item in style.findall("s:cellXfs/s:xf", NS)
    ]


def _cell_text(cell: Element, kind: str, stored: str | None, strings: list[str]) -> str:
    if kind == "s":
        string_index = int(stored or "-1")
        if not 0 <= string_index < len(strings):
            raise ValueError("Invalid shared-string index")
        return strings[string_index]
    if kind == "inlineStr":
        return _texts(cell, "s:is/s:t", "s:is/s:r/s:t")
    return stored or ""


def _cell_span(
    cell: Element,
    locator: str,
    strings: list[str],
    styles: list[str],
    hidden: bool,
) -> Span:
    kind = cell.get("t", "n")
    value = cell.find("s:v", NS)
    stored = value.text if value is not None else None
    formula_node = cell.find("s:f", NS)
    text = _cell_text(cell, kind, stored, strings)
    if formula_node is not None:
        label = formula_node.text or "[shared/array formula; see formula metadata]"
        text = f"Formula: {label}\nStored cache: {stored if stored is not None else '[absent]'}"
    style_index = int(cell.get("s", "0"))
    if style_index < 0 or (styles and style_index >= len(styles)):
        raise ValueError("Invalid cell style index")
    return Span(
        locator=locator,
        text=text,
        formula=None if formula_node is None else ET.tostring(formula_node, encoding="unicode"),
        cached_value=stored if formula_node is not None else None,
        data_type=kind,
        number_format=styles[style_index] if styles else None,
        hidden=hidden,
    )


def _sheet_spans(
    tree: Element, prefix: str, hidden_sheet: bool, strings: list[str], styles: list[str]
) -> list[Span]:
    columns = tree.findall("s:cols/s:col", NS)
    spans: list[Span] = []
    row_number = 0
    for row in tree.findall("s:sheetData/s:row", NS):
        # `r` is optional on a row and on a cell: one without it follows the previous
        row_number = int(row.get("r", row_number + 1))
        column_index = 0
        for cell in row.findall("s:c", NS):
            address, column_index = _cell_address(cell.get("r"), row_number, column_index)
            hidden_column = any(
                item.get("hidden") in TRUE
                and int(item.attrib["min"]) <= column_index <= int(item.attrib["max"])
                for item in columns
            )
            hidden = hidden_sheet or row.get("hidden") in TRUE or hidden_column
            spans.append(_cell_span(cell, prefix + "!" + address, strings, styles, hidden))
    for merged in tree.findall("s:mergeCells/s:mergeCell", NS):
        spans.append(
            Span(
                locator=prefix + "!merged:" + merged.attrib["ref"],
                text="",
                note="Merged range; only anchor cell carries the stored value",
                data_type="metadata",
                hidden=hidden_sheet,
            )
        )
    return spans


def _sheet_path(relationship: Element) -> str:
    if relationship.get("TargetMode") == "External":
        raise ValueError("External worksheet relationship is unsupported")
    target = relationship.attrib["Target"]
    path: str = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
    if not path.startswith("xl/"):
        raise ValueError("Worksheet relationship escapes workbook")
    return path


def _comment_spans(archive: ZipFile) -> list[Span]:
    spans: list[Span] = []
    for name in sorted(archive.namelist()):
        is_comment = ("/comments/" in name and name.endswith(".xml")) or (
            name.startswith("xl/comments") and name.endswith(".xml")
        )
        if not is_comment:
            continue
        comments = xml_part(archive, name)
        authors = [item.text or "" for item in comments.findall("s:authors/s:author", NS)]
        for item in comments.findall("s:commentList/s:comment", NS):
            spans.append(
                Span(
                    locator="part:" + quote(name, safe="") + "!" + item.attrib["ref"],
                    text=_texts(item, "s:text//s:t"),
                    note=authors[int(item.get("authorId", "0"))] if authors else None,
                )
            )
    return spans


def xlsx_content(raw: bytes) -> Extraction:
    """Read OOXML lexemes, never calculate formulas, execute macros or follow links."""
    with ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len({info.filename for info in infos}) != len(infos):
            raise ValueError("Duplicate ZIP members")
        if sum(info.file_size for info in infos) > MAX_EXPANDED_BYTES:
            raise ValueError("Expanded workbook exceeds extraction limit")
        workbook = xml_part(archive, "xl/workbook.xml")
        relationships = {
            item.attrib["Id"]: item for item in xml_part(archive, "xl/_rels/workbook.xml.rels")
        }
        strings, styles = _shared_strings(archive), _styles(archive)
        date_system = workbook.find("s:workbookPr", NS)
        epoch = (
            "1904" if date_system is not None and date_system.get("date1904") in TRUE else "1900"
        )
        limitations = (
            "Workbook values are raw stored lexemes, not Excel display values. Formula caches "
            "may be absent or stale; no recalculation. Charts, drawings and embedded objects "
            "require separate visual review.",
            f"Workbook date system: {epoch}; date serials are retained without conversion.",
        )
        spans: list[Span] = []
        coverage: list[Coverage] = []
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            prefix = "sheet:" + quote(sheet.attrib["name"], safe="")
            tree = xml_part(archive, _sheet_path(relationships[sheet.attrib[REL]]))
            hidden_sheet = sheet.get("state", "visible") != "visible"
            found = _sheet_spans(tree, prefix, hidden_sheet, strings, styles)
            spans.extend(found)
            coverage.append(Coverage(locator=prefix, status=_sheet_status(found)))
        spans.extend(_comment_spans(archive))
        return Extraction(spans=tuple(spans), coverage=tuple(coverage), limitations=limitations)


def sheet_pages(raw: bytes) -> list[str]:
    """One page per sheet: an `A1: value` line for each non-empty cell, in sheet order."""
    extraction = xlsx_content(raw)
    pages: list[str] = []
    for item in extraction.coverage:
        head = item.locator + "!"
        lines = [
            f"{span.locator.removeprefix(head)}: {span.text}"
            for span in extraction.spans
            if span.locator.startswith(head) and span.data_type != "metadata" and span.text.strip()
        ]
        pages.append("\n".join(lines))
    return pages
