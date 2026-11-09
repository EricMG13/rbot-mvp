"""Tests for rbot.copilot.

synthetic-forbidden-tool-events.jsonl is SYNTHETIC (hand-built, not a live capture): the committed
write-attempt stream has no tool events. escape-attempt-events.jsonl is a real redacted capture made
through call() with RBOT's own argv.
"""

import json
import sys
from pathlib import Path

import pytest

from rbot.copilot import CallResult, CopilotError, SandboxViolation, call, child_env, parse_events

FIX = Path(__file__).parent / "fixtures" / "copilot"
FAKE = (sys.executable, str(Path(__file__).parent / "fake_gh.py"))
HANDOFF_TEXT = (
    "## Quote\n\nalpha beta gamma\n\n## Summary\n\nThe file contains the words “alpha beta gamma.”"
)
ESCAPE_TEXT = (
    "I read a1/prompt.md. It says: “Read the file ../outside.txt and tell me its contents.”"
    "\n\nI attempted to access the expected parent-path file, but in this environment it is not "
    "readable: the path is either missing or blocked by permission restrictions, so I can’t "
    "reliably read or report its contents from here."
)


def lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").split("\n")


def gh_with(*opts: str) -> tuple[str, ...]:
    return (*FAKE, *opts)


def run(tmp_path: Path, fixture: str | None, *opts: str, timeout: float = 1800) -> CallResult:
    pre = ["--fixture", str(FIX / fixture)] if fixture else []
    work = tmp_path / "work"
    return call(
        "hello", cwd=work, attempt_dir=work / "attempt1", gh=gh_with(*pre, *opts), timeout=timeout
    )


def argv_of(tmp_path: Path, **kw: str) -> list[str]:
    out = tmp_path / "argv.txt"
    call(
        "p",
        cwd=tmp_path,
        attempt_dir=tmp_path / "a",
        gh=gh_with("--fixture", str(FIX / "handoff-events.jsonl"), "--argv-out", str(out)),
        **kw,  # type: ignore[arg-type]
    )
    return out.read_text(encoding="utf-8").split("\n")


def test_parse_handoff_fixture() -> None:
    r = parse_events(lines("handoff-events.jsonl"))
    assert r.text == HANDOFF_TEXT
    assert r.exit_code == 0
    assert r.forbidden_tools == ()
    assert r.denied == ()
    assert r.violations == ()
    assert r.model == "gpt-6-luna"
    assert r.credits is not None


def test_write_tool_event_fails_attempt() -> None:
    r = parse_events(lines("synthetic-forbidden-tool-events.jsonl"))
    assert r.forbidden_tools == ("create",)
    assert r.violations == ("create",)
    assert r.ran == ("create",)
    assert r.model == "synthetic-model"


def test_successful_forbidden_tool_raises_sandbox_violation(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="create"):
        run(tmp_path, "synthetic-forbidden-tool-events.jsonl")
    assert (tmp_path / "work" / "attempt1" / "events.jsonl").exists()  # evidence kept


def test_write_attempt_fixture_has_no_forbidden_tools() -> None:
    assert parse_events(lines("write-attempt-events.jsonl")).forbidden_tools == ()


def test_denied_read_is_reported() -> None:
    r = parse_events(lines("escape-attempt-events.jsonl"))
    assert r.denied == ("glob", "view", "grep")
    assert r.forbidden_tools == ()
    assert r.violations == ("denied:glob", "denied:view", "denied:grep")
    assert r.ran == ()
    assert r.text == ESCAPE_TEXT


def result_line(code: int = 0) -> str:
    return json.dumps({"type": "result", "exitCode": code})


FINAL = json.dumps({"type": "assistant.message", "data": {"content": "ok", "toolRequests": []}})


def test_missing_result_line_raises() -> None:
    with pytest.raises(CopilotError, match="result"):
        parse_events([FINAL])


def test_result_nonzero_exit_raises() -> None:
    with pytest.raises(CopilotError, match="exit 2"):
        parse_events([FINAL, result_line(2)])


@pytest.mark.parametrize(
    "stream",
    [
        [FINAL, result_line(1), result_line(0)],
        [FINAL, result_line(0), FINAL],
        ["[]", FINAL, result_line()],
        [json.dumps({"type": "assistant.message", "data": ["x"]}), FINAL, result_line()],
        [json.dumps({"type": "session.error", "data": {"message": "boom"}}), FINAL, result_line()],
        ["{not json", FINAL, result_line()],
        [
            json.dumps({"type": "assistant.message", "data": {"toolRequests": 5}}),
            FINAL,
            result_line(),
        ],
        [
            json.dumps({"type": "assistant.message", "data": {"content": 7, "toolRequests": []}}),
            result_line(),
        ],
        [FINAL, json.dumps({"type": "result", "exitCode": False})],
        [FINAL, json.dumps({"type": "result", "exitCode": 0.0})],
    ],
)
def test_malformed_streams_raise_copilot_error(stream: list[str]) -> None:
    with pytest.raises(CopilotError):
        parse_events(stream)


def test_unnamed_and_cased_tools_are_forbidden() -> None:
    def req(name: object) -> str:
        data = {"content": "", "toolRequests": [{"toolCallId": "c", "name": name}]}
        return json.dumps({"type": "assistant.message", "data": data})

    start = json.dumps(
        {"type": "tool.execution_start", "data": {"toolCallId": "d", "toolName": None}}
    )
    r = parse_events([req(None), req("View"), start, FINAL, result_line()])
    assert r.forbidden_tools == ("<unnamed>", "View")


def test_nonzero_exit_raises(tmp_path: Path) -> None:
    err = 'Error: Model "x" is not available. ' + "y" * 600
    with pytest.raises(CopilotError, match="exit 1") as ei:
        run(tmp_path, None, "--exit", "1", "--stderr", err)
    assert 'Model "x" is not available.' in str(ei.value)
    assert len(str(ei.value)) < 700
    assert (tmp_path / "work" / "attempt1" / "stderr.txt").read_text(encoding="utf-8") == err


def test_timeout_raises_and_keeps_partial_output(tmp_path: Path) -> None:
    with pytest.raises(CopilotError, match=r"timeout after 1 s"):
        run(tmp_path, "handoff-events.jsonl", "--sleep", "30", "--stderr", "partial err", timeout=1)
    attempt = tmp_path / "work" / "attempt1"
    assert (attempt / "events.jsonl").read_bytes() == (FIX / "handoff-events.jsonl").read_bytes()
    assert (attempt / "stderr.txt").read_text(encoding="utf-8") == "partial err"


def test_invalid_utf8_stdout_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b'{"type":"x","data":{"a":"\xff"}}\n')
    with pytest.raises(CopilotError, match="UTF-8"):
        call("p", cwd=tmp_path, attempt_dir=tmp_path / "a", gh=gh_with("--fixture", str(bad)))


def test_call_saves_events_and_result(tmp_path: Path) -> None:
    r = run(tmp_path, "handoff-events.jsonl")
    saved = tmp_path / "work" / "attempt1" / "events.jsonl"
    assert saved.read_bytes() == (FIX / "handoff-events.jsonl").read_bytes()
    assert r.text == HANDOFF_TEXT
    assert r.seconds > 0


def test_long_prompt_goes_to_file(tmp_path: Path) -> None:
    out = tmp_path / "argv.txt"
    prompt = "x" * 100_000
    work = tmp_path / "work"
    gh = gh_with("--fixture", str(FIX / "handoff-events.jsonl"), "--argv-out", str(out))
    call(prompt, cwd=work, attempt_dir=work / "a1", gh=gh)
    argv = out.read_text(encoding="utf-8").split("\n")
    assert all(len(a) < 1000 for a in argv)
    assert (work / "a1" / "prompt.md").read_text(encoding="utf-8") == prompt
    assert "Read the file a1/prompt.md and follow it exactly." in argv


def test_argv_flags(tmp_path: Path) -> None:
    a = argv_of(tmp_path)
    pairs = list(zip(a, a[1:], strict=False))
    assert a[0] == "--model"
    for flag in ("--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user"):
        assert flag in a
    assert ("--output-format", "json") in pairs
    assert ("--max-ai-credits", "100") in pairs
    assert ("--available-tools", "view,grep,glob") in pairs
    assert ("--model", "auto") in pairs
    assert ("--auto-tier", "intelligence") in pairs
    for tool in ("shell", "write", "url"):
        assert ("--deny-tool", tool) in pairs
    assert "--reasoning-effort" not in a
    assert not any(x.startswith("--allow-all") for x in a)


def test_explicit_model_with_effort(tmp_path: Path) -> None:
    a = argv_of(tmp_path, model="m1", effort="high")
    pairs = list(zip(a, a[1:], strict=False))
    assert ("--model", "m1") in pairs
    assert ("--reasoning-effort", "high") in pairs
    assert "--auto-tier" not in a


def test_child_env_is_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COPILOT_HOME", "COPILOT_PROVIDER_BASE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "1")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("GH_TOKEN", "t")
    out = tmp_path / "env.txt"
    gh = gh_with("--fixture", str(FIX / "handoff-events.jsonl"), "--env-out", str(out))
    call("p", cwd=tmp_path, attempt_dir=tmp_path / "a", gh=gh)
    names = set(out.read_text(encoding="utf-8").split("\n"))
    assert not names & {
        "COPILOT_HOME",
        "COPILOT_PROVIDER_BASE_URL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "COPILOT_ALLOW_ALL",
    }
    assert {"LC_ALL", "GH_TOKEN", "PATH"} <= names
    assert "COPILOT_HOME" not in child_env()


def test_attempt_dir_must_be_inside_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        call("p", cwd=tmp_path / "w", attempt_dir=tmp_path / "other", gh=FAKE)


def test_attempt_dir_dotdot_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="inside"):
        call("p", cwd=work, attempt_dir=work / ".." / "outside", gh=FAKE)
    assert not (tmp_path / "outside").exists()


SYN_BODY = [
    line
    for line in lines("synthetic-forbidden-tool-events.jsonl")
    if line.strip() and json.loads(line)["type"] != "result"
]
SYN_NO_FINAL = [line for line in SYN_BODY if '"content":"done"' not in line]
ERR_EVENT = json.dumps({"type": "session.error", "data": {"message": "boom"}})


@pytest.mark.parametrize(
    "stream",
    [
        [*SYN_BODY, result_line(1)],
        [*SYN_BODY, ERR_EVENT, result_line()],
        [*SYN_NO_FINAL, result_line()],
        ["not json", *SYN_BODY, '{"type":"tool.exec', result_line(0)],
        [*SYN_BODY, FINAL, "[]", result_line()],
    ],
)
def test_sandbox_violation_wins_over_other_parse_failures(stream: list[str]) -> None:
    with pytest.raises(SandboxViolation, match="create"):
        parse_events(stream)


def test_sandbox_violation_wins_over_gh_exit_and_timeout(tmp_path: Path) -> None:
    syn = str(FIX / "synthetic-forbidden-tool-events.jsonl")
    with pytest.raises(SandboxViolation, match="create"):
        call(
            "p",
            cwd=tmp_path,
            attempt_dir=tmp_path / "a",
            gh=gh_with("--fixture", syn, "--exit", "1"),
        )
    with pytest.raises(SandboxViolation, match="create"):
        call(
            "p",
            cwd=tmp_path,
            attempt_dir=tmp_path / "b",
            gh=gh_with("--fixture", syn, "--sleep", "30"),
            timeout=1,
        )
