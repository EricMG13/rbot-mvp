import base64
import re
from pathlib import Path

import pytest

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
    assert "—" in out and "<h1>" in out


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
    (tmp_path / "brand" / "logo.png").write_bytes(b"\x89PNG")
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
