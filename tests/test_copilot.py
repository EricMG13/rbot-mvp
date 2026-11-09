"""Tests for rbot.copilot.

synthetic-forbidden-tool-events.jsonl is SYNTHETIC (hand-built, not a live capture): the committed
write-attempt stream has no tool events. escape-attempt-events.jsonl is a real redacted capture.
"""

import json
import sys
from pathlib import Path

import pytest

from rbot.copilot import CallResult, CopilotError, call, parse_events

FIX = Path(__file__).parent / "fixtures" / "copilot"
FAKE = (sys.executable, str(Path(__file__).parent / "fake_gh.py"))


def lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str | None,
    exit_code: int = 0,
    **kw: object,
) -> CallResult:
    if fixture:
        monkeypatch.setenv("FAKE_GH_FIXTURE", str(FIX / fixture))
    monkeypatch.setenv("FAKE_GH_EXIT", str(exit_code))
    attempt = tmp_path / "work" / "attempt1"
    return call("hello", cwd=tmp_path / "work", attempt_dir=attempt, gh=FAKE, **kw)  # type: ignore[arg-type]


def final_text(name: str) -> str:
    msgs: list[str] = [
        e["data"]["content"]
        for e in map(json.loads, lines(name))
        if e["type"] == "assistant.message" and not e["data"]["toolRequests"]
    ]
    return msgs[-1]


def test_parse_handoff_fixture() -> None:
    r = parse_events(lines("handoff-events.jsonl"))
    assert r.text == final_text("handoff-events.jsonl")
    assert r.text.startswith("## Quote")
    assert r.exit_code == 0
    assert r.forbidden_tools == ()
    assert r.denied == ()
    assert r.model
    assert r.credits is not None


def test_write_tool_event_fails_attempt() -> None:
    r = parse_events(lines("synthetic-forbidden-tool-events.jsonl"))
    assert r.forbidden_tools == ("create",)
    assert r.model == "synthetic-model"


def test_write_attempt_fixture_has_no_forbidden_tools() -> None:
    assert parse_events(lines("write-attempt-events.jsonl")).forbidden_tools == ()


def test_denied_read_is_reported() -> None:
    r = parse_events(lines("escape-attempt-events.jsonl"))
    assert r.denied
    assert "view" in r.denied
    assert "bash" in r.forbidden_tools


def test_missing_result_line_raises() -> None:
    with pytest.raises(CopilotError, match="result"):
        parse_events(
            line for line in lines("handoff-events.jsonl") if '"type":"result"' not in line
        )


def test_result_nonzero_exit_raises() -> None:
    bad = [
        line.replace('"exitCode":0', '"exitCode":2') if '"type":"result"' in line else line
        for line in lines("handoff-events.jsonl")
    ]
    with pytest.raises(CopilotError, match="exit 2"):
        parse_events(bad)


def test_nonzero_exit_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_STDERR", 'Error: Model "x" is not available. ' + "y" * 600)
    with pytest.raises(CopilotError, match="exit 1") as ei:
        run(tmp_path, monkeypatch, None, exit_code=1)
    assert 'Model "x" is not available.' in str(ei.value)
    assert len(str(ei.value)) < 700


def test_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_SLEEP", "5")
    with pytest.raises(CopilotError, match=r"timeout after 0\.5 s"):
        run(tmp_path, monkeypatch, "handoff-events.jsonl", timeout=0.5)


def test_call_saves_events_and_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = run(tmp_path, monkeypatch, "handoff-events.jsonl")
    saved = tmp_path / "work" / "attempt1" / "events.jsonl"
    assert saved.read_bytes() == (FIX / "handoff-events.jsonl").read_bytes()
    assert r.text == final_text("handoff-events.jsonl")


def test_long_prompt_goes_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_GH_ARGV", str(argv_file))
    monkeypatch.setenv("FAKE_GH_FIXTURE", str(FIX / "handoff-events.jsonl"))
    prompt = "x" * 100_000
    attempt = tmp_path / "work" / "a1"
    call(prompt, cwd=tmp_path / "work", attempt_dir=attempt, gh=FAKE)
    argv = argv_file.read_text(encoding="utf-8").split("\n")
    assert all(len(a) < 1000 for a in argv)
    assert (attempt / "prompt.md").read_text(encoding="utf-8") == prompt
    assert "Read the file a1/prompt.md and follow it exactly." in argv


def test_argv_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_GH_ARGV", str(argv_file))
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "1")
    monkeypatch.setenv("FAKE_GH_FIXTURE", str(FIX / "handoff-events.jsonl"))
    call("p", cwd=tmp_path, attempt_dir=tmp_path / "a", gh=FAKE)
    a = argv_file.read_text(encoding="utf-8").split("\n")
    pairs = list(zip(a, a[1:], strict=False))
    assert a[0] == "copilot"
    for flag in ("--available-tools", "--no-custom-instructions", "--disable-builtin-mcps"):
        assert flag in a
    assert ("--output-format", "json") in pairs
    assert ("--max-ai-credits", "100") in pairs
    assert ("--available-tools", "view,grep,glob") in pairs
    assert ("--model", "auto") in pairs
    assert ("--auto-tier", "intelligence") in pairs
    assert "--reasoning-effort" not in a
    assert not any(x.startswith("--allow-all") for x in a)


def test_explicit_model_with_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_GH_ARGV", str(argv_file))
    monkeypatch.setenv("FAKE_GH_FIXTURE", str(FIX / "handoff-events.jsonl"))
    call("p", cwd=tmp_path, attempt_dir=tmp_path / "a", gh=FAKE, model="m1", effort="high")
    a = argv_file.read_text(encoding="utf-8").split("\n")
    assert ("--model", "m1") in list(zip(a, a[1:], strict=False))
    assert ("--reasoning-effort", "high") in list(zip(a, a[1:], strict=False))
    assert "--auto-tier" not in a


def test_allow_all_env_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "1")
    monkeypatch.setenv("FAKE_GH_ENV_OUT", str(tmp_path / "env.txt"))
    monkeypatch.setenv("FAKE_GH_FIXTURE", str(FIX / "handoff-events.jsonl"))
    call("p", cwd=tmp_path, attempt_dir=tmp_path / "a", gh=FAKE)
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "absent"


def test_attempt_dir_must_be_inside_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        call("p", cwd=tmp_path / "w", attempt_dir=tmp_path / "other", gh=FAKE)
