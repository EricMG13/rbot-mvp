"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from rbot import __version__
from rbot.copilot import CopilotError, SandboxViolation
from rbot.engine import ContextError, ModuleFailed, module_of, run_case, scrub
from rbot.sources import SourceError

LATCHED = "sandbox violation recorded \u2014 inspect work/, then delete FAILED.md to rerun"
RESUME = "rerun the same command to resume"
SANDBOX = "sandbox violation — inspect work/ before rerunning"


def _failed(out: Path, module: str, reason: str, findings: Sequence[str], hint: str) -> None:
    lines = ["# FAILED", "", f"Module: {module or '-'}", f"Reason: {reason}", ""]
    if findings:
        lines += ["Findings:", *(f"- {f}" for f in findings), ""]
    lines += [hint, ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "FAILED.md").write_text(scrub("\n".join(lines)), encoding="utf-8", newline="\n")


def _has_sandbox_violation(err: BaseException) -> bool:
    seen: set[int] = set()
    todo: list[BaseException | None] = [err]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, SandboxViolation):
            return True
        todo += [cur.__cause__, cur.__context__]
    return False


def _run(case: Path, out: Path) -> int:
    failed = out / "FAILED.md"
    if failed.is_file() and SANDBOX in failed.read_text(encoding="utf-8", errors="replace"):
        print(LATCHED, file=sys.stderr)
        return 1
    try:
        run_case(case, out)
    except ModuleFailed as err:
        _failed(out, err.module, str(err), err.findings, RESUME)
    except (CopilotError, SourceError, ContextError, OSError) as err:
        hint = SANDBOX if _has_sandbox_violation(err) else RESUME
        _failed(out, module_of(err), f"{type(err).__name__}: {err}", [], hint)
    else:
        failed.unlink(missing_ok=True)
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rbot")
    parser.add_argument("--version", action="version", version=f"rbot {__version__}")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run one case folder")
    run.add_argument("case", type=Path)
    run.add_argument("--out", type=Path, required=True, help="work directory")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args.case, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
