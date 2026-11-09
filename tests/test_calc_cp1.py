import json
import tempfile
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from rbot.calc.cp1 import FIELDS
from rbot.calc.cp1 import compute as _compute
from rbot.handoff import verify_quote
from rbot.html_text import convert_html

ORACLE = json.loads(Path("tests/oracles/tdg-fy2024-cp1.json").read_text(encoding="utf-8"))
SRC = "x.htm"


def compute(block: Any, source_text: Path) -> tuple[dict[str, Any], list[str]]:
    calc, findings = _compute(block, source_text)
    return dict(calc), findings


class Pages:
    """Writes one synthetic page per row quote under source-text/x.htm."""

    def __init__(self, root: Path) -> None:
        self.root = root / "source-text"
        (self.root / SRC).mkdir(parents=True)
        self.n = 0

    def cite(
        self, value: str, label: str = "Reported amount", shown: str | None = None
    ) -> dict[str, object]:
        self.n += 1
        quote = f"{label} was {shown or f'{Decimal(value):,f}'} million"
        (self.root / SRC / f"p{self.n:04d}.txt").write_text(
            f"Header\n{quote} in the year.\n", encoding="utf-8", newline="\n"
        )
        return {"source": SRC, "page": self.n, "quote": quote}

    def row(
        self,
        field: str,
        period: str,
        value: str,
        derived: list[str] | None = None,
        shown: str | None = None,
    ) -> dict[str, object]:
        out: dict[str, object] = {"field": field, "period": period, "value": value}
        if derived is None:
            return out | self.cite(value, shown=shown)
        out["derived"] = [{"value": v} | self.cite(v) for v in derived]
        return out


def block(rows: list[Any], periods: list[str] | None = None) -> dict[str, object]:
    return {
        "currency": "USD",
        "unit": "m",
        "periods": periods or ["fy-2023", "fy-2024"],
        "rows": rows,
    }


def quant(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))


def test_tdg_answer_key(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = []
    for period, fields in ORACLE["inputs"].items():
        for field, spec in fields.items():
            if isinstance(spec, dict):
                rows.append(pages.row(field, period, spec["value"], spec["derived"]))
            else:
                rows.append(pages.row(field, period, spec))
    calc, findings = compute(block(rows), pages.root)
    assert findings == []
    assert calc["currency"] == ORACLE["currency"]
    assert calc["unit"] == ORACLE["unit"]
    for period, expected in ORACLE["outputs_2dp"].items():
        for name, want in expected.items():
            got = Decimal(calc["ratios"][period][name]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            )
            assert str(got) == want, (period, name)
    assert calc["ratios"]["fy-2024"]["basis"] == "adjusted_ebitda"
    assert calc["inputs"]["fy-2024"]["total_debt"] == "25068"
    assert calc["gaps"] == [
        "fy-2023: liquidity not computed: missing rcf_commitment, rcf_drawn",
        "fy-2024: liquidity not computed: missing rcf_commitment, rcf_drawn",
    ]


def test_derived_sum_mismatch_reported(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [pages.row("total_debt", "fy-2024", "25069", ["24581", "487"])]
    calc, findings = compute(block(rows), pages.root)
    assert len(findings) == 1 and "total_debt" in findings[0] and "sum" in findings[0]
    assert "total_debt" not in calc["inputs"].get("fy-2024", {})


def test_digits_not_in_quote_reported(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("cash_and_equivalents", "fy-2024", "6261")
    row["value"] = "6262"
    _, findings = compute(block([row]), pages.root)
    assert len(findings) == 1 and "not printed as one number" in findings[0]


def test_digits_allow_parenthesised_negative(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("cfo", "fy-2024", "-302", shown="(302)")
    calc, findings = compute(block([row]), pages.root)
    assert findings == []
    assert calc["inputs"]["fy-2024"]["cfo"] == "-302"


def test_float_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("cash_and_equivalents", "fy-2024", "6261")
    row["value"] = Decimal("6261.5")  # what json_block yields for a JSON float
    _, findings = compute(block([row]), pages.root)
    assert any(f.startswith("number must be a string") for f in findings)
    row["value"] = 6261.5
    _, findings = compute(block([row]), pages.root)
    assert any(f.startswith("number must be a string") for f in findings)
    row["value"] = 6261  # JSON int
    _, findings = compute(block([row]), pages.root)
    assert any(f.startswith("number must be a string") for f in findings)


def test_float_in_json_block_rejected(tmp_path: Path) -> None:
    from rbot.handoff import json_block

    text = (
        '## Calculations\n```json\n{"currency":"USD","unit":"m","periods":["fy-2024"],'
        '"rows":[{"field":"revenue","period":"fy-2024","value":"1","note":1.5}]}\n```\n'
    )
    parsed = json_block(text)
    _, findings = compute(parsed, tmp_path)
    assert any(f.startswith("number must be a string") for f in findings)


def test_missing_operand_null_not_zero(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [pages.row("total_debt", "fy-2024", "100")]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    ratios = calc["ratios"]["fy-2024"]
    assert ratios["gross_leverage"] is None
    assert ratios["net_leverage"] is None
    assert ratios["interest_cover"] is None
    assert ratios["liquidity"] is None
    assert ratios["basis"] is None
    assert "fy-2024: gross_leverage not computed: missing adjusted_ebitda or ebitda" in calc["gaps"]
    missing = "adjusted_ebitda or ebitda, interest_expense_net"
    assert f"fy-2024: interest_cover not computed: missing {missing}" in calc["gaps"]


def test_basis_falls_back_to_ebitda_and_liquidity(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [
        pages.row("total_debt", "fy-2024", "300"),
        pages.row("ebitda", "fy-2024", "100"),
        pages.row("cash_and_equivalents", "fy-2024", "50"),
        pages.row("interest_expense_net", "fy-2024", "40"),
        pages.row("rcf_commitment", "fy-2024", "500"),
        pages.row("rcf_drawn", "fy-2024", "120"),
    ]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    assert calc["ratios"]["fy-2024"] == {
        "gross_leverage": "3.0000",
        "net_leverage": "2.5000",
        "interest_cover": "2.5000",
        "liquidity": "430",
        "basis": "ebitda",
    }
    assert calc["gaps"] == []


def test_zero_denominator_is_null_with_gap(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [
        pages.row("total_debt", "fy-2024", "300"),
        pages.row("ebitda", "fy-2024", "0"),
        pages.row("cash_and_equivalents", "fy-2024", "50"),
        pages.row("interest_expense_net", "fy-2024", "0"),
    ]
    calc, _ = compute(block(rows, ["fy-2024"]), pages.root)
    ratios = calc["ratios"]["fy-2024"]
    assert ratios["gross_leverage"] is None and ratios["net_leverage"] is None
    assert ratios["interest_cover"] is None
    assert "fy-2024: gross_leverage not computed: zero or negative denominator" in calc["gaps"]
    assert "fy-2024: net_leverage not computed: zero or negative denominator" in calc["gaps"]
    assert "fy-2024: interest_cover not computed: zero or negative denominator" in calc["gaps"]


def test_rounding_half_even(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [pages.row("total_debt", "fy-2024", "1"), pages.row("ebitda", "fy-2024", "32")]
    calc, _ = compute(block(rows, ["fy-2024"]), pages.root)
    assert calc["ratios"]["fy-2024"]["gross_leverage"] == "0.0312"


def test_structure_findings(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    cases: dict[str, dict[str, object]] = {
        "unknown field": good | {"field": "bogus"},
        "missing field": {k: v for k, v in good.items() if k != "field"},
        "period": good | {"period": "fy-1999"},
        "page": good | {"page": 0},
        "page bool": good | {"page": True},
        "quote too short": good | {"quote": "10 million"},
        "quote not found": good | {"quote": "Revenue was 10 million elsewhere"},
        "unknown source": good | {"source": "nope.htm"},
        "no value": {k: v for k, v in good.items() if k != "value"},
        "not numeric": good | {"value": "ten"},
        "non finite": good | {"value": "NaN"},
        "derived and source": good | {"derived": []},
    }
    for name, row in cases.items():
        calc, findings = compute(block([row], ["fy-2024"]), pages.root)
        assert findings, name
        assert "revenue" not in calc["inputs"].get("fy-2024", {}), name


def test_maturity_fields_and_duplicates(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    ok = pages.row("maturity_2027", "fy-2024", "700")
    assert "maturity_2027" not in FIELDS
    calc, findings = compute(block([ok], ["fy-2024"]), pages.root)
    assert findings == [] and calc["inputs"]["fy-2024"]["maturity_2027"] == "700"
    _, findings = compute(block([pages.row("maturity_27", "fy-2024", "700")]), pages.root)
    assert findings
    _, findings = compute(block([ok, ok], ["fy-2024"]), pages.root)
    assert any("duplicate" in f for f in findings)


def test_block_shape_findings(tmp_path: Path) -> None:
    bads: list[Any] = [
        None,
        [],
        {"rows": []},
        {"currency": "USD", "unit": "m", "periods": "x", "rows": []},
    ]
    for bad in bads:
        calc, findings = compute(bad, tmp_path)
        assert findings and calc["ratios"] == {}
    calc, findings = compute(
        {"currency": "USD", "unit": "m", "periods": ["a"], "rows": {}}, tmp_path
    )
    assert findings
    calc, findings = compute(
        {"currency": "USD", "unit": "m", "periods": ["a"], "rows": ["x"]}, tmp_path
    )
    assert findings


def test_verify_quote(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    cite = pages.cite("5")
    assert verify_quote(str(cite["quote"]), SRC, 1, pages.root) is None
    assert "quote not found on x.htm p.1" in (
        verify_quote("absent text here", SRC, 1, pages.root) or ""
    )
    assert (verify_quote("anything at all", SRC, 9, pages.root) or "").startswith(
        "page not found on"
    )
    assert (verify_quote("anything at all", "no.htm", 1, pages.root) or "").startswith(
        "unknown source"
    )


def _findings(rows: list[Any], pages: Pages, periods: list[str] | None = None) -> list[str]:
    return compute(block(rows, periods or ["fy-2024"]), pages.root)[1]


def test_exact_findings_for_sourced_rows(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    lab = "revenue fy-2024"
    assert _findings([good | {"value": "11"}], pages) == [
        f"{lab}: value '11' is not printed as one number in the quote"
    ]
    assert _findings([good | {"value": 5}], pages) == [
        "number must be a string: block.rows[0].value = 5",
        f"{lab}: value must be a string",
    ]
    assert _findings([good | {"value": "ten"}], pages) == [
        f"{lab}: value is not a plain decimal of at most 15 significant digits: 'ten'"
    ]
    assert _findings([good | {"value": "NaN"}], pages) == [
        f"{lab}: value is not a plain decimal of at most 15 significant digits: 'NaN'"
    ]
    assert _findings([good | {"source": ""}], pages) == [
        f"{lab}: source must be a non-empty string"
    ]
    assert _findings([good | {"source": 5}], pages) == [
        "number must be a string: block.rows[0].source = 5",
        f"{lab}: source must be a non-empty string",
    ]
    assert _findings([good | {"page": 0}], pages) == [f"{lab}: page must be an integer >= 1"]
    assert _findings([good | {"quote": "short"}], pages) == [
        f"{lab}: quote must be a string of at least 12 characters"
    ]
    assert _findings([good | {"quote": "Reported amount was 10 elsewhere"}], pages) == [
        f'{lab}: quote not found on x.htm p.1: "Reported amount was 10 elsewhere"'
    ]


def test_value_whitespace_ignored_in_digit_check(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("revenue", "fy-2024", " 1000 ", shown="1,000")
    calc, findings = compute(block([row], ["fy-2024"]), pages.root)
    assert findings == [] and calc["inputs"]["fy-2024"]["revenue"] == "1000"


def test_quote_length_boundary(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    (pages.root / SRC / "p0001.txt").write_text("Revenue is 1 m.", encoding="utf-8", newline="\n")
    row = {"field": "revenue", "period": "fy-2024", "value": "1", "source": SRC, "page": 1}
    assert _findings([row | {"quote": "Revenue is 1"}], pages) == []
    assert _findings([row | {"quote": "Revenue is "}], pages) == [
        "revenue fy-2024: quote must be a string of at least 12 characters"
    ]


def test_exact_findings_for_derived_rows(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    lab = "total_debt fy-2024"
    row = pages.row("total_debt", "fy-2024", "30", ["10", "20"])
    assert _findings([row], pages) == []
    for key in ("source", "quote", "page"):
        out = _findings([row | {key: "x"}], pages)
        assert out == [f"{lab}: row is both sourced and derived"], key
    for bad in (5, [], ["x"]):
        assert _findings([row | {"derived": bad}], pages) == (
            [
                "number must be a string: block.rows[0].derived = 5",
                f"{lab}: derived must list at least two components",
            ]
            if bad == 5
            else [f"{lab}: derived must list at least two components"]
        )
    parts = row["derived"]
    assert isinstance(parts, list)
    two_bad = [5, parts[0] | {"value": "11"}, parts[1] | {"value": "21"}]
    assert _findings([row | {"derived": two_bad}], pages) == [
        "number must be a string: block.rows[0].derived[0] = 5",
        f"{lab} component 0: must be an object",
        f"{lab} component 1: value '11' is not printed as one number in the quote",
        f"{lab} component 2: value '21' is not printed as one number in the quote",
    ]
    assert _findings([row | {"value": "31"}], pages) == [
        f"{lab}: value 31 is not the sum of its components"
    ]
    assert _findings([row | {"value": "x"}], pages) == [
        f"{lab}: value is not a plain decimal of at most 15 significant digits: 'x'"
    ]
    bad_both = [parts[0] | {"value": "11"}, parts[1]]
    assert _findings([row | {"derived": bad_both, "value": "x"}], pages) == [
        f"{lab} component 0: value '11' is not printed as one number in the quote",
        f"{lab}: value is not a plain decimal of at most 15 significant digits: 'x'",
    ]


def test_exact_findings_for_rows_and_block(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    assert _findings([5, good], pages) == [
        "number must be a string: block.rows[0] = 5",
        "rows[0] must be an object",
    ]
    calc, _ = compute(block([5, good], ["fy-2024"]), pages.root)
    assert calc["inputs"] == {"fy-2024": {"revenue": "10"}}
    assert _findings([good | {"field": "zzz"}], pages) == [
        "rows[0]: unknown or missing field: 'zzz'"
    ]
    assert _findings([good | {"period": "fy-1"}], pages) == [
        "revenue fy-1: period is not in periods"
    ]
    assert _findings([good, good], pages) == ["revenue fy-2024: duplicate row"]
    assert compute(block([good]) | {"rows": 5}, pages.root)[1] == [
        "number must be a string: block.rows = 5",
        "block.rows must be a list",
    ]
    empty: dict[str, Any] = {
        "currency": None,
        "unit": None,
        "periods": [],
        "inputs": {},
        "ratios": {},
        "gaps": [],
    }
    assert compute(None, pages.root) == (empty, ["block must be a JSON object"])
    for periods in ([], [5], [""], "fy"):
        calc, findings = compute(block([]) | {"periods": periods}, pages.root)
        assert calc == empty | {"currency": "USD", "unit": "m"}
        assert findings[-1] == "block.periods must be a non-empty list of strings"
    for key in ("currency", "unit"):
        _, findings = compute(block([]) | {key: 5}, pages.root)
        assert findings[-1] == f"block.{key} must be a string"
    calc, _ = compute(block([good], ["fy-2024"]), pages.root)
    assert calc["periods"] == ["fy-2024"]
    assert set(calc) == {"currency", "unit", "periods", "inputs", "ratios", "gaps"}


def test_rounding_mode_is_explicit(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [pages.row("total_debt", "fy-2024", "1"), pages.row("ebitda", "fy-2024", "32")]
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        calc, _ = compute(block(rows, ["fy-2024"]), pages.root)
    assert calc["ratios"]["fy-2024"]["gross_leverage"] == "0.0312"


def test_exponent_and_odd_values_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for bad in (
        "1E3",
        "１２",
        "1,000",
        "+5",
        "1.",
        ".5",
        "",
        "1234567890123456",
        "0.1234567890123456",
    ):
        row = pages.row("revenue", "fy-2024", "5") | {"value": bad}
        calc, findings = compute(block([row], ["fy-2024"]), pages.root)
        assert findings, bad
        assert "revenue" not in calc["inputs"]["fy-2024"], bad
    row = pages.row("revenue", "fy-2024", "123456789012345")
    assert compute(block([row], ["fy-2024"]), pages.root)[1] == []


def test_liquidity_gap_names_each_missing_operand(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    full = ["cash_and_equivalents", "rcf_commitment", "rcf_drawn"]
    for gone in full:
        rows = [pages.row(f, "fy-2024", "5") for f in full if f != gone]
        calc, _ = compute(block(rows, ["fy-2024"]), pages.root)
        assert calc["ratios"]["fy-2024"]["liquidity"] is None
        assert f"fy-2024: liquidity not computed: missing {gone}" in calc["gaps"]


def put(pages: Pages, text: str) -> int:
    """Write `text` as the next page and return its number."""
    pages.n += 1
    (pages.root / SRC / f"p{pages.n:04d}.txt").write_text(
        text + "\n", encoding="utf-8", newline="\n"
    )
    return pages.n


def one(pages: Pages, field: str, value: str, text: str, quote: str) -> tuple[Any, list[str]]:
    n = put(pages, text)
    row = {"field": field, "period": "fy-2024", "value": value, "source": SRC, "page": n}
    return compute(block([row | {"quote": quote}], ["fy-2024"]), pages.root)


def test_value_must_be_one_whole_number_token(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    wrong = [
        ("rcf_drawn", "0", "As of December 31, 2024, borrowed $1,250 million under the RCF"),
        ("cash_and_equivalents", "6261", "Cash and cash equivalents were $16,261 million"),
        ("cash_and_equivalents", "6261", "Cash and cash equivalents were $6,261.5 million"),
        ("revenue", "25", "Total revenue of $2,500 million for fiscal 2024"),
        ("interest_expense_net", "8611", "Interest expense, net 1,286 1,164 for two years"),
        ("revenue", "5", "Total revenue of $0.5 million in fiscal 2024 only"),
        ("revenue", "0", "Total revenue of 10-K filers in fiscal 2024 only"),
    ]
    for field, value, text in wrong:
        calc, findings = one(pages, field, value, text, text)
        assert findings == [
            f"{field} fy-2024: value {value!r} is not printed as one number in the quote"
        ], text
        assert calc["inputs"]["fy-2024"] == {}


def test_value_matches_formatted_token_and_nil_marker(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    right = [
        ("cash_and_equivalents", "6261", "Cash and cash equivalents were $6,261 million", "6261"),
        (
            "cash_and_equivalents",
            "6261.5",
            "Cash and cash equivalents were 6,261.5 million",
            "6261.5",
        ),
        ("rcf_drawn", "0", "Revolver borrowings outstanding \u2014 at year end", "0"),
        ("rcf_drawn", "0", "Revolver borrowings outstanding \u2013 at year end", "0"),
        ("rcf_drawn", "0", "Revolver borrowings outstanding - at year end", "0"),
        ("rcf_drawn", "0", "Revolver borrowings outstanding 0 at year end", "0"),
        ("rcf_drawn", "0", "Revolver borrowings outstanding \u2014", "0"),
    ]
    for field, value, text, want in right:
        calc, findings = one(pages, field, value, text, text)
        assert findings == [], text
        assert calc["inputs"]["fy-2024"][field] == want
    bad = "Revolver borrowings outstanding a-b at year end"
    assert one(pages, "rcf_drawn", "0", bad, bad)[1]
    bad = "Revolver borrowings outstanding \u2014x at year end"
    assert one(pages, "rcf_drawn", "0", bad, bad)[1]
    bad = "Revolver borrowings outstanding x\u2014 at year end"
    assert one(pages, "rcf_drawn", "0", bad, bad)[1]


def test_sign_must_be_printed(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    plain = "Interest expense, net 1,286 for the year"
    calc, findings = one(pages, "interest_expense_net", "-1286", plain, plain)
    assert findings and calc["inputs"]["fy-2024"] == {}
    for printed in ("(1,286)", "-1,286", "\u22121,286", "($1,286)", "-$1,286", "$(1,286)"):
        text = f"Interest expense, net {printed} for the year"
        calc, findings = one(pages, "interest_expense_net", "-1286", text, text)
        assert findings == [], printed
        assert calc["inputs"]["fy-2024"]["interest_expense_net"] == "-1286"
    for printed in ("(1,286)", "1,286"):  # a positive value may match a parenthesised figure
        text = f"Interest expense, net {printed} for the year"
        assert one(pages, "interest_expense_net", "1286", text, text)[1] == []
    for text in (
        "Interest 2023-1,286 for the year",
        "Interest expense, net - 1,286 for the year",
        "Interest expense, net (1,286 for the year",
        "Interest expense, net 1,286) for the year",
    ):
        assert one(pages, "interest_expense_net", "-1286", text, text)[1], text


def test_balance_fields_must_not_be_negative(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for field in (
        "total_debt",
        "senior_secured_debt",
        "cash_and_equivalents",
        "rcf_commitment",
        "rcf_drawn",
        "maturity_2027",
    ):
        text = f"Amount shown for the item (487) at year end for {field}"
        calc, findings = one(pages, field, "-487", text, text)
        assert findings == [f"{field} fy-2024: {field} must not be negative: -487"]
        assert calc["inputs"]["fy-2024"] == {}
    for field in ("cfo", "capex", "revenue", "ebitda"):
        text = f"Amount shown for the item (487) at year end for {field}"
        assert one(pages, field, "-487", text, text)[1] == []


def test_derived_cancelling_components_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Total debt 24,581 and current portion 487 at year end")
    q = "Total debt 24,581 and current portion 487"
    parts = [{"value": v, "source": SRC, "page": n, "quote": q} for v in ("487", "-487")]
    row = {"field": "total_debt", "period": "fy-2024", "value": "0", "derived": parts}
    calc, findings = compute(block([row], ["fy-2024"]), pages.root)
    assert findings and calc["inputs"]["fy-2024"] == {}


def test_nonpositive_denominators_give_null_and_gap(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [
        pages.row("total_debt", "fy-2024", "300"),
        pages.row("cash_and_equivalents", "fy-2024", "50"),
        pages.row("adjusted_ebitda", "fy-2024", "-150", shown="(150)"),
        pages.row("interest_expense_net", "fy-2024", "-40", shown="(40)"),
    ]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    r = calc["ratios"]["fy-2024"]
    assert (r["gross_leverage"], r["net_leverage"], r["interest_cover"]) == (None, None, None)
    for name in ("gross_leverage", "net_leverage", "interest_cover"):
        assert f"fy-2024: {name} not computed: zero or negative denominator" in calc["gaps"]


def test_compute_never_raises(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    odd: list[dict[str, Any]] = [
        good | {"period": ["fy-2024"]},
        good | {"period": {"a": "b"}},
        good | {"field": ["revenue"]},
        good | {"field": {"a": 1}},
        good | {"source": ["x"]},
        good | {"quote": {"q": 1}},
        good | {"value": "9" * 40},
        good | {"value": "1E999999"},
    ]
    for row in odd:
        calc, findings = compute(block([row], ["fy-2024"]), pages.root)
        assert findings and calc["inputs"]["fy-2024"] == {}
    deep: Any = []
    for _ in range(1500):
        deep = [deep]
    calc, findings = compute(block([good]) | {"note": deep}, pages.root)
    assert [f for f in findings if f.startswith("nesting deeper than 20 levels at block.note")]
    nested: Any = "x"
    for _ in range(21):
        nested = {"k": nested}
    assert compute(block([good | {"deep": nested}]), pages.root)[1] == [
        "nesting deeper than 20 levels at block.rows[0].deep" + ".k" * 18
    ]
    shallow: Any = "x"
    for _ in range(15):
        shallow = {"k": shallow}
    assert compute(block([good | {"deep": shallow}], ["fy-2024"]), pages.root)[1] == []


def test_extreme_values_stay_in_decimal_context(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    big, tiny = "999999999999999", "0.000000000000001"
    rows = [pages.row("total_debt", "fy-2024", big), pages.row("ebitda", "fy-2024", tiny)]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    assert calc["ratios"]["fy-2024"]["gross_leverage"] == "999999999999999000000000000000.0000"


def test_quote_minimum_is_measured_after_normalise(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    page = "As of December 31, 2024, borrowings of $1,250 million were outstanding. Cash 16,261."
    for field, value, quote in (
        ("rcf_drawn", "0", "0" + " " * 11),
        ("cash_and_equivalents", "6261", "6,261" + " " * 7),
        ("rcf_drawn", "1250", "1,250" + "-\n" * 4),
    ):
        calc, findings = one(pages, field, value, page, quote)
        assert findings == [f"{field} fy-2024: quote must be a string of at least 12 characters"]
        assert calc["inputs"]["fy-2024"] == {}


def test_derived_repeated_citation_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Total debt 24,581 and current portion 487 at year end.")
    part = {
        "value": "24581",
        "source": SRC,
        "page": n,
        "quote": "Total debt 24,581 and current portion 487",
    }
    row = {
        "field": "total_debt",
        "period": "fy-2024",
        "value": "49162",
        "derived": [part, dict(part, quote=str(part["quote"]) + "  ")],
    }
    calc, findings = compute(block([row], ["fy-2024"]), pages.root)
    assert findings == ["total_debt fy-2024 component 1: repeats the citation of component 0"]
    assert calc["inputs"]["fy-2024"] == {}
    other = dict(part, value="487", quote="Total debt 24,581 and current portion 487 at")
    row["derived"] = [part, other]
    row["value"] = "25068"
    assert compute(block([row], ["fy-2024"]), pages.root)[1] == []


def test_conflicting_duplicates_drop_the_field(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    a = pages.row("adjusted_ebitda", "fy-2024", "4173")
    b = pages.row("adjusted_ebitda", "fy-2024", "4000")
    debt = pages.row("total_debt", "fy-2024", "25068")
    calc, findings = compute(block([a, b, debt], ["fy-2024"]), pages.root)
    assert findings == ["adjusted_ebitda fy-2024: duplicate row"]
    assert calc["inputs"]["fy-2024"] == {"total_debt": "25068"}
    assert calc["ratios"]["fy-2024"]["gross_leverage"] is None
    assert calc["gaps"][0] == "fy-2024: adjusted_ebitda dropped: conflicting duplicate rows"
    calc, findings = compute(block([a, a], ["fy-2024"]), pages.root)
    assert findings == ["adjusted_ebitda fy-2024: duplicate row"]
    assert calc["inputs"]["fy-2024"] == {"adjusted_ebitda": "4173"}
    assert not any("dropped" in g for g in calc["gaps"])
    bad = dict(b, value="9999")
    calc, _ = compute(block([a, bad], ["fy-2024"]), pages.root)
    assert "adjusted_ebitda" not in calc["inputs"]["fy-2024"]


def test_maturity_field_needs_ascii_digits(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for field in ("maturity_\uff12\uff10\uff12\uff17", "maturity_\u0662\u0660\u0662\u0667"):
        calc, findings = compute(block([pages.row(field, "fy-2024", "700")]), pages.root)
        assert findings == [f"rows[0]: unknown or missing field: {field!r}"]
        assert calc["inputs"]["fy-2024"] == {}


def test_row_with_a_finding_contributes_nothing(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("adjusted_ebitda", "fy-2024", "4173") | {"confidence": Decimal("0.9")}
    calc, findings = compute(block([row], ["fy-2024"]), pages.root)
    assert findings == ["number must be a string: block.rows[0].confidence = 0.9"]
    assert calc["inputs"]["fy-2024"] == {}
    assert calc["ratios"]["fy-2024"]["basis"] is None


def nest_dicts(layers: int) -> Any:
    node: Any = "x"
    for _ in range(layers):
        node = {"k": node}
    return node


def nest_lists(layers: int) -> Any:
    node: Any = "x"
    for _ in range(layers):
        node = [node]
    return node


def test_depth_bound_boundaries(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    assert compute(block([good | {"d": nest_dicts(18)}], ["fy-2024"]), pages.root)[1] == []
    assert compute(block([good | {"d": nest_dicts(19)}], ["fy-2024"]), pages.root)[1] == [
        "nesting deeper than 20 levels at block.rows[0].d" + ".k" * 18
    ]
    assert compute(block([nest_lists(19)], ["fy-2024"]), pages.root)[1] == [
        "rows[0] must be an object"
    ]
    assert compute(block([nest_lists(20)], ["fy-2024"]), pages.root)[1] == [
        "nesting deeper than 20 levels at block.rows[0]" + "[0]" * 19,
        "rows[0] must be an object",
    ]
    assert compute(block([good]) | {"d": nest_dicts(20)}, pages.root)[1] == []
    assert compute(block([good]) | {"d": nest_dicts(21)}, pages.root)[1] == [
        "nesting deeper than 20 levels at block.d" + ".k" * 20
    ]


def test_row_findings_accumulate(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    good = pages.row("revenue", "fy-2024", "10")
    out = compute(block([good | {"period": "zz"}, 5, "text", ["a"]], ["fy-2024"]), pages.root)[1]
    assert out == [
        "revenue zz: period is not in periods",
        "number must be a string: block.rows[1] = 5",
        "rows[1] must be an object",
        "rows[2] must be an object",
        "rows[3] must be an object",
    ]
    bad = good | {"value": "11"}
    calc, findings = compute(block([bad, good], ["fy-2024"]), pages.root)
    assert findings[-1] == "revenue fy-2024: duplicate row"
    assert len(findings) == 2
    assert calc["inputs"]["fy-2024"] == {}


def test_citation_repeat_needs_same_source_page_and_quote(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Total debt 24,581 and current portion 487 at year end.")
    q = "Total debt 24,581 and current portion 487"
    part = {"value": "24581", "source": SRC, "page": n, "quote": q}
    lab = "total_debt fy-2024"

    def run(second: dict[str, Any]) -> list[str]:
        row = {"field": "total_debt", "period": "fy-2024", "value": "49162"}
        return compute(block([row | {"derived": [part, second]}], ["fy-2024"]), pages.root)[1]

    assert run(dict(part, source="y.htm")) != []
    assert not any("repeats" in f for f in run(dict(part, source="y.htm")))
    assert not any("repeats" in f for f in run(dict(part, page=n + 1)))
    assert any("repeats" in f for f in run(dict(part)))
    five, six = dict(part, quote=5), dict(part, quote=6, value="487")
    row = {"field": "total_debt", "period": "fy-2024", "value": "1", "derived": [five, six]}
    out = compute(block([row], ["fy-2024"]), pages.root)[1]
    assert not any("repeats" in f for f in out) and out
    row["derived"] = [five, dict(five)]
    out = compute(block([row], ["fy-2024"]), pages.root)[1]
    assert f"{lab} component 1: repeats the citation of component 0" in out


def test_split_number_in_unnormalised_quote_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    page = "As of December 31, 2024, borrowings of $1,250 million were outstanding under the RCF."
    cash = "Cash and cash equivalents were $%s million at year end."
    cases = [
        ("rcf_drawn", "0", page, "borrowings of $1,25-\n0 million were outstanding"),
        ("cash_and_equivalents", "6261", cash % "16,261", "were $1-\n6,261 million at year end"),
        ("cash_and_equivalents", "261", cash % "6,261", "were $6\uff0c261 million at year end"),
    ]
    for field, value, text, quote in cases:
        calc, findings = one(pages, field, value, text, quote)
        assert findings == [
            f"{field} fy-2024: value {value!r} is not printed as one number in the quote"
        ]
        assert calc["inputs"]["fy-2024"] == {}
    calc, findings = one(
        pages, "rcf_drawn", "1250", page, "borrowings of $1,25-\n0 million were outstanding"
    )
    assert findings == [] and calc["inputs"]["fy-2024"] == {"rcf_drawn": "1250"}
    calc, findings = one(
        pages,
        "cash_and_equivalents",
        "6261",
        cash % "6,261",
        "were $6\uff0c261 million at year end",
    )
    assert findings == []
    neg = "Interest expense, net \uff081,286\uff09 for the year"
    assert one(pages, "interest_expense_net", "-1286", neg, neg)[1] == []


def test_nil_marker_needs_a_quote_without_other_numbers(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for quote in (
        "Note 7 - Debt: borrowings of $1,250 million were outstanding under the revolver",
        "the revolver \u2014 under which $1,250 million was drawn \u2014 remains in place",
        "Revolver borrowings outstanding \u2014 at December 31, 2024",
    ):
        text = f"Liquidity. {quote}."
        calc, findings = one(pages, "rcf_drawn", "0", text, quote)
        assert findings == [
            "rcf_drawn fy-2024: value '0' is not printed as one number in the quote"
        ]
        assert calc["inputs"]["fy-2024"] == {}
    quote = "Revolver borrowings outstanding \u2014 at year end"
    assert one(pages, "rcf_drawn", "0", quote, quote)[1] == []


def test_positive_ebitda_needs_an_unsigned_token(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for field in ("ebitda", "adjusted_ebitda"):
        for printed in ("(4,173)", "-4,173", "\u22124,173", "($4,173)"):
            text = f"Reported {field} figure {printed} for fiscal 2024"
            calc, findings = one(pages, field, "4173", text, text)
            assert findings == [
                f"{field} fy-2024: value '4173' is not printed as one number in the quote"
            ]
            assert calc["inputs"]["fy-2024"] == {}
            assert one(pages, field, "-4173", text, text)[1] == []
        text = f"Reported {field} figure 4,173 for fiscal 2024"
        assert one(pages, field, "4173", text, text)[1] == []
        assert one(pages, field, "-4173", text, text)[1]
    for field in ("interest_expense_net", "capex", "revenue"):
        text = f"Reported {field} figure (4,173) for fiscal 2024"
        assert one(pages, field, "4173", text, text)[1] == []


def test_zero_and_fractional_ebitda_in_parentheses_rejected(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for value, printed in (("0", "(0)"), ("0.5", "(0.5)")):
        text = f"Reported EBITDA figure {printed} for fiscal 2024"
        calc, findings = one(pages, "ebitda", value, text, text)
        assert findings and calc["inputs"]["fy-2024"] == {}


def test_ebitda_derived_components_are_unsigned_only(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Operating income (3,000) and add-backs 813 for the year")
    q = "Operating income (3,000) and add-backs 813"
    parts = [{"value": v, "source": SRC, "page": n, "quote": q} for v in ("3000", "813")]
    row = {"field": "ebitda", "period": "fy-2024", "value": "3813", "derived": parts}
    findings = compute(block([row], ["fy-2024"]), pages.root)[1]
    assert findings == [
        "ebitda fy-2024 component 0: value '3000' is not printed as one number in the quote"
    ]


def test_derived_repeat_is_keyed_on_source_page_and_value(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Total debt 24,581 and current portion 487 at year end.")
    a = {
        "value": "24581",
        "source": SRC,
        "page": n,
        "quote": "Total debt 24,581 and current portion",
    }
    b = dict(a, quote="debt 24,581 and current portion 487")
    lab = "total_debt fy-2024"

    def run(parts: list[dict[str, Any]], value: str) -> list[str]:
        row = {"field": "total_debt", "period": "fy-2024", "value": value, "derived": parts}
        return compute(block([row], ["fy-2024"]), pages.root)[1]

    assert run([a, b], "49162") == [f"{lab} component 1: repeats the citation of component 0"]
    other = dict(b, value="487", quote="current portion 487 at year end")
    assert run([a, other], "25068") == []


def rejected(field: str, value: str) -> list[str]:
    return [f"{field} fy-2024: value {value!r} is not printed as one number in the quote"]


def test_split_cell_parentheses_read_negative(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    html = b"<table><tr><td>Adjusted EBITDA</td><td>$</td><td>(4,173</td><td>)</td></tr></table>"
    page = convert_html(html).text
    quote = "Adjusted EBITDA $ (4,173 )"
    assert quote in page
    calc, findings = one(pages, "adjusted_ebitda", "4173", page, quote)
    assert findings == rejected("adjusted_ebitda", "4173") and calc["inputs"]["fy-2024"] == {}
    assert one(pages, "adjusted_ebitda", "-4173", page, quote)[1] == []
    text = "Adjusted EBITDA ( $ 4,173 ) for 2024"
    assert one(pages, "adjusted_ebitda", "-4173", text, text)[1] == []
    text = "Interest expense, net (1,286 ) for the year"
    assert one(pages, "interest_expense_net", "-1286", text, text)[1] == []


def test_truncated_quote_keeps_the_page_sign(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    page = "Adjusted EBITDA $ (4,173 ) and $ 3,395"
    for quote in ("Adjusted EBITDA $ (4,173", "4,173 ) and $ 3,395"):
        calc, findings = one(pages, "adjusted_ebitda", "4173", page, quote)
        assert findings == rejected("adjusted_ebitda", "4173"), quote
        assert calc["inputs"]["fy-2024"] == {}
        assert one(pages, "adjusted_ebitda", "-4173", page, quote)[1] == [], quote
    page = "(compared with Adjusted EBITDA of $3,395)"
    assert one(pages, "adjusted_ebitda", "3395", page, "Adjusted EBITDA of $3,395)")[1] == []


def test_quote_edge_cannot_cut_a_number(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    page = "Total debt 25,068 at December 31, 2024."
    for value, quote in (("25", "Total debt 25"), ("5068", "5,068 at December")):
        calc, findings = one(pages, "total_debt", value, page, quote)
        assert findings == rejected("total_debt", value), quote
        assert calc["inputs"]["fy-2024"] == {}
    assert one(pages, "total_debt", "25068", page, "Total debt 25,068")[1] == []


def test_number_run_is_read_whole(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    for value, text in (
        ("1", "Total debt 1,25 at year end"),
        ("234", "Total debt .1,234 at year end"),
        ("234", "Total debt \u0663,234 at year end"),
    ):
        calc, findings = one(pages, "total_debt", value, text, text)
        assert findings == rejected("total_debt", value), text
        assert calc["inputs"]["fy-2024"] == {}
    page = "Total debt 25,068\u00b9 at December 31, 2024."
    for value in ("25", "25068"):
        calc, findings = one(pages, "total_debt", value, page, page[:28])
        assert findings == rejected("total_debt", value), value
        assert calc["inputs"]["fy-2024"] == {}


def test_nil_dash_must_stand_alone_on_the_page(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    quote = "Revolver borrowings -"
    calc, findings = one(pages, "rcf_drawn", "0", "Revolver borrowings -302 at year end", quote)
    assert findings == rejected("rcf_drawn", "0") and calc["inputs"]["fy-2024"] == {}
    assert one(pages, "rcf_drawn", "0", "Revolver borrowings - at year end", quote)[1] == []


def test_derived_repeat_ignores_sign_and_source_case(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    n = put(pages, "Current portion of long-term debt (487) and term loan 24,581 at year end.")
    q = "Current portion of long-term debt (487)"
    pos = {"value": "487", "source": SRC, "page": n, "quote": q}
    loan = {"value": "24581", "source": SRC, "page": n, "quote": "and term loan 24,581 at year end"}
    parts = [loan, pos, pos | {"value": "-487"}]
    row = {"field": "total_debt", "period": "fy-2024", "value": "24581", "derived": parts}
    findings = compute(block([row], ["fy-2024"]), pages.root)[1]
    assert "total_debt fy-2024 component 2: repeats the citation of component 1" in findings
    n = put(pages, "Total debt 487 and more")
    a = {"value": "487", "source": "x.htm", "page": n, "quote": "Total debt 487 and more"}
    parts = [a, a | {"source": "X.HTM"}]
    row = {"field": "total_debt", "period": "fy-2024", "value": "974", "derived": parts}
    findings = compute(block([row], ["fy-2024"]), pages.root)[1]
    assert "total_debt fy-2024 component 1: repeats the citation of component 0" in findings


def test_figures_are_read_from_every_occurrence_and_wrap(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    q = "Cash flow ( 1,000"
    page = f"{q} more. {q} ) more. {q} more."
    assert one(pages, "ebitda", "-1000", page, q)[1] == []
    page = "Cash 5,068 first. Then total debt 25 at December"
    calc, findings = one(pages, "total_debt", "5068", page, "total debt 25 at December")
    assert findings == rejected("total_debt", "5068") and calc["inputs"]["fy-2024"] == {}
    page = "Total debt 25,0-\n68 at December 31, 2024."
    assert one(pages, "total_debt", "25068", page, "Total debt 25,068 at")[1] == []
    page = "The cash flow-\nbased EBITDA 4,173 for 2024"
    assert one(pages, "ebitda", "4173", page, "cash flow-based EBITDA 4,173 for")[1] == []


def test_non_dict_block_still_reports_numbers(tmp_path: Path) -> None:
    calc, findings = compute(Decimal("1.5"), tmp_path)
    assert findings == ["number must be a string: block = 1.5", "block must be a JSON object"]
    assert calc["currency"] is None and calc["unit"] is None


def test_tiny_amounts_print_plain(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [
        pages.row("cash_and_equivalents", "fy-2024", "0.0000001"),
        pages.row("rcf_commitment", "fy-2024", "0"),
        pages.row("rcf_drawn", "fy-2024", "0"),
    ]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    assert calc["inputs"]["fy-2024"]["cash_and_equivalents"] == "0.0000001"
    assert calc["ratios"]["fy-2024"]["liquidity"] == "0.0000001"


pos = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1e9"), places=2)
nonneg = st.decimals(min_value=Decimal("0"), max_value=Decimal("1e9"), places=2)


def _calc_from_values(debt: Decimal, cash: Decimal, ebitda: Decimal) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as d:
        pages = Pages(Path(d))
        rows = [
            pages.row("total_debt", "p", f"{debt:f}"),
            pages.row("cash_and_equivalents", "p", f"{cash:f}"),
            pages.row("ebitda", "p", f"{ebitda:f}"),
        ]
        calc, findings = compute(block(rows, ["p"]), pages.root)
    assert findings == []
    return calc


@given(debt=pos, cash=nonneg, ebitda=pos)
def test_property_net_not_above_gross(debt: Decimal, cash: Decimal, ebitda: Decimal) -> None:
    r = _calc_from_values(debt, cash, ebitda)["ratios"]["p"]
    assert Decimal(r["net_leverage"]) <= Decimal(r["gross_leverage"])


@given(debt=pos, cash=pos, ebitda=pos)
def test_property_gross_is_quantized_quotient(
    debt: Decimal, cash: Decimal, ebitda: Decimal
) -> None:
    ratios = _calc_from_values(debt, cash, ebitda)["ratios"]["p"]
    assert ratios["gross_leverage"] == quant(debt / ebitda)
