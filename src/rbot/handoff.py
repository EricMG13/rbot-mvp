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
_H2 = re.compile(r"^## (.+?)\s*$")
_FENCE = re.compile(r"^```\s*(\S*)\s*$")
_TRANSLATE = str.maketrans("“”‘’–—", "\"\"''--")
_DASH_WRAP = re.compile(r"[–—]\s*\n\s*")


def normalise(text: str, hyphen_wrap: str = "") -> str:
    """Canonical form; `hyphen_wrap` replaces an ASCII `-\\n` (removed, or kept as `-`)."""
    text = unicodedata.normalize("NFKC", text).replace("-\n", hyphen_wrap)
    text = _DASH_WRAP.sub("-", text).translate(_TRANSLATE)
    return " ".join(text.split())


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
    m: re.Match[str], prose: str, source_text: Path, cache: dict[tuple[str, str], str | None]
) -> str | None:
    quote = m.group(1) if m.group(1) is not None else m.group(2)
    if len(quote) < 12:
        return None
    shown = quote[:60]
    cite = _CITATION.match(prose, m.end())
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


def check(text: str, source_text: Path, *, require_json: bool = False) -> list[str]:
    out = _section_findings(text)
    prose = _prose(text)
    cache: dict[tuple[str, str], str | None] = {}
    found = (_quote_finding(m, prose, source_text, cache) for m in _QUOTE.finditer(prose))
    out += [f for f in found if f]
    if require_json and (err := _parse(text)[1]):
        out.append(err)
    return out
