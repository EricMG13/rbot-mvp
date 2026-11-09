import base64
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_calc_cp1 import ORACLE, Pages, block

from rbot.calc.cp1 import compute as compute_cp1
from rbot.render import render

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
    "script-src 'none'; base-uri 'none'; form-action 'none'"
)
CTX = {
    "identity": {"issuer_name": "Acme <Corp>"},
    "run": {"as_of": "2026-06-30", "pathway": "full"},
}
MODULES = ["MOD_A", "MOD_B", "MOD_C"]


def handoff(title: str, gaps: str, extra: str = "") -> str:
    return (
        f"# {title}\n## Request\nr\n## Sources\ns\n## Findings\nf <script>alert(1)</script>\n"
        f"{extra}## Calculations\nnone\n## Gaps\n{gaps}\n## Next module\nn\n"
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    (w / "handoffs").mkdir(parents=True)
    (w / "handoffs" / "MOD_B.md").write_text(
        handoff("B", "- b gap one\n- b gap two"), encoding="utf-8", newline="\n"
    )
    (w / "handoffs" / "MOD_A.md").write_text(
        handoff("A", "1. a gap", "| X | Y |\n|---|---|\n| 1 | 2 |\n\n"),
        encoding="utf-8",
        newline="\n",
    )
    for name, n in (("10-K.htm", 3), ("deck.pdf", 1)):
        folder = w / "source-text" / name
        folder.mkdir(parents=True)
        for i in range(1, n + 1):
            (folder / f"p{i}.txt").write_text("x", encoding="utf-8", newline="\n")
    return w


def html_of(work: Path) -> str:
    return render(work, CTX, MODULES).read_text(encoding="utf-8")


def test_render_has_csp_and_no_script(work: Path) -> None:
    out = html_of(work)
    assert f'content="{CSP}"' in out
    assert "<script" not in out.lower()
    assert "/*STYLES*/" not in out and "/*BODY*/" not in out


def test_render_no_external_urls(work: Path) -> None:
    out = html_of(work)
    for match in re.findall(r"(?:src|href)=\"([^\"]*)\"|url\(([^)]*)\)", out):
        target = "".join(match)
        assert not re.match(r"(?i)(https?:)?//", target.strip("'\""))
        assert target.startswith(("data:", "#"))
    assert "http://" not in out.replace("http://www.w3.org/", "")


def test_cover_sections_sources_gaps(work: Path) -> None:
    out = html_of(work)
    assert out.index('<section id="MOD_A"') < out.index('<section id="MOD_B"')
    assert 'id="MOD_C"' not in out
    assert "Acme &lt;Corp&gt;" in out and "Acme <Corp>" not in out
    assert "2026-06-30" in out and "full" in out
    assert '<span class="wordmark">RBOT</span>' in out
    assert "<table>" in out and "&lt;script&gt;alert(1)" in out
    sources = out[out.index('id="sources"') : out.index('id="gaps"')]
    assert re.search(r"10-K\.htm</td><td>3<", sources)
    assert re.search(r"deck\.pdf</td><td>1<", sources)
    gaps = out[out.index('id="gaps"') :]
    for text in ("a gap", "b gap one", "b gap two"):
        assert text in gaps
    assert gaps.count('class="tag">MOD_B') == 2
    assert gaps.count('class="tag">MOD_A') == 1


def test_missing_context_renders_dash(work: Path) -> None:
    out = render(work, {}, MODULES).read_text(encoding="utf-8")
    assert "<h1>—</h1>" in out
    assert "<dt>As of</dt><dd>—</dd>" in out
    assert "<dt>Pathway</dt><dd>—</dd>" in out


def test_flat_context_keys(work: Path) -> None:
    ctx = {"identity.issuer_name": "Flat Co", "run.as_of": "2025-12-31", "run.pathway": "quick"}
    out = render(work, ctx, MODULES).read_text(encoding="utf-8")
    assert "<h1>Flat Co</h1>" in out
    assert "<dt>As of</dt><dd>2025-12-31</dd>" in out
    assert "<dt>Pathway</dt><dd>quick</dd>" in out


def gaps_of(work: Path, gaps: str) -> str:
    (work / "handoffs" / "MOD_A.md").write_text(handoff("A", gaps), encoding="utf-8", newline="\n")
    out = html_of(work)
    return out[out.index('id="gaps"') :]


def test_gaps_collected(work: Path) -> None:
    para = gaps_of(work, "Only p.43 was reviewed; no broader context.")
    assert "No gaps reported" not in para and "Only p.43 was reviewed" in para
    assert para.count('class="tag">MOD_A') == 1
    runs = gaps_of(work, "first run\nsecond line\n\nsecond run")
    assert "first run second line</li>" in runs and "second run</li>" in runs
    cont = gaps_of(work, "- item one\n  continued detail\n1. numbered\n   more")
    assert "item one continued detail</li>" in cont and "numbered more</li>" in cont


@pytest.mark.parametrize("text", ["None.", "none", "- No gaps.", "N/A", "No gaps", "- n/a."])
def test_gaps_none_means_empty(work: Path, text: str) -> None:
    (work / "handoffs" / "MOD_B.md").unlink()
    assert "No gaps reported" in gaps_of(work, text)


def test_gaps_ignores_fenced_and_other_sections(work: Path) -> None:
    out = gaps_of(work, "- real gap\n```\n- fenced\n```")
    assert "real gap" in out and "fenced" not in out


def test_gaps_line_grammar_matches_checker(work: Path) -> None:
    out = gaps_of(work, "- kept\u2028  wrapped")
    assert "kept wrapped</li>" in out


def test_logo_rejected_cases(work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brand").mkdir()
    logo = tmp_path / "brand" / "logo.png"
    logo.write_bytes(b"not a png")
    assert 'class="wordmark"' in html_of(work)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (512 * 1024))
    assert 'class="wordmark"' in html_of(work)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    assert 'class="logo"' in html_of(work)
    (tmp_path / "brand" / "logo.svg").write_bytes(b"  \n<?xml version='1.0'?><svg/>")
    assert "image/svg+xml" in html_of(work)
    (tmp_path / "brand" / "logo.svg").write_bytes(b"-----BEGIN KEY-----")
    assert "image/svg+xml" not in html_of(work)


def test_symlinked_logo_refused(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brand").mkdir()
    target = tmp_path / "real.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    try:
        (tmp_path / "brand" / "logo.png").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert 'class="wordmark"' in html_of(work)


def test_lone_surrogate_and_bidi_in_context(work: Path) -> None:
    first = render(work, {}, MODULES).read_text(encoding="utf-8")
    ctx = {"identity": {"issuer_name": "Acme \ud800 \u202e<b>\u2066 x\u200b"}}
    out = render(work, ctx, MODULES).read_text(encoding="utf-8")
    assert "<h1>Acme \ufffd \ufffd&lt;b&gt;\ufffd x\u200b</h1>" in out
    assert first != out
    assert not list(work.glob("*.tmp"))


def test_logo_override(work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brand").mkdir()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'
    (tmp_path / "brand" / "logo.svg").write_bytes(svg)
    out = html_of(work)
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    assert f'<img class="logo" src="{uri}" alt="">' in out
    assert 'class="wordmark"' not in out
    assert "<svg" not in out and "<script" not in out.lower()
    (tmp_path / "brand" / "logo.svg").unlink()
    (tmp_path / "brand" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert "data:image/png;base64," in html_of(work)


def test_license_comment_is_safe(work: Path) -> None:
    out = html_of(work)
    comment = out[out.index("<!-- Font license:") :]
    body = comment[len("<!--") : comment.index("-->")]
    assert "--" not in body
    assert "SIL OPEN FONT LICENSE" in body.upper()


def test_placeholder_text_in_handoff_not_substituted(work: Path) -> None:
    (work / "handoffs" / "MOD_A.md").write_text(
        handoff("A", "- /*LICENSE*/ gap"), encoding="utf-8", newline="\n"
    )
    out = html_of(work)
    assert out.count("SIL OPEN FONT LICENSE") == 1  # the license was not pasted into the gap
    assert "<em>LICENSE</em>/ gap" in out


def test_fonts_inlined(work: Path) -> None:
    out = html_of(work)
    assert out.count("@font-face") == 4
    assert "font/woff2;base64," in out


def test_gaps_drop_a_none_item_among_others(work: Path) -> None:
    out = gaps_of(work, "- real gap\n- None.")
    assert "real gap" in out and "None." not in out


def test_gaps_use_the_shared_unit_grammar(work: Path) -> None:
    out = gaps_of(work, "- item\nlazy paragraph\n\n#1 risk")
    assert "item</li>" in out and "lazy paragraph</li>" in out and "#1 risk</li>" in out


# --- Key metrics (Task 2.3) ---------------------------------------------------------------
def tdg_calculation(tmp_path: Path) -> dict[str, Any]:
    """The TDG CP-1 calculation: the oracle inputs through cp1.compute on synthetic pages."""
    pages = Pages(tmp_path)
    rows = []
    for period, fields in ORACLE["inputs"].items():
        for field, spec in fields.items():
            if isinstance(spec, dict):
                rows.append(pages.row(field, period, spec["value"], spec["derived"]))
            else:
                rows.append(pages.row(field, period, spec))
    calc, findings = compute_cp1(block(rows, ["fy-2023", "fy-2024"]), pages.root)
    assert findings == []
    return dict(calc)


def with_calc(work: Path, calc: Mapping[str, Any]) -> str:
    (work / "calculation.json").write_text(
        json.dumps(calc, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    return html_of(work)


def key_metrics(out: str) -> str:
    return out[out.index('id="key-metrics"') : out.index('id="MOD_A"')]


def test_key_metrics_table_values(work: Path, tmp_path: Path) -> None:
    section = key_metrics(with_calc(work, tdg_calculation(tmp_path)))
    rows = {
        m[0]: re.findall(r"<td[^>]*>(.*?)</td>", m[1])
        for m in re.findall(r"<tr><th[^>]*>(.*?)</th>(.*?)</tr>", section)
    }
    assert re.findall(r"<th[^>]*>(fy-[0-9]+)</th>", section) == ["fy-2023", "fy-2024"]
    assert rows["Gross leverage"] == ["5.87x", "6.01x"]
    assert rows["Net leverage"] == ["4.84x", "4.51x"]
    assert rows["Interest cover"] == ["2.92x", "3.24x"]
    assert rows["Liquidity"] == ["—", "—"]
    assert "not computed: missing rcf_commitment, rcf_drawn" in section  # the gap in the title
    assert "<svg" in section and 'class="legend"' in section
    assert "Leverage on reported EBITDA" not in section  # adjusted basis in both periods


def test_liquidity_amount_formatting(work: Path, tmp_path: Path) -> None:
    calc = tdg_calculation(tmp_path)
    calc["ratios"]["fy-2024"]["liquidity"] = "7141"
    assert ">USD 7,141m<" in key_metrics(with_calc(work, calc))


def test_no_calculation_no_metrics(work: Path) -> None:
    out = html_of(work)
    assert "key-metrics" not in out and "<svg" not in out and "Key metrics" not in out


def test_ebitda_basis_footnote(work: Path, tmp_path: Path) -> None:
    calc = tdg_calculation(tmp_path)
    calc["ratios"]["fy-2023"]["basis"] = "ebitda"
    note = "Leverage on reported EBITDA where EBITDA As Defined is not reported."
    assert key_metrics(with_calc(work, calc)).count(note) == 1


@pytest.mark.parametrize("char", ["\u202e", "\ud800"])
def test_period_label_sanitised_everywhere(work: Path, tmp_path: Path, char: str) -> None:
    calc = tdg_calculation(tmp_path)
    bad = f"fy-2024{char}gpj"
    calc["periods"] = [bad]
    calc["ratios"] = {bad: calc["ratios"]["fy-2024"]}
    out = with_calc(work, calc)  # must not raise UnicodeEncodeError
    assert char not in out and "fy-2024\ufffdgpj" in out


def test_malformed_calculation_fails_loudly(work: Path) -> None:
    (work / "calculation.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        html_of(work)


def test_hostile_period_labels_escaped(work: Path, tmp_path: Path) -> None:
    calc = tdg_calculation(tmp_path)
    evil = '<script>evil()</script>"onload=x'
    calc["periods"] = [evil, "fy-2024"]
    calc["ratios"][evil] = calc["ratios"].pop("fy-2023")
    calc["gaps"] = [f'{evil}: liquidity not computed: missing "x"']
    out = with_calc(work, calc)
    assert "<script" not in out.lower()
    assert "&lt;script&gt;evil()&lt;/script&gt;" in out
    assert not re.search(r"<[^>]*\sonload=", out)
