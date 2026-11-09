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


def test_short_straight_quoted_terms_keep_pairing(tmp_path: Path) -> None:
    prose = 'The "EBITDA" figure is the company\'s headline measure, unlike "Net sales" or "EPS".'
    assert check(doc(prose), tmp_path) == []


def test_fenced_json_strings_not_scanned_as_quotes(tmp_path: Path) -> None:
    calc = '```json\n{"metric": "EBITDA As Defined margin", "m": 52.6}\n```'
    assert check(doc("f", calc), tmp_path, require_json=True) == []


def test_inner_curly_quotes_in_straight_quote(tmp_path: Path) -> None:
    root = pages(tmp_path, p0043="See “Non-GAAP Financial Measures” in this discussion.")
    ok = doc('"See “Non-GAAP Financial Measures” in this discussion." [x.htm p.43]')
    assert check(ok, root) == []


def test_dash_and_hyphen_wraps(tmp_path: Path) -> None:
    root = pages(
        tmp_path,
        p0001="Reports non-\nGAAP measures. Total Company—\nNet Sales rose. A well-\nknown fact.",
    )
    for q in (
        "Reports non-GAAP measures.",
        "Total Company—Net Sales rose.",
        "A well-known fact.",
        "A wellknown fact.",
    ):
        assert check(doc(f'"{q}" [x.htm p.1]'), root) == []
    assert check(doc('"A well known fact." [x.htm p.1]'), root)


def test_blank_quote_and_deep_json(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="anything")
    assert "empty" in check(doc('"              " [x.htm p.1]'), root)[0]
    deep = "```json\n" + "[" * 100000 + "]" * 100000 + "\n```"
    assert check(doc("f", deep), tmp_path, require_json=True)


FAB = '"Revenue grew 50% in fiscal 2024." [x.htm p.1]'


def test_stray_quote_cannot_silence_fabricated_quote(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="Revenue grew 5% in fiscal 2024.")
    unclosed = check(doc(f'He said "hi. {FAB}'), root)
    assert any("unclosed quote" in f for f in unclosed)
    assert any("could not be paired before [x.htm p.1]" in f for f in unclosed)
    assert check(doc(f'The 12" pipe line is new. {FAB}'), root)
    earlier = doc(FAB).replace("## Sources\nx", '## Sources\nThe 3" item')
    found = check(earlier, root)
    assert any("not found" in f for f in found)
    assert not any("unclosed" in f for f in found)


def test_unpaired_short_cited_quote_and_paraphrase(tmp_path: Path) -> None:
    root = pages(tmp_path, p0001="Revenue grew")
    assert any("could not be paired" in f for f in check(doc('The "EPS" [x.htm p.1]'), root))
    assert check(doc("Revenue grew [x.htm p.1]"), root) == []


def test_spaced_dash_wrap() -> None:
    assert normalise("The company —\nnot the parent") == "The company - not the parent"
    assert normalise("The company—\nnot") == "The company-not"


def quoted_pages(tmp_path: Path) -> Path:
    return pages(tmp_path, p0001="The issuer is Acme Corp.", p0002="Revenue grew in fiscal 2024.")


def test_quoted_findings_rejects_item_without_quote(tmp_path: Path) -> None:
    root = quoted_pages(tmp_path)
    text = doc('- "The issuer is Acme Corp." [x.htm p.1]\n- The cover page is on page 80.')
    assert check(text, root) == []
    out = check(text, root, quoted_findings=True)
    assert len(out) == 1 and out[0].startswith("Findings item with no verified quote:")
    assert "cover page" in out[0]


def test_quoted_findings_rejects_paragraph_and_short_quote(tmp_path: Path) -> None:
    root = quoted_pages(tmp_path)
    text = doc('The issuer is Acme.\n\nShort "Acme" [x.htm p.1] only.')
    out = check(text, root, quoted_findings=True)
    assert sum(f.startswith("Findings item with no verified quote") for f in out) == 2


def test_quoted_findings_rejects_quote_on_wrong_page(tmp_path: Path) -> None:
    root = quoted_pages(tmp_path)
    out = check(doc('- "The issuer is Acme Corp." [x.htm p.2]'), root, quoted_findings=True)
    assert out and "not found" in out[0]


def test_quoted_findings_accepts_fully_quoted(tmp_path: Path) -> None:
    root = quoted_pages(tmp_path)
    text = doc(
        '- "The issuer is Acme Corp." [x.htm p.1]\n'
        '- Growth: "Revenue grew in fiscal 2024." [x.htm p.2]\n'
        "  continued line\n\n### Detail\n"
        '1. "The issuer is Acme Corp." [x.htm p.1]\n```\nunquoted fenced line\n```'
    )
    assert check(text, root, quoted_findings=True) == []
