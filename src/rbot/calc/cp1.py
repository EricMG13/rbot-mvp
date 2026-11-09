"""CP-1 calculator: verify every sourced input against its page, then compute the ratios.

Formulas follow final-workflow `src/deploy_vii/model/metrics.py` lines 88-100 at fda70c29
(`_METRICS`): leverage = debt / EBITDA, net leverage = (debt - cash) / EBITDA, each on the
adjusted basis when available. Interest cover departs from FW: the RBOT brief divides EBITDA by
`interest_expense_net`, while FW `_METRICS` uses `cash_interest` with the sign flipped. A
denominator of zero or below gives null plus a gap, as FW metrics.py:163-174 does.
`compute` never raises: every malformed input becomes a finding.
"""

import re
from bisect import bisect_right
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import NamedTuple

from rbot.handoff import normalise, page_text, verify_quote

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
_MATURITY = re.compile(r"maturity_[0-9]{4}")
_BALANCE = frozenset(
    ("total_debt", "senior_secured_debt", "cash_and_equivalents", "rcf_commitment", "rcf_drawn")
)
# At most 15 integer and 15 fraction digits (and 15 significant), so a ratio of two values stays
# below 1e30 and its 4 dp quantize fits the 38-digit context.
_VALUE = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,15})?")
_MAX_SIGNIFICANT = 15
_PRECISION = 38
_MAX_DEPTH = 20
_FIGURE = re.compile(
    r"(?P<nil>(?<!\S)[-−](?!\S))"
    r"|(?P<open>\(\s*\$?\s*)?"
    r"(?P<minus>(?<![0-9A-Za-z])[-−]\$?)?"
    r"(?<![\d.,])(?P<run>\d+(?:[.,]\d+)*)"
    r"(?P<close>\s*\))?"
)
_WELL_FORMED = re.compile(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?")
_DIGIT = re.compile(r"\d")
_EBITDA = frozenset(("ebitda", "adjusted_ebitda"))
_MIN_QUOTE = 12
_FOUR_DP = Decimal("0.0001")
_RATIOS = ("gross_leverage", "net_leverage", "interest_cover", "liquidity")

type Row = dict[str, object]


def _known(field: object) -> bool:
    return isinstance(field, str) and (field in FIELDS or bool(_MATURITY.fullmatch(field)))


def _nonnegative(field: str) -> bool:
    return field in _BALANCE or bool(_MATURITY.fullmatch(field))


def _numbers(node: object, path: str = "block", depth: int = 0) -> list[str]:
    """Findings for every non-string number anywhere in the block, to a bounded depth."""
    if isinstance(node, dict | list):
        return _container_numbers(node, path, depth)
    is_int = isinstance(node, int) and not isinstance(node, bool)
    if (is_int or isinstance(node, float | Decimal)) and not path.endswith(".page"):
        return [f"number must be a string: {path} = {node}"]
    return []


def _container_numbers(
    node: dict[object, object] | list[object], path: str, depth: int
) -> list[str]:
    if depth > _MAX_DEPTH:
        return [f"nesting deeper than {_MAX_DEPTH} levels at {path}"]
    if isinstance(node, dict):
        return [f for k, v in node.items() for f in _numbers(v, f"{path}.{k}", depth + 1)]
    return [f for i, v in enumerate(node) for f in _numbers(v, f"{path}[{i}]", depth + 1)]


def _decimal(raw: object, label: str) -> Decimal | str:
    """The parsed value, or the finding text when `raw` is not a plain ASCII decimal string."""
    if not isinstance(raw, str):
        return f"{label}: value must be a string"
    text = raw.strip()
    significant = len(text.removeprefix("-").replace(".", "").lstrip("0"))
    if not _VALUE.fullmatch(text) or significant > _MAX_SIGNIFICANT:
        return f"{label}: value is not a plain decimal of at most 15 significant digits: {raw!r}"
    return Decimal(text)


class _Figure(NamedTuple):
    digits: str
    negative: bool
    bare: bool


def _occurrences(wanted: str, text: str) -> list[int]:
    """Start offsets of every (overlapping) occurrence of `wanted` in `text`."""
    starts: list[int] = []
    at = text.find(wanted)
    while at != -1:
        starts.append(at)
        at = text.find(wanted, at + 1)
    return starts


def _inside(starts: list[int], span: tuple[int, int], length: int) -> bool:
    """Whether `span` lies wholly within one occurrence (given by its start) of the quote."""
    s, e = span
    i = bisect_right(starts, s) - 1
    return i >= 0 and e <= starts[i] + length


def _figure(m: re.Match[str]) -> _Figure | None:
    """The figure a run prints, or None when the run is not a well-formed number."""
    run = m.group("run")
    if not _WELL_FORMED.fullmatch(run):
        return None
    minus = m.group("minus") is not None
    opened = m.group("open") is not None
    negative = minus or (opened and m.group("close") is not None)
    return _Figure(run.replace(",", ""), negative, not (minus or opened))


def _figures(quote: str, page: str) -> tuple[list[_Figure], bool]:
    """Figures the page prints inside the quote's occurrences, and whether a nil dash does.

    The nil flag is true only when the quote itself holds no digit at all.
    """
    wanted = normalise(quote)
    found: list[_Figure] = []
    nil = False
    for wrap in ("", "-"):
        text = normalise(page, wrap)
        starts = _occurrences(wanted, text)
        if not starts:
            continue
        for m in _FIGURE.finditer(text):
            if m.group("nil") is not None:
                nil = nil or _inside(starts, m.span("nil"), len(wanted))
            elif _inside(starts, m.span("run"), len(wanted)) and (fig := _figure(m)):
                found.append(fig)
    return found, nil and not _DIGIT.search(wanted)


def _supported(
    value: Decimal, text: str, figures: list[_Figure], nil: bool, *, unsigned_only: bool
) -> bool:
    """Whether the page prints `value` as one whole number, with its sign.

    A negative value needs a figure printed negative. A positive value may match a parenthesised
    figure, except where `unsigned_only` (EBITDA: parentheses there mean a loss). A zero may
    also match a standalone nil dash when the quote has no digit.
    """
    digits = text.removeprefix("-")
    for f in figures:
        if f.digits == digits and (f.negative if value < 0 else f.bare or not unsigned_only):
            return True
    return digits == "0" and nil


def _citation_finding(
    item: Row, value: Decimal, source_text: Path, unsigned_only: bool
) -> str | None:
    source, page, quote = item.get("source"), item.get("page"), item.get("quote")
    if not isinstance(source, str) or not source:
        return "source must be a non-empty string"
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return "page must be an integer >= 1"
    if not isinstance(quote, str) or len(normalise(quote)) < _MIN_QUOTE:
        return f"quote must be a string of at least {_MIN_QUOTE} characters"
    if finding := verify_quote(quote, source, page, source_text):
        return finding
    text = str(item["value"]).strip()
    # The page exists because verify_quote passed; an empty text would yield no figures (closed).
    figures, nil = _figures(quote, page_text(source, page, source_text) or "")
    if not _supported(value, text, figures, nil, unsigned_only=unsigned_only):
        return f"value {text!r} is not printed as one number in the quote"
    return None


def _cited(
    item: Row, label: str, source_text: Path, unsigned_only: bool
) -> tuple[Decimal | None, list[str]]:
    """Value of one sourced item, or None with findings."""
    value = _decimal(item.get("value"), label)
    if isinstance(value, str):
        return None, [value]
    if finding := _citation_finding(item, value, source_text, unsigned_only):
        return None, [f"{label}: {finding}"]
    return value, []


def _citation_key(part: Row) -> tuple[str, str, str]:
    """Same source (any case), page and unsigned value: the same printed figure."""
    source = part.get("source")
    named = source.casefold() if isinstance(source, str) else source
    return repr(named), repr(part.get("page")), str(part.get("value")).strip().removeprefix("-")


def _derived(
    derived: object, row: Row, label: str, source_text: Path, unsigned_only: bool
) -> tuple[Decimal | None, list[str]]:
    if not isinstance(derived, list) or len(derived) < 2:
        return None, [f"{label}: derived must list at least two components"]
    values: list[Decimal] = []
    findings: list[str] = []
    cited: dict[tuple[str, str, str], int] = {}
    for i, part in enumerate(derived):
        if not isinstance(part, dict):
            findings.append(f"{label} component {i}: must be an object")
            continue
        first = cited.setdefault(_citation_key(part), i)
        if first != i:
            findings.append(f"{label} component {i}: repeats the citation of component {first}")
        value, found = _cited(part, f"{label} component {i}", source_text, unsigned_only)
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
    field = row.get("field")
    unsigned_only = isinstance(field, str) and field in _EBITDA
    derived = row.get("derived")
    if derived is None:
        return _cited(row, label, source_text, unsigned_only)
    if "source" in row or "quote" in row or "page" in row:
        return None, [f"{label}: row is both sourced and derived"]
    return _derived(derived, row, label, source_text, unsigned_only)


def _row(row: Row, index: int, source_text: Path) -> tuple[Decimal | None, list[str]]:
    """The verified value of one row, or None with every finding against the row."""
    field, period = row.get("field"), row.get("period")
    label = f"{field} {period}"
    findings = _numbers(row, f"block.rows[{index}]", 2)
    value, found = _row_value(row, label, source_text)
    findings += found
    if value is not None and value < 0 and _nonnegative(str(field)):
        findings.append(f"{label}: {field} must not be negative: {value}")
    return (None if findings else value), findings


def _store(
    inputs: dict[str, dict[str, Decimal]],
    seen: set[tuple[str, str]],
    key: tuple[str, str],
    value: Decimal | None,
) -> list[str]:
    """Record a verified value; a duplicate row that disagrees drops the field. Returns gaps."""
    field, period = key
    gaps: list[str] = []
    if key in seen:
        if inputs[period].get(field) != value:
            inputs[period].pop(field, None)
            gaps.append(f"{period}: {field} dropped: conflicting duplicate rows")
    elif value is not None:
        inputs[period][field] = value
    seen.add(key)
    return gaps


def _inputs(
    block: dict[str, object], periods: list[str], source_text: Path
) -> tuple[dict[str, dict[str, Decimal]], list[str], list[str]]:
    rows = block.get("rows")
    inputs: dict[str, dict[str, Decimal]] = {p: {} for p in periods}
    findings: list[str] = []
    gaps: list[str] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(rows, list):
        return inputs, ["block.rows must be a list"], gaps
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            findings += _numbers(row, f"block.rows[{i}]", 2)
            findings.append(f"rows[{i}] must be an object")
            continue
        field, period = row.get("field"), row.get("period")
        label = f"{field} {period}"
        if not isinstance(field, str) or not _known(field):
            findings.append(f"rows[{i}]: unknown or missing field: {field!r}")
        elif not isinstance(period, str) or period not in inputs:
            findings.append(f"{label}: period is not in periods")
        else:
            value, found = _row(row, i, source_text)
            findings += found
            if (field, period) in seen:
                findings.append(f"{label}: duplicate row")
            gaps += _store(inputs, seen, (field, period), value)
    return inputs, findings, gaps


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
        elif den is not None and den <= 0:
            gaps.append(f"{period}: {name} not computed: zero or negative denominator")
        elif num is not None and den is not None:
            out[name] = _ratio(num, den)
    rcf, drawn = have.get("rcf_commitment"), have.get("rcf_drawn")
    out["liquidity"] = _liquidity(cash, rcf, drawn, period, gaps)
    return out


def _empty(findings: list[str], block: object) -> tuple[dict[str, object], list[str]]:
    keys = block if isinstance(block, dict) else {}
    calc: dict[str, object] = {
        "currency": keys.get("currency"),
        "unit": keys.get("unit"),
        "periods": [],
        "inputs": {},
        "ratios": {},
        "gaps": [],
    }
    return calc, findings


def compute(block: object, source_text: Path) -> tuple[dict[str, object], list[str]]:
    """The `calculation.json` content for a CP-1 block, and the findings against it."""
    if not isinstance(block, dict):
        return _empty(_numbers(block) + ["block must be a JSON object"], block)
    rows_ok = isinstance(block.get("rows"), list)
    findings = _numbers({k: v for k, v in block.items() if k != "rows" or not rows_ok})
    periods = block.get("periods")
    if (
        not isinstance(periods, list)
        or not periods
        or not all(isinstance(p, str) and p for p in periods)
    ):
        return _empty(findings + ["block.periods must be a non-empty list of strings"], block)
    for key in ("currency", "unit"):
        if not isinstance(block.get(key), str):
            findings.append(f"block.{key} must be a string")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        inputs, more, gaps = _inputs(block, periods, source_text)
        ratios = {p: _period_ratios(inputs[p], p, gaps) for p in periods}
    calc: dict[str, object] = {
        "currency": block.get("currency"),
        "unit": block.get("unit"),
        "periods": periods,
        "inputs": {p: {f: format(v, "f") for f, v in have.items()} for p, have in inputs.items()},
        "ratios": ratios,
        "gaps": gaps,
    }
    return calc, findings + more
