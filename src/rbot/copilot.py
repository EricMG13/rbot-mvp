"""One `gh copilot` call: prompt in a file, read-only tools, events per docs/copilot-cli.md."""

import json
import os
import signal
import subprocess  # nosec B404
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

READ_ONLY_TOOLS = ("view", "grep", "glob")
STDERR_LIMIT = 500
KILL_WAIT = 5
UNNAMED = "<unnamed>"
ENV_ALLOW = frozenset(
    "PATH HOME USERPROFILE APPDATA LOCALAPPDATA SYSTEMROOT SYSTEMDRIVE WINDIR COMSPEC PATHEXT "
    "TEMP TMP TMPDIR LANG XDG_CONFIG_HOME GH_CONFIG_DIR GH_TOKEN GITHUB_TOKEN "
    "COPILOT_GITHUB_TOKEN PROGRAMDATA PROGRAMFILES PROGRAMFILES(X86)".split()
)


class CopilotError(Exception):
    """The call failed: non-zero exit, timeout, bad or missing events, or an error event."""


class SandboxViolation(CopilotError):
    """A tool outside READ_ONLY_TOOLS ran to success: the case folder may have been changed."""


@dataclass(frozen=True)
class CallResult:
    text: str
    exit_code: int
    seconds: float
    credits: str | None  # session total, nanoAIU, as the digits Copilot reports
    forbidden_tools: tuple[str, ...]  # tools outside READ_ONLY_TOOLS the model asked for or started
    model: str | None  # routed model: session.auto_mode_resolved, else the final assistant.message
    denied: tuple[str, ...] = ()  # tools whose execution Copilot refused (error.code "denied")
    violations: tuple[
        str, ...
    ] = ()  # forbidden_tools plus "denied:<tool>"; engine treats as finding
    ran: tuple[str, ...] = ()  # forbidden tools whose execution completed with success


def _add(seen: list[str], name: object) -> None:
    label = name if isinstance(name, str) and name else UNNAMED
    if label not in seen:
        seen.append(label)


class _Stream:
    """Fold events into the fields of CallResult."""

    def __init__(self) -> None:
        self.forbidden: list[str] = []
        self.denied: list[str] = []
        self.ran: list[str] = []
        self.started: dict[str, object] = {}  # toolCallId -> name (execution_complete has none)
        self.text: str | None = None
        self.model: str | None = None
        self.chosen: str | None = None
        self.credits: str | None = None
        self.result: dict[str, Any] | None = None

    def message(self, data: dict[str, Any]) -> None:
        requests = data.get("toolRequests") or []
        if not isinstance(requests, list):
            raise CopilotError("assistant.message: toolRequests is not a list")
        for req in requests:
            if not isinstance(req, dict) or req.get("name") not in READ_ONLY_TOOLS:
                _add(self.forbidden, req.get("name") if isinstance(req, dict) else None)
        if not requests:
            content = data.get("content", "")
            if not isinstance(content, str):
                raise CopilotError("assistant.message: content is not a string")
            self.text, self.model = content, data.get("model")

    def start(self, data: dict[str, Any]) -> None:
        name = data.get("toolName")
        self.started[str(data.get("toolCallId"))] = name
        if name not in READ_ONLY_TOOLS:
            _add(self.forbidden, name)

    def complete(self, data: dict[str, Any]) -> None:
        name = self.started.get(str(data.get("toolCallId")), UNNAMED)
        error = data.get("error")
        if (
            data.get("success") is False
            and isinstance(error, dict)
            and error.get("code") == "denied"
        ):
            _add(self.denied, name)
        elif data.get("success") is True and name not in READ_ONLY_TOOLS:
            _add(self.ran, name)

    def feed(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise CopilotError("event line is not a JSON object")
        if self.result is not None:
            raise CopilotError("event after the result line (result must be unique and last)")
        kind, data = event.get("type"), event.get("data")
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise CopilotError(f"event {kind}: data is not an object")
        if isinstance(kind, str) and kind.endswith("error"):
            raise CopilotError(f"error event {kind}: {str(data.get('message', ''))[:STDERR_LIMIT]}")
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


def completed_forbidden(lines: Iterable[str]) -> tuple[str, ...]:
    """Tolerant scan: forbidden tools that completed with success; bad lines are skipped."""
    stream = _Stream()
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and event.get("type") == "tool.execution_start":
            stream.start(data)
        elif isinstance(data, dict) and event.get("type") == "tool.execution_complete":
            stream.complete(data)
    return tuple(stream.ran)


def _violation(ran: tuple[str, ...]) -> SandboxViolation:
    return SandboxViolation(f"tool outside read-only set ran: {', '.join(ran)}")


def parse_events(lines: Iterable[str]) -> CallResult:
    """Parse a stream; a completed forbidden tool wins over any other failure (SandboxViolation)."""
    kept = list(lines)
    try:
        return _parse(kept)
    except SandboxViolation:
        raise
    except CopilotError as err:
        if ran := completed_forbidden(kept):
            raise _violation(ran) from err
        raise


def _parse(lines: Iterable[str]) -> CallResult:
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
    if type(code) is not int or code != 0:
        raise CopilotError(f"result reported exit {code}")
    if stream.text is None:
        raise CopilotError("no final assistant message in the event stream")
    forbidden, denied = tuple(stream.forbidden), tuple(stream.denied)
    return CallResult(  # `call` replaces seconds with wall time
        stream.text, 0, 0.0, stream.credits, forbidden, stream.chosen or stream.model, denied,
        (*forbidden, *(f"denied:{t}" for t in denied)), tuple(stream.ran),
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
        "--deny-tool", "shell", "--deny-tool", "write", "--deny-tool", "url",
        "--max-ai-credits", str(max_credits),
        "-p", pointer,
    ]  # fmt: skip


def child_env() -> dict[str, str]:
    """Allowlisted environment: nothing that redirects Copilot's home, provider or telemetry."""
    fold = sys.platform == "win32"  # Windows names are case-insensitive
    return {
        k: v
        for k, v in os.environ.items()
        if (k.upper() if fold else k) in ENV_ALLOW or (k.upper() if fold else k).startswith("LC_")
    }


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    if sys.platform == "win32":
        cmd = ["taskkill", "/T", "/F", "/PID", str(proc.pid)]  # noqa: S607  # nosec B607
        subprocess.run(cmd, capture_output=True, check=False)  # noqa: S603  # nosec B603
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # own session, so pgid == pid
        except ProcessLookupError:
            pass  # already gone


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int | None, bytes, bytes]:
    """Run in its own process group; on timeout kill the tree. Returns (code or None, out, err)."""
    flags: dict[str, Any] = {}
    if sys.platform == "win32":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    # argv is a fixed list (no shell); caller-supplied strings are only the pointer and gh
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        argv, cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **flags,
    )  # fmt: skip
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return None, out or b"", err or b""
    return proc.returncode, out, err


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
    root, attempt = cwd.resolve(), attempt_dir.resolve()
    if not attempt.is_relative_to(root):
        raise ValueError(f"attempt_dir {attempt_dir} must be inside cwd {cwd}")
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    rel = (attempt.relative_to(root) / "prompt.md").as_posix()
    pointer = f"Read the file {rel} and follow it exactly."
    model_flags = ["--model", model]
    if model == "auto":
        model_flags += ["--auto-tier", auto_tier]
    elif effort:
        model_flags += ["--reasoning-effort", effort]
    started = time.monotonic()
    code, out, err = _run(build_argv(gh, pointer, model_flags, max_credits), root, timeout)
    out = out.replace(b"\r\n", b"\n")
    (attempt / "events.jsonl").write_bytes(out)
    (attempt / "stderr.txt").write_bytes(err)
    lines = out.decode("utf-8", errors="replace").split("\n")
    if code is None or code != 0:
        if ran := completed_forbidden(lines):
            raise _violation(ran)
        if code is None:
            raise CopilotError(f"timeout after {timeout:g} s")
        text = err.decode("utf-8", errors="replace").strip()[:STDERR_LIMIT]
        raise CopilotError(f"gh copilot exit {code}: {text}")
    try:
        out.decode("utf-8")
    except UnicodeDecodeError as exc:
        if ran := completed_forbidden(lines):
            raise _violation(ran) from None
        raise CopilotError(f"stdout is not valid UTF-8: {exc}") from None
    # split on LF only: str.splitlines also splits on U+2028 that JSON may carry unescaped
    result = parse_events(lines)
    if result.ran:
        raise _violation(result.ran)
    return replace(result, seconds=time.monotonic() - started)
