from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from rbot.handoff import SECTIONS, check, json_block, normalise

BODY = "\n\n".join(f"## {s}\nx" for s in SECTIONS)


def pages(root: Path, name: str = "x.htm", **texts: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for page, text in texts.items():
        (folder / f"{page}.txt").write_text(text, encoding="utf-8", newline="\n")
    return root


def doc(findings: str, calc: str = "none") -> str:
    return (
        f"# Title\n## Request\nr\n## Sources\ns\n## Findings\n{findings}\n"
        f"## Calculations\n{calc}\n## Gaps\ng\n## Next module\nn\n"
    )


def test_sections_in_order(tmp_path: Path) -> None:
    assert check("# T\n" + BODY + "\n### sub\nz", tmp_path) == []


def test_missing_section_reported(tmp_path: Path) -> None:
    out = check(BODY.replace("## Gaps\nx", ""), tmp_path)
    assert any("Gaps" in f for f in out)


def test_out_of_order_and_duplicate_reported(tmp_path: Path) -> None:
    swapped = "## Sources\nx\n## Request\nx\n" + BODY.split("## Findings", 1)[1].join(
        ["## Findings", ""]
    )
    assert check(swapped, tmp_path)
    assert check(BODY + "\n## Gaps\nagain", tmp_path)


def test_quote_normalisation(tmp_path: Path) -> None:
    page = "Cash.\nThe company\u2019s net   debt was  stable \u2013 mostly sta-\nble."
    root = pages(tmp_path, p0012=page)
    assert (
        check(doc('"The company\'s net debt was stable - mostly stable." [x.htm p.12]'), root) == []
    )
    assert (
        check(
            doc("\u201cThe company's net debt was stable \u2014 mostly stable.\u201d [x.htm p.12]"),
            root,
        )
        == []
    )
    bad = check(doc('"The company\'s net debt was unstable - mostly stable" [x.htm p.12]'), root)
    assert len(bad) == 1 and "x.htm p.12" in bad[0]


def test_normalise_examples() -> None:
    assert normalise("\u201cNet debt\u201d was  stable") == normalise('"Net debt" was stable')
    assert normalise("  a\u00a0b \u2013 c\u2014d  sta-\nble ") == "a b - c-d stable"


def test_case_sensitive(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="Net debt was stable")
    assert check(doc('"net debt was stable" [x.htm p.1]'), root)


def test_uncited_quote_reported(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="Net debt was stable")
    out = check(doc('"Net debt was stable" and nothing else'), root)
    assert len(out) == 1 and "no citation" in out[0]
    assert check(doc('"short one"'), root) == []


def test_unknown_page_reported(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="Net debt was stable")
    assert "x.htm p.2" in check(doc('"Net debt was stable" [x.htm p.2]'), root)[0]
    assert "y.htm" in check(doc('"Net debt was stable" [y.htm p.1]'), root)[0]


def test_citation_cannot_escape_source_text(tmp_path: Path) -> None:
    root = tmp_path / "source_text"
    pages(root, p0001="x")
    (tmp_path / "p0001.txt").write_text("Net debt was stable", encoding="utf-8")
    for name in ("..", "../x.htm", "x.htm/..", "a\\b", "x\0y"):
        out = check(doc(f'"Net debt was stable" [{name} p.1]'), root)
        assert len(out) == 1 and "unknown source" in out[0]


def test_json_block_parsed(tmp_path: Path) -> None:
    calc = 'before\n```json\n{"a": 1.50, "b": [2]}\n```\n'
    assert json_block(doc("f", calc)) == {"a": Decimal("1.50"), "b": [2]}
    assert check(doc("f", calc), tmp_path, require_json=True) == []
    assert json_block(doc("f")) is None
    outside = doc("f").replace("## Gaps\ng", '## Gaps\n```json\n{"a": 1}\n```')
    assert json_block(outside) is None


def test_require_json_findings(tmp_path: Path) -> None:
    blk = "```json\n{}\n```\n"
    assert check(doc("f"), tmp_path) == []
    assert check(doc("f"), tmp_path, require_json=True)
    assert check(doc("f", blk + blk), tmp_path, require_json=True)
    assert check(doc("f", "```json\n{bad\n```"), tmp_path, require_json=True)
    assert check(doc("f", '```json\n{"a": NaN}\n```'), tmp_path, require_json=True)
    assert check(doc("f", '```json\n{"a": -Infinity}\n```'), tmp_path, require_json=True)
    assert check(doc("f", "```python\n{}\n```"), tmp_path, require_json=True)


@given(st.text())
def test_normalise_idempotent(s: str) -> None:
    assert normalise(normalise(s)) == normalise(s)
