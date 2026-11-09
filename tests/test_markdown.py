import pytest

from rbot.markdown import to_html

HOSTILE = [
    "<script>x</script>",
    "<img src=x onerror=y>",
    "[a](javascript:alert(1))",
    "</style>",
    "**<b onclick=z>**",
    "`</style>`",
    "| <script> |\n|---|\n| </style> |",
    "```\n</pre><script>\n```",
    "- <img src=x>\n1. <script>",
    "# <script>alert(1)</script>",
    '"><svg onload=1>',
]


@pytest.mark.parametrize("md", HOSTILE)
def test_markdown_escapes_html(md: str) -> None:
    out = to_html(md)
    for bad in ("<script", "<img", "<svg", "<b ", "href=", "</style>", "onerror=y>"):
        assert bad not in out


def test_link_is_text_only() -> None:
    assert to_html("see [the page](https://example.com/x) now") == "<p>see the page now</p>"


def test_citation_brackets_stay_literal() -> None:
    assert to_html("Revenue rose [10-K.htm p.3].") == "<p>Revenue rose [10-K.htm p.3].</p>"


def test_table_renders() -> None:
    out = to_html("| A | B |\n|---|---|\n| 1 | **2** |\n| 3 | `4` |")
    assert "<table>" in out
    assert "<thead><tr><th>A</th><th>B</th></tr></thead>" in out
    assert "<td>1</td><td><strong>2</strong></td>" in out
    assert "<td><code>4</code></td>" in out


def test_blocks_and_inline() -> None:
    out = to_html(
        "# T\n## U\n#### V\n\nsome *em* and **bold**\nsame para\n\n- a\n- b\n\n1. x\n2. y\n"
        '\n```json\n{"a": "<"}\n```\n'
    )
    assert "<h1>T</h1><h2>U</h2><h4>V</h4>" in out
    assert "<p>some <em>em</em> and <strong>bold</strong> same para</p>" in out
    assert "<ul><li>a</li><li>b</li></ul>" in out
    assert "<ol><li>x</li><li>y</li></ol>" in out
    assert "<pre><code>{&quot;a&quot;: &quot;&lt;&quot;}</code></pre>" in out


def test_heading_shift_and_list_continuation() -> None:
    out = to_html("# T\n#### D\n- one\n  more\n- two", shift=2)
    assert "<h3>T</h3><h6>D</h6>" in out
    assert "<li>one more</li>" in out


def test_unclosed_fence_and_ragged_table() -> None:
    assert to_html("```\nx") == "<pre><code>x</code></pre>"
    out = to_html("| A | B |\n|---|---|\n| 1 |")
    assert "<td>1</td><td></td>" in out
