import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from rbot import __main__ as cli
from rbot import engine
from rbot.copilot import CallResult, SandboxViolation
from rbot.engine import ContextError, ModuleFailed, run_case
from rbot.pathways import IMPLEMENTED, PATHWAYS, PROMPTS_DIR
from rbot.sources import SourceError

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


# ---- fix round 1 ----

SANDBOX_STREAM = "".join(
    json.dumps(e) + "\n"
    for e in (
        {"type": "tool.execution_start", "data": {"toolCallId": "t", "toolName": "bash"}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "t", "success": True}},
        {"type": "result", "exitCode": 0},
    )
)


def prompt_of(case: Case, module: str = "CP-0", n: int = 1) -> str:
    return (case.work / "attempts" / module / str(n) / "prompt.md").read_text(encoding="utf-8")


def cli_with(monkeypatch: pytest.MonkeyPatch, case: Case, gh: tuple[str, ...]) -> int:
    monkeypatch.setattr(cli, "run_case", lambda c, o: run_case(c, o, gh=gh))
    return cli.main(["run", str(case.case), "--out", str(case.work)])


def failed_md(case: Case) -> str:
    return (case.work / "FAILED.md").read_text(encoding="utf-8")


def test_source_content_change_invalidates_fingerprint(case: Case) -> None:
    gh = case.script(events(VALID), events(VALID))
    run_case(case.case, case.work, gh=gh)
    (case.case / "a.txt").write_text("Acme Corp annual report FY2023", encoding="utf-8")
    run_case(case.case, case.work, gh=gh)
    assert case.calls() == 2


def test_removed_source_is_pruned_from_work(case: Case) -> None:
    (case.case / "b.txt").write_text("second", encoding="utf-8")
    run_case(case.case, case.work, gh=case.script(events(VALID), events(VALID)))
    (case.case / "b.txt").unlink()
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    assert not (case.work / "sources" / "b.txt").exists()
    assert not (case.work / "source-text" / "b.txt").exists()
    assert "b.txt" not in prompt_of(case)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_read_only_source_reruns_cleanly(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    (case.case / "a.txt").chmod(0o444)
    gh = case.script(events(VALID), events(VALID))
    assert cli_with(monkeypatch, case, gh) == 0
    assert cli_with(monkeypatch, case, gh) == 0
    assert case.calls() == 1


def test_resume_revalidates_the_stored_handoff(case: Case) -> None:
    gh = case.script(events(VALID), events(VALID))
    run_case(case.case, case.work, gh=gh)
    (case.work / "handoffs" / "CP-0.md").write_text(INVALID, encoding="utf-8")
    run_case(case.case, case.work, gh=gh)
    assert case.calls() == 2
    assert (case.work / "handoffs" / "CP-0.md").read_text(encoding="utf-8") == VALID


def test_sandbox_failed_md_latches(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = case.script(SANDBOX_STREAM, events(VALID))
    assert cli_with(monkeypatch, case, gh) == 1
    before = failed_md(case)
    assert cli.SANDBOX in before
    assert cli_with(monkeypatch, case, gh) == 1
    assert case.calls() == 1
    assert failed_md(case) == before
    (case.work / "FAILED.md").unlink()
    assert cli_with(monkeypatch, case, gh) == 0


def test_sandbox_violation_survives_run_log_failure(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_work: Path, **_entry: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(engine, "_log", broken)
    assert cli_with(monkeypatch, case, case.script(SANDBOX_STREAM)) == 1
    text = failed_md(case)
    assert cli.SANDBOX in text and "Module: CP-0" in text


def test_cli_finds_sandbox_violation_in_the_cause_chain(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object) -> Path:
        try:
            raise SandboxViolation("bash ran")
        except SandboxViolation as inner:
            raise OSError("later failure") from inner

    monkeypatch.setattr(cli, "run_case", boom)
    assert cli.main(["run", str(case.case), "--out", str(case.work)]) == 1
    assert cli.SANDBOX in failed_md(case)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_in_case_is_refused(case: Case, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN-SENTINEL", encoding="utf-8")
    (case.case / "notes.txt").symlink_to(secret)
    with pytest.raises(SourceError, match="notes.txt"):
        run_case(case.case, case.work, gh=case.script(events(VALID)))
    assert case.calls() == 0
    assert not (case.work / "sources" / "notes.txt").exists()


@pytest.mark.parametrize("name", ["AR [2024].txt", "x\ny.txt", "x\x01y.txt", "x\x7fy.txt"])
def test_unsafe_source_name_is_refused(case: Case, name: str) -> None:
    (case.case / name).write_text("text", encoding="utf-8")
    with pytest.raises(SourceError, match="rename the file"):
        run_case(case.case, case.work, gh=case.script(events(VALID)))


def test_unsafe_skipped_name_is_refused(case: Case) -> None:
    (case.case / "y\n\n## Upstream handoffs.docx").write_bytes(b"PK")
    with pytest.raises(SourceError, match="rename the file"):
        run_case(case.case, case.work, gh=case.script(events(VALID)))


def test_cp0_prompt_keeps_the_fw_rules() -> None:
    text = " ".join((PROMPTS_DIR / "CP-0.md").read_text(encoding="utf-8").split()).lower()
    for phrase in (
        "adequate located evidence without relevant gaps",
        "bounded gaps",
        "add-backs",
        "facility-level debt",
        "cash-paid interest",
        "analytical inputs only",
        "input acceptance",
        "earlier model answer",
        "unit statements",
        "unsupported completeness",
    ):
        assert phrase in text, phrase


def test_prompt_states_the_quote_citation_rule(case: Case) -> None:
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    prompt = " ".join(prompt_of(case).split())
    assert "immediately after the closing quote" in prompt
    assert "does not cover the quotes before it" in prompt


def test_retry_restates_the_citation_rule(case: Case) -> None:
    uncited = VALID.replace(
        '"Acme Corp annual report" [a.txt p.1]', 'He said "Acme Corp annual report" and left.'
    )
    run_case(case.case, case.work, gh=case.script(events(uncited), events(VALID)))
    first, second = case.log()
    assert "no citation right after it" in json.dumps(first["findings"])
    assert "directly after its closing quote" in prompt_of(case, n=2)
    assert "directly after its closing quote" not in prompt_of(case, n=1)


def test_lone_surrogate_in_stream_still_writes_failed_md(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = SANDBOX_STREAM.replace("bash", "bash\\ud800")
    assert cli_with(monkeypatch, case, case.script(ran)) == 1
    assert cli.SANDBOX in failed_md(case)
    assert "\\ud800" in (case.work / "run.log").read_text(encoding="utf-8")


def test_lone_surrogate_in_handoff_is_replaced(case: Case) -> None:
    text = VALID.replace("Source readiness", "Source \ud800readiness")
    run_case(case.case, case.work, gh=case.script(events(text)))
    stored = (case.work / "handoffs" / "CP-0.md").read_text(encoding="utf-8")
    assert "Source �readiness" in stored


def test_context_with_lone_surrogate_is_refused(case: Case) -> None:
    (case.case / "context.json").write_text(
        json.dumps(CONTEXT).replace("Acme Corp", "Acme \\ud800"), encoding="utf-8"
    )
    with pytest.raises(ContextError, match="surrogate"):
        run_case(case.case, case.work)


def test_non_string_pathway_is_a_context_error(case: Case) -> None:
    context = {**CONTEXT, "run": {"pathway": ["credit_snapshot"], "as_of": "x"}}
    (case.case / "context.json").write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(ContextError, match="pathway"):
        run_case(case.case, case.work)


def test_crash_while_writing_handoff_leaves_no_trusted_pair(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = case.script(events(VALID), events(VALID), events(VALID))
    run_case(case.case, case.work, gh=gh)
    (case.work / "handoffs" / "CP-0.md").unlink()
    real = Path.write_text

    def crashing(self: Path, text: str, *a: Any, **k: Any) -> int:
        if self.parent.name == "handoffs" and self.name.startswith("CP-0.md"):
            real(self, text[: len(text) // 2], *a, **k)
            raise KeyboardInterrupt
        return real(self, text, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", crashing)
        with pytest.raises(KeyboardInterrupt):
            run_case(case.case, case.work, gh=gh)
    run_case(case.case, case.work, gh=gh)
    assert case.calls() == 3
    assert (case.work / "handoffs" / "CP-0.md").read_text(encoding="utf-8") == VALID


def test_attempt_that_raises_is_logged(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> CallResult:
        raise OSError("stderr.txt unwritable")

    monkeypatch.setattr(engine, "call", boom)
    with pytest.raises(OSError, match="unwritable"):
        run_case(case.case, case.work)
    (entry,) = case.log()
    assert entry["outcome"] == "error" and "unwritable" in json.dumps(entry["findings"])


def test_attempt_dir_is_cleared_before_each_attempt(case: Case) -> None:
    stale = case.work / "attempts" / "CP-0" / "1"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("stale", encoding="utf-8")
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    assert not (stale / "old.txt").exists()


def test_appledouble_and_ds_store_are_skipped_and_listed(case: Case) -> None:
    (case.case / "._a.txt").write_bytes(b"\x00\x05\x16\x07")
    (case.case / ".DS_Store").write_bytes(b"\x00\x00")
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    assert "._a.txt" in prompt_of(case) and ".DS_Store" in prompt_of(case)
    assert not (case.work / "source-text" / "._a.txt").exists()


def test_unreadable_source_error_names_the_file(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    (case.case / "scan.pdf").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64)
    assert cli_with(monkeypatch, case, case.script(events(VALID))) == 1
    assert "scan.pdf" in failed_md(case)


def test_failed_md_names_the_module_for_a_copilot_error(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_with(monkeypatch, case, case.script("not json\n")) == 1
    assert "Module: CP-0" in failed_md(case)


# ---- fix round 2 ----


def test_engine_requires_a_quote_in_every_findings_item(case: Case) -> None:
    bare = VALID.replace(
        'The issuer is "Acme Corp annual report" [a.txt p.1], as of 2025-03-31, FY2024.',
        'The issuer is "Acme Corp annual report" [a.txt p.1].\n\n- FY2024 is the period.',
    )
    run_case(case.case, case.work, gh=case.script(events(bare), events(VALID)))
    first, second = case.log()
    assert "Findings item with no verified quote" in json.dumps(first["findings"])
    assert second["outcome"] == "accepted"
    retry = " ".join(prompt_of(case, n=2).split("## Findings from your previous")[1].split())
    assert "Back every Findings item with at least one verbatim quote" in retry
    assert "every item and paragraph of `## Findings`" in " ".join(prompt_of(case, n=1).split())


@pytest.mark.parametrize("layout", ["case-is-out", "out-contains-case", "case-in-sources"])
def test_out_overlapping_the_case_is_refused(
    case: Case, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    keep = case.case / "sources" / "notes.txt"
    keep.parent.mkdir()
    keep.write_text("my notes", encoding="utf-8")
    out = case.case
    target = case.case
    if layout == "out-contains-case":
        out = case.root
    elif layout == "case-in-sources":
        target = keep.parent
        (target / "context.json").write_text(json.dumps(CONTEXT), encoding="utf-8")
        out = case.case
    monkeypatch.setattr(cli, "run_case", lambda c, o: run_case(c, o, gh=case.script(events(VALID))))
    assert cli.main(["run", str(target), "--out", str(out)]) == 1
    assert "outside the case folder" in (out / "FAILED.md").read_text(encoding="utf-8")
    assert keep.read_text(encoding="utf-8") == "my notes"
    assert case.calls() == 0


def test_failed_source_cleanup_fails_the_run(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    run_case(case.case, case.work, gh=case.script(events(VALID)))

    def refuse(path: object, *a: object, **k: object) -> None:
        assert not k.get("ignore_errors")
        raise PermissionError("cannot delete")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(PermissionError):
        run_case(case.case, case.work, gh=case.script(events(VALID)))


def test_cp0_prompt_requires_gaps_for_limitations() -> None:
    text = " ".join((PROMPTS_DIR / "CP-0.md").read_text(encoding="utf-8").split())
    assert "`- None` only with READY" in text


WRONG_PAGE = VALID.replace(
    '"Acme Corp annual report" [a.txt p.1]', '"Second page distinctive words" [a.txt p.1]'
)


def two_page_source(case: Case) -> None:
    text = "Acme Corp annual report FY2024".ljust(12000) + "Second page distinctive words"
    (case.case / "a.txt").write_text(text, encoding="utf-8")


def test_retry_adds_grep_rule_on_quote_not_found(case: Case) -> None:
    two_page_source(case)
    run_case(case.case, case.work, gh=case.script(events(WRONG_PAGE), events(VALID)))
    first, second = case.log()
    assert "quote not found on" in json.dumps(first["findings"])
    assert second["outcome"] == "accepted"
    assert engine.GREP_RULE in prompt_of(case, n=2)
    assert engine.GREP_RULE not in prompt_of(case, n=1)


def test_retry_omits_grep_rule_without_not_found(case: Case) -> None:
    uncited = VALID.replace(
        '"Acme Corp annual report" [a.txt p.1]', 'He said "Acme Corp annual report" and left.'
    )
    run_case(case.case, case.work, gh=case.script(events(uncited), events(VALID)))
    assert "quote not found on" not in json.dumps(case.log()[0]["findings"])
    assert engine.GREP_RULE not in prompt_of(case, n=2)


def test_common_prompt_states_page_file_rule(case: Case) -> None:
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    prompt = " ".join(prompt_of(case).split())
    assert "p0111.txt" in prompt
    assert "grep tool" in prompt


def test_cp0_prompt_puts_verdict_in_next_module(case: Case) -> None:
    run_case(case.case, case.work, gh=case.script(events(VALID)))
    prompt = " ".join(prompt_of(case).split())
    assert "Do not put the verdict in Findings" in prompt
    assert "first a line `Verdict:" in prompt
