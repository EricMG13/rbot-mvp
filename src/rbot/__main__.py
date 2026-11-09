"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from rbot import __version__
from rbot.copilot import CopilotError, SandboxViolation
from rbot.engine import ContextError, ModuleFailed, run_case
from rbot.sources import SourceError

RESUME = "rerun the same command to resume"
SANDBOX = "sandbox violation — inspect work/ before rerunning"


def _failed(out: Path, module: str, reason: str, findings: Sequence[str], hint: str) -> None:
    lines = ["# FAILED", "", f"Module: {module or '-'}", f"Reason: {reason}", ""]
    if findings:
        lines += ["Findings:", *(f"- {f}" for f in findings), ""]
    lines += [hint, ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "FAILED.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _run(case: Path, out: Path) -> int:
    try:
        run_case(case, out)
    except ModuleFailed as err:
        _failed(out, err.module, str(err), err.findings, RESUME)
    except SandboxViolation as err:
        _failed(out, "", str(err), [], SANDBOX)
    except (CopilotError, SourceError, ContextError, OSError) as err:
        _failed(out, "", f"{type(err).__name__}: {err}", [], RESUME)
    else:
        (out / "FAILED.md").unlink(missing_ok=True)
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
