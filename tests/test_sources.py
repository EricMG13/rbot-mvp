import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pypdf import PdfReader, PdfWriter

from rbot.sources import SourceError, SourceText, extract_pages, write_source_text
from rbot.xlsx import xml_part

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_xlsx(sheet_xml: str | None = None, shared: str | None = None) -> bytes:
    sheet = sheet_xml or (
        f'<worksheet xmlns="{MAIN}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Cost</t></is></c><c r="B2"/></row>'
        "</sheetData></worksheet>"
    )
    parts = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/sharedStrings.xml": shared or f'<sst xmlns="{MAIN}"><si><t>Revenue</t></si></sst>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_pdf_fixture_is_valid(pdf_bytes: bytes) -> None:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert "Second page text" in (reader.pages[1].extract_text() or "")


def test_pdf_pages_one_per_page(sources_dir: Path, pdf_bytes: bytes) -> None:
    path = sources_dir / "a.pdf"
    path.write_bytes(pdf_bytes)
    pages = extract_pages(path)
    assert len(pages) == 2
    assert "Second page text" in pages[1]


def test_pdf_encrypted_raises(sources_dir: Path, pdf_bytes: bytes) -> None:
    writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
    writer.encrypt("secret")
    path = sources_dir / "enc.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(SourceError):
        extract_pages(path)


def test_pdf_garbage_raises(sources_dir: Path) -> None:
    path = sources_dir / "bad.pdf"
    path.write_bytes(b"not a pdf")
    with pytest.raises(SourceError):
        extract_pages(path)


def test_html_chunks_3000(sources_dir: Path) -> None:
    path = sources_dir / "a.htm"
    path.write_text("<html><body>" + "<p>" + "x" * 7000 + "</p></body></html>", encoding="utf-8")
    pages = extract_pages(path)
    assert [len(p) for p in pages[:-1]] == [3000] * (len(pages) - 1)
    assert len(pages) == 3
    assert "".join(pages).count("x") == 7000


def test_html_undecodable_raises(sources_dir: Path) -> None:
    path = sources_dir / "a.html"
    path.write_bytes(b"<p>a\x00b</p>")
    with pytest.raises(SourceError):
        extract_pages(path)


def test_text_chunks_12000(sources_dir: Path) -> None:
    path = sources_dir / "a.txt"
    path.write_text("y" * 25000, encoding="utf-8")
    assert [len(p) for p in extract_pages(path)] == [12000, 12000, 1000]


def test_text_invalid_utf8_raises(sources_dir: Path) -> None:
    path = sources_dir / "a.csv"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(SourceError):
        extract_pages(path)


def test_xlsx_sheet_page_has_cell_refs(sources_dir: Path) -> None:
    path = sources_dir / "a.xlsx"
    path.write_bytes(make_xlsx())
    pages = extract_pages(path)
    assert len(pages) == 1
    assert pages[0].splitlines() == ["A1: Revenue", "B1: 42", "A2: Cost"]


def test_xlsx_entity_refused(sources_dir: Path) -> None:
    evil = f'<!DOCTYPE x [<!ENTITY a "b">]><worksheet xmlns="{MAIN}"><sheetData/></worksheet>'
    path = sources_dir / "e.xlsx"
    path.write_bytes(make_xlsx(sheet_xml=evil))
    with pytest.raises(SourceError):
        extract_pages(path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("p.xml", '<!ENTITY a "b"><r/>')
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive, pytest.raises(ValueError):
        xml_part(archive, "p.xml")


def test_xlsx_bad_zip_raises(sources_dir: Path) -> None:
    path = sources_dir / "z.xlsx"
    path.write_bytes(b"nope")
    with pytest.raises(SourceError):
        extract_pages(path)


def test_xml_part_plain_parses() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("p.xml", "<r/>")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        assert isinstance(xml_part(archive, "p.xml"), ET.Element)


def test_unsupported_skipped(sources_dir: Path, tmp_path: Path) -> None:
    (sources_dir / "a.txt").write_text("hello", encoding="utf-8")
    (sources_dir / "b.docx").write_bytes(b"PK")
    out = tmp_path / "out"
    done, skipped = write_source_text(sources_dir, out)
    assert skipped == ["b.docx"]
    assert done == [SourceText(name="a.txt", pages=1)]


def test_page_files_utf8_lf(sources_dir: Path, tmp_path: Path, pdf_bytes: bytes) -> None:
    (sources_dir / "a.txt").write_bytes("line1\r\nline2 é\r\n".encode())
    (sources_dir / "b.pdf").write_bytes(pdf_bytes)
    out = tmp_path / "out"
    done, _ = write_source_text(sources_dir, out)
    assert {s.name: s.pages for s in done} == {"a.txt": 1, "b.pdf": 2}
    for page in (out / "a.txt" / "p0001.txt", out / "b.pdf" / "p0002.txt"):
        assert b"\r" not in page.read_bytes()
    assert (out / "a.txt" / "p0001.txt").read_text(encoding="utf-8").startswith("line1\nline2 é")


def _book(extra: dict[str, str] | None = None, sheet_cells: str | None = None) -> bytes:
    cells = sheet_cells or '<row r="1"><c r="A1"><v>1</v></c></row>'
    sheet = f'<worksheet xmlns="{MAIN}"><sheetData>{cells}</sheetData></worksheet>'
    base = make_xlsx(sheet_xml=sheet)
    if not extra:
        return base
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(base)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for item in src.namelist():
            dst.writestr(item, src.read(item))
        for name, body in extra.items():
            dst.writestr(name, body)
    return buffer.getvalue()


def test_xlsx_comment_author_out_of_range_raises(sources_dir: Path) -> None:
    comments = (
        f'<comments xmlns="{MAIN}"><authors><author>a</author></authors><commentList>'
        '<comment ref="A1" authorId="5"><text><t>x</t></text></comment></commentList></comments>'
    )
    path = sources_dir / "c.xlsx"
    path.write_bytes(_book({"xl/comments1.xml": comments}))
    with pytest.raises(SourceError):
        extract_pages(path)


def test_xlsx_corrupt_deflate_raises(sources_dir: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(make_xlsx())) as src:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.namelist():
                dst.writestr(item, src.read(item))
    raw = bytearray(buffer.getvalue())
    start = raw.find(b"PK\x03\x04")
    name_len = int.from_bytes(raw[start + 26 : start + 28], "little")
    extra_len = int.from_bytes(raw[start + 28 : start + 30], "little")
    data = start + 30 + name_len + extra_len
    raw[data : data + 8] = b"\xff" * 8
    path = sources_dir / "d.xlsx"
    path.write_bytes(bytes(raw))
    with pytest.raises(SourceError):
        extract_pages(path)


def test_xlsx_duplicate_cell_address_raises(sources_dir: Path) -> None:
    cells = '<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>'
    path = sources_dir / "dup.xlsx"
    path.write_bytes(_book(sheet_cells=cells))
    with pytest.raises(SourceError):
        extract_pages(path)


def test_xlsx_malformed_xml_raises(sources_dir: Path) -> None:
    path = sources_dir / "m.xlsx"
    path.write_bytes(make_xlsx(sheet_xml="<worksheet><unclosed>"))
    with pytest.raises(SourceError):
        extract_pages(path)


def test_pdf_damaged_structure_raises(sources_dir: Path, pdf_bytes: bytes) -> None:
    path = sources_dir / "dmg.pdf"
    path.write_bytes(pdf_bytes.replace(b"/Kids [3 0 R 5 0 R]", b"/Kids 7        "))
    with pytest.raises(SourceError):
        extract_pages(path)


def test_text_bom_stripped_and_nul_refused(sources_dir: Path) -> None:
    bom = sources_dir / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbfhello")
    assert extract_pages(bom) == ["hello"]
    nul = sources_dir / "nul.txt"
    nul.write_bytes(b"a\x00b")
    with pytest.raises(SourceError):
        extract_pages(nul)


def test_stale_pages_cleared(sources_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    stale = out / "a.txt" / "p0005.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    (sources_dir / "a.txt").write_text("new", encoding="utf-8")
    write_source_text(sources_dir, out)
    assert not stale.exists()
    assert (out / "a.txt" / "p0001.txt").read_text(encoding="utf-8") == "new"
