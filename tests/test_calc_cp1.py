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
    assert len(findings) == 1 and "digits" in findings[0]


def test_digits_allow_parenthesised_negative(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    row = pages.row("cfo", "fy-2024", "302")
    row["value"] = "-302"
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
    assert "fy-2024: gross_leverage not computed: zero denominator" in calc["gaps"]
    assert "fy-2024: net_leverage not computed: zero denominator" in calc["gaps"]
    assert "fy-2024: interest_cover not computed: zero denominator" in calc["gaps"]


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
    assert _findings([good | {"value": "11"}], pages) == [f"{lab}: digits '11' not found in quote"]
    assert _findings([good | {"value": 5}], pages) == [
        "number must be a string: block.rows[0].value = 5",
        f"{lab}: value must be a string",
    ]
    assert _findings([good | {"value": "ten"}], pages) == [f"{lab}: value is not a number: 'ten'"]
    assert _findings([good | {"value": "NaN"}], pages) == [f"{lab}: value is not finite: 'NaN'"]
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
    (pages.root / SRC / "p0001.txt").write_text("Revenue is 10 m.", encoding="utf-8", newline="\n")
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
        f"{lab} component 1: digits '11' not found in quote",
        f"{lab} component 2: digits '21' not found in quote",
    ]
    assert _findings([row | {"value": "31"}], pages) == [
        f"{lab}: value 31 is not the sum of its components"
    ]
    assert _findings([row | {"value": "x"}], pages) == [f"{lab}: value is not a number: 'x'"]
    bad_both = [parts[0] | {"value": "11"}, parts[1]]
    assert _findings([row | {"derived": bad_both, "value": "x"}], pages) == [
        f"{lab} component 0: digits '11' not found in quote",
        f"{lab}: value is not a number: 'x'",
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
    empty: dict[str, Any] = {"periods": [], "inputs": {}, "ratios": {}, "gaps": []}
    assert compute(None, pages.root) == (empty, ["block must be a JSON object"])
    for periods in ([], [5], [""], "fy"):
        calc, findings = compute(block([]) | {"periods": periods}, pages.root)
        assert calc == empty
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


def test_decimal_text_is_plain_not_exponent(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    rows = [
        pages.row("cash_and_equivalents", "fy-2024", "1E3", shown="1E3"),
        pages.row("rcf_commitment", "fy-2024", "1E3", shown="1E3"),
        pages.row("rcf_drawn", "fy-2024", "1E3", shown="1E3"),
    ]
    calc, findings = compute(block(rows, ["fy-2024"]), pages.root)
    assert findings == []
    assert calc["inputs"]["fy-2024"]["cash_and_equivalents"] == "1000"
    assert calc["ratios"]["fy-2024"]["liquidity"] == "1000"


def test_liquidity_gap_names_each_missing_operand(tmp_path: Path) -> None:
    pages = Pages(tmp_path)
    full = ["cash_and_equivalents", "rcf_commitment", "rcf_drawn"]
    for gone in full:
        rows = [pages.row(f, "fy-2024", "5") for f in full if f != gone]
        calc, _ = compute(block(rows, ["fy-2024"]), pages.root)
        assert calc["ratios"]["fy-2024"]["liquidity"] is None
        assert f"fy-2024: liquidity not computed: missing {gone}" in calc["gaps"]


pos = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1e9"), places=2)


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


@given(debt=pos, cash=pos, ebitda=pos)
def test_property_net_not_above_gross(debt: Decimal, cash: Decimal, ebitda: Decimal) -> None:
    r = _calc_from_values(debt, cash, ebitda)["ratios"]["p"]
    assert Decimal(r["net_leverage"]) <= Decimal(r["gross_leverage"])


@given(debt=pos, cash=pos, ebitda=pos)
def test_property_gross_is_quantized_quotient(
    debt: Decimal, cash: Decimal, ebitda: Decimal
) -> None:
    ratios = _calc_from_values(debt, cash, ebitda)["ratios"]["p"]
    assert ratios["gross_leverage"] == quant(debt / ebitda)
