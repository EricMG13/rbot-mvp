"""Accept or reject a model handoff: section order, cited pages, verbatim quotes."""

import decimal
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path

SECTIONS = ("Request", "Sources", "Findings", "Calculations", "Gaps", "Next module")
_QUOTE = re.compile(r"\"([^\"]*)\"|“([^”]*)”")
_CITATION = re.compile(r"\s*\[([^\]]+?) p\.(\d+)\]")
H2 = re.compile(r"^## (.+?)\s*$")
FENCE = re.compile(r"^```\s*(\S*)\s*$")
_TRANSLATE = str.maketrans("“”‘’–—", "\"\"''--")
_SPACED_DASH_WRAP = re.compile(r"(?<=\s)[–—]\s*\n\s*")
_DASH_WRAP = re.compile(r"[–—]\s*\n\s*")
_MARK = re.compile(r"[\"“”]")
_MARK_CITED = re.compile(r"[\"”]\s*\[([^\]]+?) p\.(\d+)\]")
_PARAGRAPHS = re.compile(r"\n\s*\n")


def normalise(text: str, hyphen_wrap: str = "") -> str:
    """Canonical form; `hyphen_wrap` replaces an ASCII `-\\n` (removed, or kept as `-`)."""
    text = unicodedata.normalize("NFKC", text).replace("-\n", hyphen_wrap)
    text = _SPACED_DASH_WRAP.sub("- ", text)
    text = _DASH_WRAP.sub("-", text).translate(_TRANSLATE)
    return " ".join(text.split())


def split_lines(text: str) -> list[str]:
    """The line splitter the checker uses; the renderer must split the same way."""
    return text.splitlines()


def _lines(text: str) -> Iterator[tuple[str, bool, str]]:
    """Yield (line, in_fence, fence_info) so headings inside fences are ignored."""
    fence: str | None = None
    for line in split_lines(text):
        m = FENCE.match(line)
        if m and fence is None:
            fence = m.group(1)
            yield line, True, fence
        elif m:
            fence = None
            yield line, True, ""
        else:
            yield line, fence is not None, fence or ""


def _section_findings(text: str) -> list[str]:
    seen = [m.group(1) for line, fenced, _ in _lines(text) if not fenced and (m := H2.match(line))]
    found = [h for h in seen if h in SECTIONS]
    out = [f"missing section: ## {s}" for s in SECTIONS if s not in found]
    out += [f"duplicate section: ## {s}" for s in SECTIONS if found.count(s) > 1]
    if not out and found != list(SECTIONS):
        out.append("sections out of order; required: " + ", ".join(f"## {s}" for s in SECTIONS))
    return out


def _json_blocks(text: str) -> list[str]:
    """Bodies of fenced blocks with info string `json` inside ## Calculations."""
    section = ""
    blocks: list[list[str]] = []
    for line, fenced, info in _lines(text):
        heading = None if fenced else H2.match(line)
        if heading:
            section = heading.group(1)
        elif section == "Calculations" and fenced and not FENCE.match(line):
            if info == "json":
                blocks[-1].append(line)
        elif section == "Calculations" and info == "json" and FENCE.match(line):
            blocks.append([])
    return ["\n".join(b) for b in blocks]


def _refuse(constant: str) -> object:
    raise ValueError(f"{constant} is not allowed")


def _parse(text: str) -> tuple[object | None, str | None]:
    blocks = _json_blocks(text)
    if len(blocks) != 1:
        return None, f"expected exactly one json block in ## Calculations, found {len(blocks)}"
    try:
        return json.loads(blocks[0], parse_float=decimal.Decimal, parse_constant=_refuse), None
    except (ValueError, RecursionError) as exc:
        return (
            None,
            f"json block in ## Calculations does not parse: {exc.__class__.__name__}: {exc}",
        )


def json_block(text: str) -> object | None:
    return _parse(text)[0]


def _page(source_text: Path, name: str, number: str) -> str | None:
    if not name or name in (".", "..") or any(c in name for c in "/\\\0"):
        return None
    root = source_text.resolve()
    path = (root / name / f"p{int(number[:9]):04d}.txt").resolve()
    if len(number) > 9 or root not in path.parents or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _missing(source_text: Path, name: str) -> str:
    safe = name not in ("", ".", "..") and not any(c in name for c in "/\\\0")
    return "page not found on" if safe and (source_text / name).is_dir() else "unknown source"


def _prose(text: str) -> str:
    """The text outside fenced code, where quotations are looked for."""
    return "\n".join(line for line, fenced, _ in _lines(text) if not fenced)


def _quote_finding(
    quote: str,
    cite: re.Match[str] | None,
    source_text: Path,
    cache: dict[tuple[str, str], str | None],
) -> str | None:
    shown = quote[:60]
    if not cite:
        return f'quote with no citation right after it: "{shown}"'
    name, number = cite.groups()
    where = f"{name} p.{number}"
    if (name, number) not in cache:
        cache[name, number] = _page(source_text, name, number)
    page = cache[name, number]
    if page is None:
        return f'{_missing(source_text, name)} {where}: "{shown}"'
    wanted = normalise(quote)
    if not wanted:
        return f'empty quote on {where}: "{shown}"'
    if not any(wanted in normalise(page, wrap) for wrap in ("", "-")):
        return f'quote not found on {where}: "{shown}"'
    return None


def _paragraph_findings(
    para: str, source_text: Path, cache: dict[tuple[str, str], str | None]
) -> list[str]:
    """Fail closed: an unpaired quote mark or an unverified cited mark is a finding."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    verified: set[int] = set()
    for m in _QUOTE.finditer(para):
        spans.append(m.span())
        quote = m.group(1) if m.group(1) is not None else m.group(2)
        if len(quote) >= 12:
            verified.add(m.end())
            finding = _quote_finding(quote, _CITATION.match(para, m.end()), source_text, cache)
            out += [finding] if finding else []
    for mark in _MARK.finditer(para):
        if not any(a <= mark.start() < b for a, b in spans):
            out.append(f'unclosed quote: "{para[mark.start() + 1 :][:60]}"')
    for cm in _MARK_CITED.finditer(para):
        if cm.start() + 1 not in verified:
            out.append(f"cited quote could not be paired before [{cm.group(1)} p.{cm.group(2)}]")
    return out


_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _findings_units(text: str) -> list[str]:
    """Each list item and paragraph of ## Findings, outside fences, split as the renderer does."""
    units: list[list[str]] = []
    current: list[str] | None = None
    section = ""
    for line, fenced, _ in _lines(text):
        if fenced:
            continue
        if heading := H2.match(line):
            section, current = heading.group(1), None
        elif section != "Findings":
            continue
        elif not line.strip() or line.lstrip().startswith("#"):
            current = None
        elif current is None or _ITEM.match(line):
            current = [line.strip()]
            units.append(current)
        else:
            current.append(line.strip())
    return ["\n".join(u) for u in units]


def _unquoted_findings(text: str) -> list[str]:
    out = []
    for unit in _findings_units(text):
        if not any(len(m.group(1) or m.group(2)) >= 12 for m in _QUOTE.finditer(unit)):
            out.append(f'Findings item with no verified quote: "{unit[:60]}"')
    return out


def check(
    text: str, source_text: Path, *, require_json: bool = False, quoted_findings: bool = False
) -> list[str]:
    out = _section_findings(text)
    if quoted_findings:
        out += _unquoted_findings(text)
    cache: dict[tuple[str, str], str | None] = {}
    for para in _PARAGRAPHS.split(_prose(text)):
        out += _paragraph_findings(para, source_text, cache)
    if require_json and (err := _parse(text)[1]):
        out.append(err)
    return out
