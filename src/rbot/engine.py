"""Run one case: extract sources, run each module with validated attempts, render the report."""

import hashlib
import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rbot.copilot import CallResult, SandboxViolation, call
from rbot.handoff import check
from rbot.pathways import IMPLEMENTED, PATHWAYS, PROMPTS_DIR
from rbot.render import render
from rbot.sources import SourceError, SourceText, write_source_text

ATTEMPTS = 3
REQUIRED = (("run", "pathway"), ("run", "as_of"), ("identity", "issuer_name"))
VIOLATION = (
    "the run tried a tool outside the read-only set or outside the case folder: {}; "
    "use only view/grep/glob on files under source-text/ and handoffs/"
)


UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f\[\]]")
SURROGATES = re.compile("[\ud800-\udfff]")
CITATION_RULE = (
    "Put each citation directly after its closing quote, before any other text: "
    '`"exact words" [file p.N]`. A citation at the end of the paragraph does not cover '
    "the quotes before it."
)


def scrub(text: str) -> str:
    """Replace lone surrogates (valid in JSON, unencodable in UTF-8) with U+FFFD."""
    return SURROGATES.sub("\ufffd", text)


def module_of(err: BaseException) -> str:
    """The module an exception was tagged with by `_attempt`, or empty."""
    for note in getattr(err, "__notes__", []):
        if note.startswith("module: "):
            return str(note[len("module: ") :])
    return ""


class ModuleFailed(Exception):
    def __init__(self, module: str, findings: Sequence[str]) -> None:
        super().__init__(f"{module}: no valid handoff after {ATTEMPTS} attempts")
        self.module = module
        self.findings = list(findings)


class ContextError(Exception):
    """context.json is unreadable or lacks a required key."""


def load_context(case: Path) -> dict[str, Any]:
    path = case / "context.json"
    try:
        context: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ContextError(f"cannot read {path}: {err}") from err
    try:
        json.dumps(context, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as err:
        raise ContextError("context.json holds a lone surrogate (invalid text)") from err
    for keys in REQUIRED:
        node: Any = context
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        if node is None or str(node).strip() == "":
            raise ContextError(f"context.json lacks {'.'.join(keys)}")
    pathway = context["run"]["pathway"]
    if not isinstance(pathway, str) or pathway not in PATHWAYS:
        raise ContextError(f"unknown pathway {pathway!r}")
    return context


def _model_settings() -> dict[str, Any]:
    env = os.environ
    try:
        credits = int(env.get("RBOT_MAX_CREDITS", "100"))
    except ValueError as err:
        raise ContextError(f"RBOT_MAX_CREDITS is not an integer: {err}") from err
    return {
        "model": env.get("RBOT_MODEL", "auto"),
        "effort": env.get("RBOT_EFFORT", ""),
        "auto_tier": env.get("RBOT_AUTO_TIER", "intelligence"),
        "max_credits": credits,
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    _write(tmp, text)
    os.replace(tmp, path)


def _refuse_overlap(case: Path, work: Path) -> None:
    """The source rebuild deletes work/sources and work/source-text: never near the case."""
    c, w = case.resolve(), work.resolve()
    if c.is_relative_to(w) or w.is_relative_to(c):
        raise ContextError("--out must be outside the case folder and the case outside --out")


def stage_sources(case: Path, work: Path) -> None:
    """Rebuild work/sources and work/source-text from the case; refuse unsafe entries."""
    for name in ("sources", "source-text"):
        if (work / name).exists():
            shutil.rmtree(work / name)
    (work / "sources").mkdir(parents=True)
    for path in sorted(case.iterdir()):
        name = path.name
        if name == "context.json" or (path.is_dir() and not path.is_symlink()):
            continue
        if path.is_symlink() or not path.is_file():
            raise SourceError(f"{name}: not a regular file (symlink or special file); remove it")
        if UNSAFE_NAME.search(name):
            raise SourceError(
                f"{name!r}: rename the file; names may not contain control characters or [ ]"
            )
        shutil.copyfile(path, work / "sources" / name)  # contents only: no mode is copied


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_listing(pages: Sequence[SourceText], skipped: Sequence[str], sources: Path) -> str:
    listing = "\n".join(
        f"- source-text/{s.name}/ : {s.pages} page file(s), p0001.txt to p{s.pages:04d}.txt "
        f"(cite as [{s.name} p.N]; sha256 {_sha256(sources / s.name)})"
        for s in pages
    )
    if skipped:
        listing += "\n\nSkipped (unsupported type, not readable): " + ", ".join(skipped)
    return listing or "(none)"


def build_prompt(
    module: str, prompts: Path, context: dict[str, Any], listing: str, upstream: Sequence[Path]
) -> str:
    ups = "\n".join(f"- handoffs/{p.name} (sha256 {_sha256(p)})" for p in upstream)
    context_json = json.dumps(context, indent=2, sort_keys=True)
    pages_intro = "Read pages with your view and grep tools; each page is one file."
    return "\n\n".join(
        [
            _read(prompts / "_common.md").rstrip(),
            _read(prompts / f"{module}.md").rstrip(),
            f"## Case context\n\n```json\n{context_json}\n```",
            f"## Source pages\n\n{pages_intro}\n\n{listing}",
            "## Upstream handoffs\n\n" + (ups or "(none)"),
        ]
    )


def _log(work: Path, **entry: Any) -> None:
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **entry}, ensure_ascii=True)
    with (work / "run.log").open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _findings(result: CallResult, text: str, source_text: Path) -> list[str]:
    found = [VIOLATION.format(v) for v in result.violations]
    return found + check(text, source_text, quoted_findings=True)


@dataclass(frozen=True)
class Setup:
    work: Path
    gh: Sequence[str]
    settings: dict[str, Any]


def _log_best_effort(work: Path, err: BaseException, **entry: Any) -> None:
    """Log a failed attempt without masking `err`: a logging failure becomes a note on it."""
    try:
        _log(work, **entry)
    except (OSError, ValueError) as log_err:
        err.add_note(f"run.log line not written: {log_err}")


def _attempt(module: str, n: int, prompt: str, setup: Setup) -> tuple[str, list[str]]:
    """One call plus validation. Returns (handoff text, findings); logs the attempt."""
    log: dict[str, Any] = {
        "module": module,
        "attempt": n,
        "requested_model": setup.settings["model"],
        "model": None,
        "exit": None,
        "seconds": None,
        "credits": None,
    }
    try:
        return _attempt_once(module, n, prompt, setup, log)
    except Exception as err:  # logged, tagged with the module and re-raised: nothing is hidden
        err.add_note(f"module: {module}")
        outcome = "sandbox" if isinstance(err, SandboxViolation) else "error"
        _log_best_effort(setup.work, err, **log, outcome=outcome, findings=[str(err)])
        raise


def _attempt_once(
    module: str, n: int, prompt: str, setup: Setup, log: dict[str, Any]
) -> tuple[str, list[str]]:
    work = setup.work
    adir = work / "attempts" / module / str(n)
    if adir.exists():
        shutil.rmtree(adir)
    result = call(prompt, cwd=work, attempt_dir=adir, gh=setup.gh, **setup.settings)
    text = scrub(result.text)
    findings = _findings(result, text, work / "source-text")
    log.update(model=result.model, exit=result.exit_code, seconds=round(result.seconds, 3))
    log["credits"] = result.credits
    _write(adir / "handoff.md", text)
    _write(adir / "findings.txt", scrub("\n".join(findings)))
    _log(work, **log, outcome="findings" if findings else "accepted", findings=findings)
    return text, findings


NO_CITATION = "no citation right after it"
NO_QUOTE = "Findings item with no verified quote"
QUOTE_RULE = (
    "Back every Findings item with at least one verbatim quote (12+ characters) and its citation."
)
NOT_FOUND = "quote not found on"
GREP_RULE = (
    "For each quote not found: search source-text/<file>/ with your grep tool for a distinctive "
    "part of the quote and cite the page file it is in (p0111.txt is p.111), or drop the quote."
)


def _accepted(handoff: Path, fp: Path, fingerprint: str, source_text: Path) -> bool:
    """A stored handoff is trusted only if its fingerprint matches and the checker still passes."""
    if not (handoff.is_file() and fp.is_file() and _read(fp).strip() == fingerprint):
        return False
    stored = handoff.read_text(encoding="utf-8", errors="replace")
    return not check(stored, source_text, quoted_findings=True)


def _run_module(module: str, prompt: str, setup: Setup) -> None:
    work = setup.work
    fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    handoff, fp = work / "handoffs" / f"{module}.md", work / "handoffs" / f"{module}.fp"
    if _accepted(handoff, fp, fingerprint, work / "source-text"):
        return
    findings: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        text_prompt = prompt
        if findings:
            rules = [CITATION_RULE] * any(NO_CITATION in f for f in findings)
            rules += [QUOTE_RULE] * any(NO_QUOTE in f for f in findings)
            rules += [GREP_RULE] * any(NOT_FOUND in f for f in findings)
            rule = "".join(f"\n{r}\n" for r in rules)
            text_prompt += (
                "\n\n## Findings from your previous attempt\n\nFix every item and reply with the "
                f"complete handoff again:{rule}\n" + "\n".join(f"- {f}" for f in findings)
            )
        text, findings = _attempt(module, n, text_prompt, setup)
        if not findings:
            fp.unlink(missing_ok=True)  # never a matching .fp beside a half-written .md
            _write_atomic(handoff, text)
            _write_atomic(fp, fingerprint)
            return
    raise ModuleFailed(module, findings)


def run_case(
    case: Path,
    work: Path,
    *,
    gh: Sequence[str] = ("gh",),
    modules: Sequence[str] | None = None,
    prompts: Path = PROMPTS_DIR,
) -> Path:
    _refuse_overlap(case, work)
    context = load_context(case)
    settings = _model_settings()
    names = modules or [m for m in PATHWAYS[context["run"]["pathway"]] if m in IMPLEMENTED]
    work.mkdir(parents=True, exist_ok=True)
    stage_sources(case, work)
    pages, skipped = write_source_text(work / "sources", work / "source-text")
    setup, listing = Setup(work, gh, settings), source_listing(pages, skipped, work / "sources")
    for i, module in enumerate(names):
        upstream = [p for m in names[:i] if (p := work / "handoffs" / f"{m}.md").is_file()]
        prompt = build_prompt(module, prompts, context, listing, upstream)
        _run_module(module, prompt, setup)
    return render(work, context, names)
