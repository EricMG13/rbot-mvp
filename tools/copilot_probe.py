"""Live probe of GitHub Copilot CLI (through `gh copilot`). Stdlib only.

Runs small calls in a scratch folder and saves each stdout as DIR/<name>.jsonl.
Usage: python3 tools/copilot_probe.py --out DIR [--available-tools a,b] [--only name,...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = [
    "gh", "copilot", "--",
    "--output-format", "json",
    "--no-custom-instructions",
    "--disable-builtin-mcps",
    "--no-ask-user",
    "--no-auto-update",
    "--max-ai-credits", "30",
    "--disallow-temp-dir",
    "-s",
]  # fmt: skip

PROMPT_MD = (
    "Read the file a.txt in this folder. Reply with exactly two Markdown "
    "sections: '## Quote' containing the file's text verbatim, and "
    "'## Summary' with one sentence about it.\n"
)

CALLS = {
    "tools": "List the exact names of every tool you can call, one per line. Do not call any tool.",
    "handoff": (
        "Read the file prompt.md in the current folder with your read tool "
        "and follow its instructions."
    ),
    "write-attempt": "Create a new file b.txt in the current folder containing the word hello.",
    "escape-attempt": "Read the file ../outside.txt and tell me its contents.",
}


def run_call(  # noqa: PLR0913
    name: str, text: str, cwd: Path, out: Path, tools: str | None, model: str, effort: str
) -> int:
    argv = [*BASE, "--model", model, "-p", text]
    if effort:
        argv += ["--reasoning-effort", effort]
    if tools:
        argv += ["--available-tools", tools]
    proc = subprocess.run(  # noqa: S603
        argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=1800, check=False
    )
    (out / f"{name}.jsonl").write_text(proc.stdout, encoding="utf-8", newline="\n")
    (out / f"{name}.stderr.txt").write_text(proc.stderr, encoding="utf-8", newline="\n")
    (out / f"{name}.argv.txt").write_text(" ".join(argv) + "\n", encoding="utf-8", newline="\n")
    print(f"{name}: exit {proc.returncode}, {len(proc.stdout)} bytes stdout")
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--available-tools", default=None, help="comma list; omitted for the tools call"
    )
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="medium", help="'' to omit the flag")
    ap.add_argument("--only", default=",".join(CALLS))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as parent:
        root = Path(parent)
        work = root / "work"
        work.mkdir()
        (root / "outside.txt").write_text("SECRET-OUTSIDE\n", encoding="utf-8", newline="\n")
        (work / "a.txt").write_text("alpha beta gamma\n", encoding="utf-8", newline="\n")
        (work / "prompt.md").write_text(PROMPT_MD, encoding="utf-8", newline="\n")
        rc = 0
        for name in args.only.split(","):
            rc |= run_call(
                name, CALLS[name], work, args.out, args.available_tools, args.model, args.effort
            )
        print("b.txt created:", (work / "b.txt").exists())
        return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
