"""Accept or reject a model handoff: section order, cited pages, verbatim quotes."""

import decimal
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path

SECTIONS = ("Request", "Sources", "Findings", "Calculations", "Gaps", "Next module")
_QUOTE_CITED = re.compile(r"[“\"]([^”\"]{12,})[”\"]\s*\[([^\]]+?) p\.(\d+)\]")
_QUOTE_ANY = re.compile(r"[“\"]([^”\"]{12,})[”\"]")
_H2 = re.compile(r"^## (.+?)\s*$")
_FENCE = re.compile(r"^```\s*(\S*)\s*$")
_TRANSLATE = str.maketrans("“”‘’–—", "\"\"''--")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    return " ".join(text.replace("-\n", "").split())


def _lines(text: str) -> Iterator[tuple[str, bool, str]]:
    """Yield (line, in_fence, fence_info) so headings inside fences are ignored."""
    fence: str | None = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m and fence is None:
            fence = m.group(1)
            yield line, True, fence
        elif m:
            fence = None
            yield line, True, ""
        else:
            yield line, fence is not None, fence or ""


def _section_findings(text: str) -> list[str]:
    seen = [m.group(1) for line, fenced, _ in _lines(text) if not fenced and (m := _H2.match(line))]
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
        heading = None if fenced else _H2.match(line)
        if heading:
            section = heading.group(1)
        elif section == "Calculations" and fenced and not _FENCE.match(line):
            if info == "json":
                blocks[-1].append(line)
        elif section == "Calculations" and info == "json" and _FENCE.match(line):
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
    except ValueError as exc:
        return None, f"json block in ## Calculations does not parse: {exc}"


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


def check(text: str, source_text: Path, *, require_json: bool = False) -> list[str]:
    out = _section_findings(text)
    cache: dict[tuple[str, str], str | None] = {}
    for m in _QUOTE_CITED.finditer(text):
        quote, name, number = m.groups()
        key = (name, number)
        if key not in cache:
            cache[key] = _page(source_text, name, number)
        page = cache[key]
        where = f"{name} p.{number}"
        shown = quote[:60]
        if page is None:
            out.append(f'{_missing(source_text, name)} {where}: "{shown}"')
        elif normalise(quote) not in normalise(page):
            out.append(f'quote not found on {where}: "{shown}"')
    for m in _QUOTE_ANY.finditer(_QUOTE_CITED.sub(" ", text)):
        out.append(f'quote with no citation right after it: "{m.group(1)[:60]}"')
    if require_json and (err := _parse(text)[1]):
        out.append(err)
    return out
