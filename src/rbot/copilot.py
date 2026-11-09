"""One `gh copilot` call: prompt in a file, read-only tools, events per docs/copilot-cli.md."""

import json
import os
import subprocess  # nosec B404
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

READ_ONLY_TOOLS = ("view", "grep", "glob")
STDERR_LIMIT = 500


class CopilotError(Exception):
    """The call failed: non-zero exit, timeout, missing result line or missing final message."""


@dataclass(frozen=True)
class CallResult:
    text: str
    exit_code: int
    seconds: float
    credits: str | None  # session total, nanoAIU, as the digits Copilot reports
    forbidden_tools: tuple[str, ...]  # tools outside READ_ONLY_TOOLS the model asked for or started
    model: str | None  # routed model: session.auto_mode_resolved, else the final assistant.message
    denied: tuple[str, ...] = ()  # tools whose execution Copilot refused (error.code "denied")


def _add(seen: list[str], name: object) -> None:
    if isinstance(name, str) and name not in seen:
        seen.append(name)


class _Stream:
    """Fold events into the fields of CallResult."""

    def __init__(self) -> None:
        self.forbidden: list[str] = []
        self.denied: list[str] = []
        self.started: dict[str, str] = {}  # toolCallId -> name (execution_complete has no name)
        self.text: str | None = None
        self.model: str | None = None
        self.chosen: str | None = None
        self.credits: str | None = None
        self.result: dict[str, Any] | None = None

    def message(self, data: dict[str, Any]) -> None:
        requests = data.get("toolRequests") or []
        for req in requests:
            if req.get("name") not in READ_ONLY_TOOLS:
                _add(self.forbidden, req.get("name"))
        if not requests:
            self.text, self.model = data.get("content", ""), data.get("model")

    def start(self, data: dict[str, Any]) -> None:
        name = data.get("toolName", "")
        self.started[data.get("toolCallId", "")] = name
        if name not in READ_ONLY_TOOLS:
            _add(self.forbidden, name)

    def complete(self, data: dict[str, Any]) -> None:
        if data.get("success") is False and (data.get("error") or {}).get("code") == "denied":
            _add(self.denied, self.started.get(data.get("toolCallId", ""), "unknown"))

    def feed(self, event: dict[str, Any]) -> None:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "session.auto_mode_resolved":
            self.chosen = data.get("chosenModel")
        elif kind == "assistant.message":
            self.message(data)
        elif kind == "tool.execution_start":
            self.start(data)
        elif kind == "tool.execution_complete":
            self.complete(data)
        elif kind == "session.usage_checkpoint" and "totalNanoAiu" in data:
            self.credits = str(data["totalNanoAiu"])
        elif kind == "result":
            self.result = event


def parse_events(lines: Iterable[str]) -> CallResult:
    stream = _Stream()
    for raw in lines:
        if raw.strip():
            try:
                stream.feed(json.loads(raw))
            except json.JSONDecodeError as err:
                raise CopilotError(f"unparseable event line: {err}") from err
    if stream.result is None:
        raise CopilotError("no result line in the event stream")
    code = stream.result.get("exitCode")
    if code != 0:
        raise CopilotError(f"result reported exit {code}")
    if stream.text is None:
        raise CopilotError("no final assistant message in the event stream")
    return CallResult(  # `call` replaces seconds with wall time
        stream.text, 0, 0.0, stream.credits, tuple(stream.forbidden),
        stream.chosen or stream.model, tuple(stream.denied),
    )  # fmt: skip


def build_argv(
    gh: Sequence[str], pointer: str, model_flags: list[str], max_credits: int
) -> list[str]:
    return [
        *gh, "copilot", *model_flags,
        "--output-format", "json",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-ask-user",
        "--no-auto-update",
        "--disallow-temp-dir",
        "-s",
        "--available-tools", ",".join(READ_ONLY_TOOLS),
        "--max-ai-credits", str(max_credits),
        "-p", pointer,
    ]  # fmt: skip


def call(  # noqa: PLR0913
    prompt: str,
    *,
    cwd: Path,
    attempt_dir: Path,
    gh: Sequence[str] = ("gh",),
    model: str = "auto",
    effort: str = "",
    auto_tier: str = "intelligence",
    max_credits: int = 100,
    timeout: float = 1800,
) -> CallResult:
    try:
        relative = attempt_dir.relative_to(cwd)
    except ValueError:
        raise ValueError(f"attempt_dir {attempt_dir} must be inside cwd {cwd}") from None
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    pointer = f"Read the file {(relative / 'prompt.md').as_posix()} and follow it exactly."
    model_flags = ["--model", model]
    if model == "auto":
        model_flags += ["--auto-tier", auto_tier]
    elif effort:
        model_flags += ["--reasoning-effort", effort]
    argv = build_argv(gh, pointer, model_flags, max_credits)
    env = {k: v for k, v in os.environ.items() if k != "COPILOT_ALLOW_ALL"}
    # argv is a fixed list (no shell); caller-supplied strings are only the pointer and gh
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603
            argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise CopilotError(f"timeout after {timeout:g} s") from None
    raw = proc.stdout.replace(b"\r\n", b"\n")
    (attempt_dir / "events.jsonl").write_bytes(raw)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:STDERR_LIMIT]
        raise CopilotError(f"gh copilot exit {proc.returncode}: {err}")
    # split on LF only: str.splitlines also splits on U+2028 that JSON may carry unescaped
    result = parse_events(raw.decode("utf-8", errors="replace").split("\n"))
    return replace(result, seconds=time.monotonic() - started)
