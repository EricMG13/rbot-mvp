"""CP-1 calculator: verify every sourced input against its page, then compute the ratios.

Formulas follow final-workflow `src/deploy_vii/model/metrics.py` lines 88-100 at fda70c29
(`_METRICS`): leverage = debt / EBITDA, net leverage = (debt - cash) / EBITDA, coverage =
EBITDA / interest, each on the adjusted basis when available.
"""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from rbot.handoff import verify_quote

FIELDS = (
    "revenue",
    "ebitda",
    "adjusted_ebitda",
    "interest_expense_net",
    "cash_interest_paid",
    "cfo",
    "capex",
    "cash_and_equivalents",
    "rcf_commitment",
    "rcf_drawn",
    "total_debt",
    "senior_secured_debt",
)
_MATURITY = re.compile(r"maturity_\d{4}")
_STRIP = re.compile(r"[,\s]")
_MIN_QUOTE = 12
_FOUR_DP = Decimal("0.0001")
_RATIOS = ("gross_leverage", "net_leverage", "interest_cover", "liquidity")

type Row = dict[str, object]


def _known(field: object) -> bool:
    return isinstance(field, str) and (field in FIELDS or bool(_MATURITY.fullmatch(field)))


def _numbers(node: object, path: str = "block") -> list[str]:
    """Findings for every non-string number (float, Decimal, int) anywhere in the block."""
    if isinstance(node, dict):
        return [f for k, v in node.items() for f in _numbers(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [f for i, v in enumerate(node) for f in _numbers(v, f"{path}[{i}]")]
    is_int = isinstance(node, int) and not isinstance(node, bool)
    if (is_int or isinstance(node, (float, Decimal))) and not path.endswith(".page"):
        return [f"number must be a string: {path} = {node}"]
    return []


def _decimal(raw: object, label: str) -> Decimal | str:
    """The parsed value, or the finding text when `raw` is not a finite numeric string."""
    if not isinstance(raw, str):
        return f"{label}: value must be a string"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return f"{label}: value is not a number: {raw!r}"
    return value if value.is_finite() else f"{label}: value is not finite: {raw!r}"


def _citation_finding(item: Row, source_text: Path) -> str | None:
    source, page, quote = item.get("source"), item.get("page"), item.get("quote")
    if not isinstance(source, str) or not source:
        return "source must be a non-empty string"
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return "page must be an integer >= 1"
    if not isinstance(quote, str) or len(quote) < _MIN_QUOTE:
        return f"quote must be a string of at least {_MIN_QUOTE} characters"
    if finding := verify_quote(quote, source, page, source_text):
        return finding
    digits = _STRIP.sub("", str(item["value"])).removeprefix("-")
    if digits not in _STRIP.sub("", quote):
        return f"digits {digits!r} not found in quote"
    return None


def _cited(item: Row, label: str, source_text: Path) -> tuple[Decimal | None, list[str]]:
    """Value of one sourced item, or None with findings."""
    value = _decimal(item.get("value"), label)
    if isinstance(value, str):
        return None, [value]
    if finding := _citation_finding(item, source_text):
        return None, [f"{label}: {finding}"]
    return value, []


def _derived(
    derived: object, row: Row, label: str, source_text: Path
) -> tuple[Decimal | None, list[str]]:
    if not isinstance(derived, list) or len(derived) < 2:
        return None, [f"{label}: derived must list at least two components"]
    values: list[Decimal] = []
    findings: list[str] = []
    for i, part in enumerate(derived):
        if not isinstance(part, dict):
            findings.append(f"{label} component {i}: must be an object")
            continue
        value, found = _cited(part, f"{label} component {i}", source_text)
        findings += found
        if value is not None:
            values.append(value)
    stated = _decimal(row.get("value"), label)
    if isinstance(stated, str):
        return None, findings + [stated]
    if not findings and stated != sum(values):
        findings.append(f"{label}: value {stated} is not the sum of its components")
    return (None if findings else stated), findings


def _row_value(row: Row, label: str, source_text: Path) -> tuple[Decimal | None, list[str]]:
    derived = row.get("derived")
    if derived is None:
        return _cited(row, label, source_text)
    if "source" in row or "quote" in row or "page" in row:
        return None, [f"{label}: row is both sourced and derived"]
    return _derived(derived, row, label, source_text)


def _inputs(
    block: dict[str, object], periods: list[str], source_text: Path
) -> tuple[dict[str, dict[str, Decimal]], list[str]]:
    rows = block.get("rows")
    if not isinstance(rows, list):
        return {}, ["block.rows must be a list"]
    inputs: dict[str, dict[str, Decimal]] = {p: {} for p in periods}
    findings: list[str] = []
    seen: set[tuple[object, object]] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            findings.append(f"rows[{i}] must be an object")
            continue
        field, period = row.get("field"), row.get("period")
        label = f"{field} {period}"
        if not _known(field):
            findings.append(f"rows[{i}]: unknown or missing field: {field!r}")
        elif period not in inputs:
            findings.append(f"{label}: period is not in periods")
        elif (field, period) in seen:
            findings.append(f"{label}: duplicate row")
        else:
            seen.add((field, period))
            value, found = _row_value(row, label, source_text)
            findings += found
            if value is not None:
                inputs[str(period)][str(field)] = value
    return inputs, findings


def _ratio(num: Decimal, den: Decimal) -> str:
    return str((num / den).quantize(_FOUR_DP, rounding=ROUND_HALF_EVEN))


def _liquidity(
    cash: Decimal | None, rcf: Decimal | None, drawn: Decimal | None, period: str, gaps: list[str]
) -> str | None:
    if cash is None or rcf is None or drawn is None:
        named = (("cash_and_equivalents", cash), ("rcf_commitment", rcf), ("rcf_drawn", drawn))
        missing = ", ".join(n for n, v in named if v is None)
        gaps.append(f"{period}: liquidity not computed: missing {missing}")
        return None
    return format(cash + rcf - drawn, "f")


def _period_ratios(have: dict[str, Decimal], period: str, gaps: list[str]) -> dict[str, str | None]:
    basis = next((f for f in ("adjusted_ebitda", "ebitda") if f in have), None)
    out: dict[str, str | None] = dict.fromkeys(_RATIOS)
    out["basis"] = basis
    operands: dict[str, Decimal | None] = {
        "total_debt": have.get("total_debt"),
        "cash_and_equivalents": have.get("cash_and_equivalents"),
        "adjusted_ebitda or ebitda": have[basis] if basis else None,
        "interest_expense_net": have.get("interest_expense_net"),
    }
    debt, cash, base, interest = operands.values()
    # name -> (operands needed, calculation); the zero check applies to the denominator
    plans = {
        "gross_leverage": (("total_debt", "adjusted_ebitda or ebitda"), debt, base),
        "net_leverage": (
            ("total_debt", "cash_and_equivalents", "adjusted_ebitda or ebitda"),
            None if debt is None or cash is None else debt - cash,
            base,
        ),
        "interest_cover": (("adjusted_ebitda or ebitda", "interest_expense_net"), base, interest),
    }
    for name, (needed, num, den) in plans.items():
        missing = [n for n in needed if operands[n] is None]
        if missing:
            gaps.append(f"{period}: {name} not computed: missing {', '.join(missing)}")
        elif den == 0:
            gaps.append(f"{period}: {name} not computed: zero denominator")
        elif num is not None and den is not None:
            out[name] = _ratio(num, den)
    rcf, drawn = have.get("rcf_commitment"), have.get("rcf_drawn")
    out["liquidity"] = _liquidity(cash, rcf, drawn, period, gaps)
    return out


def _empty(findings: list[str]) -> tuple[dict[str, object], list[str]]:
    calc: dict[str, object] = {"periods": [], "inputs": {}, "ratios": {}, "gaps": []}
    return calc, findings


def compute(block: object, source_text: Path) -> tuple[dict[str, object], list[str]]:
    """The `calculation.json` content for a CP-1 block, and the findings against it."""
    findings = _numbers(block)
    if not isinstance(block, dict):
        return _empty(findings + ["block must be a JSON object"])
    periods = block.get("periods")
    if (
        not isinstance(periods, list)
        or not periods
        or not all(isinstance(p, str) and p for p in periods)
    ):
        return _empty(findings + ["block.periods must be a non-empty list of strings"])
    for key in ("currency", "unit"):
        if not isinstance(block.get(key), str):
            findings.append(f"block.{key} must be a string")
    inputs, more = _inputs(block, periods, source_text)
    gaps: list[str] = []
    ratios = {p: _period_ratios(inputs[p], p, gaps) for p in periods} if inputs else {}
    calc: dict[str, object] = {
        "currency": block.get("currency"),
        "unit": block.get("unit"),
        "periods": periods,
        "inputs": {p: {f: format(v, "f") for f, v in have.items()} for p, have in inputs.items()},
        "ratios": ratios,
        "gaps": gaps,
    }
    return calc, findings + more
