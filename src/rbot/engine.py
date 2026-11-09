"""Run one case: extract sources, run each module with validated attempts, render the report."""

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rbot.copilot import CallResult, CopilotError, SandboxViolation, call
from rbot.handoff import check
from rbot.pathways import IMPLEMENTED, PATHWAYS, PROMPTS_DIR
from rbot.render import render
from rbot.sources import SourceText, write_source_text

ATTEMPTS = 3
REQUIRED = (("run", "pathway"), ("run", "as_of"), ("identity", "issuer_name"))
VIOLATION = (
    "the run tried a tool outside the read-only set or outside the case folder: {}; "
    "use only view/grep/glob on files under source-text/ and handoffs/"
)


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
    for keys in REQUIRED:
        node: Any = context
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        if node is None or str(node).strip() == "":
            raise ContextError(f"context.json lacks {'.'.join(keys)}")
    if context["run"]["pathway"] not in PATHWAYS:
        raise ContextError(f"unknown pathway {context['run']['pathway']!r}")
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


def source_listing(pages: Sequence[SourceText], skipped: Sequence[str]) -> str:
    listing = "\n".join(
        f"- source-text/{s.name}/ : {s.pages} page file(s), p0001.txt to p{s.pages:04d}.txt "
        f"(cite as [{s.name} p.N])"
        for s in pages
    )
    if skipped:
        listing += "\n\nSkipped (unsupported type, not readable): " + ", ".join(skipped)
    return listing or "(none)"


def build_prompt(
    module: str, prompts: Path, context: dict[str, Any], listing: str, upstream: Sequence[Path]
) -> str:
    ups = "\n".join(
        f"- handoffs/{p.name} (sha256 {hashlib.sha256(p.read_bytes()).hexdigest()})"
        for p in upstream
    )
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
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **entry}, ensure_ascii=False)
    with (work / "run.log").open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _findings(result: CallResult, text: str, source_text: Path) -> list[str]:
    found = [VIOLATION.format(v) for v in result.violations]
    return found + check(text, source_text)


@dataclass(frozen=True)
class Setup:
    work: Path
    gh: Sequence[str]
    settings: dict[str, Any]


def _attempt(module: str, n: int, prompt: str, setup: Setup) -> tuple[str, list[str]]:
    """One call plus validation. Returns (handoff text, findings); logs the attempt."""
    work, settings = setup.work, setup.settings
    adir = work / "attempts" / module / str(n)
    log: dict[str, Any] = {
        "module": module,
        "attempt": n,
        "requested_model": settings["model"],
        "model": None,
        "exit": None,
        "seconds": None,
        "credits": None,
    }
    try:
        result = call(prompt, cwd=work, attempt_dir=adir, gh=setup.gh, **settings)
    except CopilotError as err:
        outcome = "sandbox" if isinstance(err, SandboxViolation) else "error"
        _log(work, **log, outcome=outcome, findings=[str(err)])
        raise
    findings = _findings(result, result.text, work / "source-text")
    log.update(model=result.model, exit=result.exit_code, seconds=round(result.seconds, 3))
    log["credits"] = result.credits
    _write(adir / "handoff.md", result.text)
    _write(adir / "findings.txt", "\n".join(findings))
    _log(work, **log, outcome="findings" if findings else "accepted", findings=findings)
    return result.text, findings


def _run_module(module: str, prompt: str, setup: Setup) -> None:
    work = setup.work
    fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    handoff, fp = work / "handoffs" / f"{module}.md", work / "handoffs" / f"{module}.fp"
    if handoff.is_file() and fp.is_file() and _read(fp).strip() == fingerprint:
        return
    findings: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        text_prompt = prompt
        if findings:
            text_prompt += (
                "\n\n## Findings from your previous attempt\n\nFix every item and reply with the "
                "complete handoff again:\n" + "\n".join(f"- {f}" for f in findings)
            )
        text, findings = _attempt(module, n, text_prompt, setup)
        if not findings:
            _write(handoff, text)
            _write(fp, fingerprint)
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
    context = load_context(case)
    settings = _model_settings()
    names = modules or [m for m in PATHWAYS[context["run"]["pathway"]] if m in IMPLEMENTED]
    work.mkdir(parents=True, exist_ok=True)
    (work / "sources").mkdir(exist_ok=True)
    for path in sorted(p for p in case.iterdir() if p.is_file() and p.name != "context.json"):
        shutil.copy2(path, work / "sources" / path.name)
    pages, skipped = write_source_text(work / "sources", work / "source-text")
    setup, listing = Setup(work, gh, settings), source_listing(pages, skipped)
    for i, module in enumerate(names):
        upstream = [p for m in names[:i] if (p := work / "handoffs" / f"{m}.md").is_file()]
        prompt = build_prompt(module, prompts, context, listing, upstream)
        _run_module(module, prompt, setup)
    return render(work, context, names)
