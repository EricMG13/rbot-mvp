import json
import sys
from pathlib import Path

import pytest

from rbot import __main__ as cli
from rbot.copilot import SandboxViolation
from rbot.engine import ContextError, ModuleFailed, run_case
from rbot.pathways import IMPLEMENTED, PATHWAYS, PROMPTS_DIR

FAKE = str(Path(__file__).parent / "fake_gh.py")
CONTEXT = {
    "run": {"pathway": "credit_snapshot", "as_of": "2025-03-31"},
    "identity": {"issuer_name": "Acme Corp"},
}
VALID = """## Request

Source readiness for CP-1.

## Sources

- a.txt: annual report.

## Findings

The issuer is "Acme Corp annual report" [a.txt p.1], as of 2025-03-31, FY2024.

## Calculations

Not applicable to CP-0.

## Gaps

- None

## Next module

CP-1: read a.txt p.1.
"""
INVALID = "## Request\n\nonly this section\n"


def events(markdown: str, tool: str | None = None) -> str:
    """A minimal valid event stream per docs/copilot-cli.md; `tool` adds a forbidden request."""
    requests: list[dict[str, object]] = [
        {"toolCallId": "t1", "name": tool, "arguments": {}, "type": "function"}
    ]
    lines: list[dict[str, object]] = []
    if tool:
        lines.append(
            {"type": "assistant.message", "data": {"content": "", "toolRequests": requests}}
        )
    final = {"content": markdown, "toolRequests": [], "model": "fake-model"}
    lines.append({"type": "assistant.message", "data": final})
    lines.append({"type": "result", "exitCode": 0, "sessionId": "s"})
    return "".join(json.dumps(line) + "\n" for line in lines)


class Case:
    def __init__(self, root: Path) -> None:
        self.case = root / "case"
        self.case.mkdir()
        (self.case / "context.json").write_text(json.dumps(CONTEXT), encoding="utf-8")
        (self.case / "a.txt").write_text("Acme Corp annual report FY2024", encoding="utf-8")
        self.work = root / "work"
        self.root = root
        self.seq = root / "seq.json"

    def script(self, *streams: str) -> tuple[str, ...]:
        paths = []
        for i, stream in enumerate(streams):
            path = self.root / f"f{i}.jsonl"
            path.write_text(stream, encoding="utf-8", newline="\n")
            paths.append(str(path))
        self.seq.write_text(json.dumps(paths), encoding="utf-8")
        counter = self.seq.with_name("seq.json.n")
        counter.unlink(missing_ok=True)
        return (sys.executable, FAKE, "--sequence", str(self.seq))

    def calls(self) -> int:
        counter = self.seq.with_name("seq.json.n")
        return int(counter.read_text(encoding="utf-8")) if counter.exists() else 0

    def log(self) -> list[dict[str, object]]:
        text = (self.work / "run.log").read_text(encoding="utf-8")
        return [json.loads(x) for x in text.splitlines()]


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return Case(tmp_path)


def test_run_case_writes_handoff_and_report(case: Case) -> None:
    gh = case.script(events(VALID))
    report = run_case(case.case, case.work, gh=gh)
    assert report == case.work / "report.html"
    assert "Acme Corp" in report.read_text(encoding="utf-8")
    assert (case.work / "handoffs" / "CP-0.md").read_text(encoding="utf-8") == VALID
    assert (case.work / "handoffs" / "CP-0.fp").is_file()
    assert (case.work / "sources" / "a.txt").is_file()
    assert not (case.work / "sources" / "context.json").exists()
    assert (case.work / "source-text" / "a.txt" / "p0001.txt").is_file()
    adir = case.work / "attempts" / "CP-0" / "1"
    for name in ("prompt.md", "events.jsonl", "stderr.txt", "handoff.md", "findings.txt"):
        assert (adir / name).is_file()
    prompt = (adir / "prompt.md").read_text(encoding="utf-8")
    assert "source-text/a.txt/" in prompt
    assert "Acme Corp" not in prompt.split("## Source pages")[1]
    (entry,) = case.log()
    assert entry["module"] == "CP-0" and entry["outcome"] == "accepted"
    assert entry["model"] == "fake-model" and entry["requested_model"] == "auto"
    assert {"ts", "attempt", "exit", "seconds", "credits", "findings"} <= entry.keys()


def test_invalid_then_valid_attempt(case: Case) -> None:
    gh = case.script(events(INVALID), events(VALID))
    run_case(case.case, case.work, gh=gh)
    first, second = case.log()
    assert first["outcome"] == "findings" and second["outcome"] == "accepted"
    assert first["attempt"] == 1 and second["attempt"] == 2
    findings = first["findings"]
    assert isinstance(findings, list) and findings
    retry = (case.work / "attempts" / "CP-0" / "2" / "prompt.md").read_text(encoding="utf-8")
    assert all(f in retry for f in findings)
    first_prompt = (case.work / "attempts" / "CP-0" / "1" / "prompt.md").read_text(encoding="utf-8")
    assert retry.startswith(first_prompt)


def test_forbidden_tool_request_is_a_finding(case: Case) -> None:
    gh = case.script(events(VALID, tool="bash"), events(VALID))
    run_case(case.case, case.work, gh=gh)
    first, second = case.log()
    assert first["outcome"] == "findings" and second["outcome"] == "accepted"
    assert "outside the read-only set" in json.dumps(first["findings"])
    assert "bash" in json.dumps(first["findings"])


def test_three_failures_raise(case: Case) -> None:
    gh = case.script(events(INVALID), events(INVALID), events(INVALID), events(VALID))
    with pytest.raises(ModuleFailed) as err:
        run_case(case.case, case.work, gh=gh)
    assert err.value.module == "CP-0" and err.value.findings
    assert case.calls() == 3
    assert not (case.work / "handoffs" / "CP-0.md").exists()
    assert not (case.work / "report.html").exists()


def test_resume_skips_accepted_module(case: Case, tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for p in PROMPTS_DIR.glob("*.md"):
        (prompts / p.name).write_bytes(p.read_bytes())
    gh = case.script(events(VALID), events(VALID))
    run_case(case.case, case.work, gh=gh, prompts=prompts)
    assert case.calls() == 1
    run_case(case.case, case.work, gh=gh, prompts=prompts)
    assert case.calls() == 1
    with (prompts / "CP-0.md").open("a", encoding="utf-8", newline="\n") as f:
        f.write("\nOne more rule.\n")
    run_case(case.case, case.work, gh=gh, prompts=prompts)
    assert case.calls() == 2


def test_model_settings_from_env(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RBOT_MODEL", "some-model")
    monkeypatch.setenv("RBOT_EFFORT", "high")
    monkeypatch.setenv("RBOT_MAX_CREDITS", "7")
    gh = case.script(events(VALID))
    run_case(case.case, case.work, gh=(*gh[:2], "--argv-out", str(case.root / "argv"), *gh[2:]))
    argv = (case.root / "argv").read_text(encoding="utf-8").split("\n")
    assert argv[argv.index("--model") + 1] == "some-model"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert argv[argv.index("--max-ai-credits") + 1] == "7"
    assert case.log()[0]["requested_model"] == "some-model"


@pytest.mark.parametrize("drop", ["run.pathway", "run.as_of", "identity.issuer_name"])
def test_context_requires_keys(case: Case, drop: str) -> None:
    a, b = drop.split(".")
    context = json.loads(json.dumps(CONTEXT))
    del context[a][b]
    (case.case / "context.json").write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(ContextError, match=drop):
        run_case(case.case, case.work, gh=case.script())


def test_context_unreadable_or_unknown_pathway(case: Case) -> None:
    (case.case / "context.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ContextError):
        run_case(case.case, case.work)
    context = {**CONTEXT, "run": {"pathway": "nope", "as_of": "2025-01-01"}}
    (case.case / "context.json").write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(ContextError, match="nope"):
        run_case(case.case, case.work)


def test_bad_max_credits(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RBOT_MAX_CREDITS", "lots")
    with pytest.raises(ContextError, match="RBOT_MAX_CREDITS"):
        run_case(case.case, case.work)


def test_upstream_handoff_is_listed_in_the_next_prompt(case: Case, tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for p in PROMPTS_DIR.glob("*.md"):
        (prompts / p.name).write_bytes(p.read_bytes())
    (prompts / "CP-1.md").write_text("# CP-1 test module\n", encoding="utf-8")
    gh = case.script(events(VALID), events(VALID))
    run_case(case.case, case.work, gh=gh, modules=["CP-0", "CP-1"], prompts=prompts)
    first = (case.work / "attempts" / "CP-0" / "1" / "prompt.md").read_text(encoding="utf-8")
    second = (case.work / "attempts" / "CP-1" / "1" / "prompt.md").read_text(encoding="utf-8")
    assert "handoffs/CP-0.md" not in first
    assert "handoffs/CP-0.md (sha256 " in second


def test_cli_failure_writes_failed_md(case: Case) -> None:
    (case.case / "context.json").unlink()
    assert cli.main(["run", str(case.case), "--out", str(case.work)]) == 1
    failed = (case.work / "FAILED.md").read_text(encoding="utf-8")
    assert "rerun the same command to resume" in failed and "context.json" in failed


def test_cli_module_failure_lists_findings(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> Path:
        raise ModuleFailed("CP-0", ["missing section: ## Gaps"])

    monkeypatch.setattr(cli, "run_case", boom)
    assert cli.main(["run", str(case.case), "--out", str(case.work)]) == 1
    failed = (case.work / "FAILED.md").read_text(encoding="utf-8")
    assert "CP-0" in failed and "- missing section: ## Gaps" in failed
    assert "rerun the same command to resume" in failed


def test_cli_sandbox_violation_message(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> Path:
        raise SandboxViolation("tool outside read-only set ran: bash")

    monkeypatch.setattr(cli, "run_case", boom)
    assert cli.main(["run", str(case.case), "--out", str(case.work)]) == 1
    failed = (case.work / "FAILED.md").read_text(encoding="utf-8")
    assert "sandbox violation — inspect work/ before rerunning" in failed
    assert "rerun the same command" not in failed


def test_cli_success_returns_zero_and_clears_failed(case: Case) -> None:
    case.work.mkdir()
    (case.work / "FAILED.md").write_text("old", encoding="utf-8")
    gh = case.script(events(VALID))
    run_case(case.case, case.work, gh=gh)
    # second run through the CLI resumes from the fingerprint: no gh call is needed
    assert cli.main(["run", str(case.case), "--out", str(case.work)]) == 0
    assert not (case.work / "FAILED.md").exists()


def test_copilot_error_stops_without_retry_and_is_logged(case: Case) -> None:
    from rbot.copilot import CopilotError

    gh = case.script("not json\n", events(VALID))
    with pytest.raises(CopilotError):
        run_case(case.case, case.work, gh=gh)
    assert case.calls() == 1
    (entry,) = case.log()
    assert entry["outcome"] == "error"


def test_sandbox_violation_logged(case: Case) -> None:
    stream = "".join(
        json.dumps(e) + "\n"
        for e in (
            {"type": "tool.execution_start", "data": {"toolCallId": "t", "toolName": "bash"}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "t", "success": True}},
            {"type": "result", "exitCode": 0},
        )
    )
    with pytest.raises(SandboxViolation):
        run_case(case.case, case.work, gh=case.script(stream, events(VALID)))
    assert case.calls() == 1
    assert case.log()[0]["outcome"] == "sandbox"


def test_pathways_match_fw_presets() -> None:
    assert set(PATHWAYS) == {
        "credit_snapshot",
        "earnings_update",
        "relative_value",
        "covenant_refinancing",
        "distressed_restructuring",
        "portfolio_decision",
    }
    assert PATHWAYS["credit_snapshot"] == (
        "CP-0", "CP-1", "CP-MODEL", "CP-1A", "CP-2", "CP-4", "CP-3", "CP-5", "CP-6",
    )  # fmt: skip
    assert all(m[0] == "CP-0" for m in PATHWAYS.values())
    assert IMPLEMENTED == {"CP-0"}
    assert all((PROMPTS_DIR / f"{m}.md").is_file() for m in IMPLEMENTED)
