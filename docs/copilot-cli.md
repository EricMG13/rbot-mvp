# Copilot CLI, pinned from live runs (2026-11-09)

Tool: `gh` 2.101 running Copilot CLI 1.0.105. Probe: `python3 tools/copilot_probe.py --out DIR [--model M] [--effort E] [--available-tools a,b] [--only n,...]`.
Fixtures: `tests/fixtures/copilot/handoff-events.jsonl`, `write-attempt-events.jsonl` (paths and user name redacted; the opaque `apiCallId` blobs also redacted because gitleaks flags them).

## Deviation from the plan: the pinned model is not available

`--model gpt-6.1-sol` exits 1 with no stdout: stderr is `Error: Model "gpt-6.1-sol" from --model flag is not available.`
`--model auto --reasoning-effort medium` also exits 1: `Error: Model "auto" does not support reasoning effort configuration (requested: "medium").`
All fixtures below were produced with `--model auto` and no `--reasoning-effort`. Auto routed to `gpt-6-luna` (`mai-code-1.1-flash` on one call); `availableModels` in the plan was a single model each time. The default model and effort in the spec need an owner decision.

## (a) Working argv

```
gh copilot -- --model auto --output-format json --no-custom-instructions --disable-builtin-mcps \
  --no-ask-user --no-auto-update --max-ai-credits 30 --disallow-temp-dir -s \
  --available-tools view,grep,glob -p "<text>"      # cwd = work folder
```
`--` is not needed: the same flags without it (`gh copilot --model auto ... -p "Reply with the single word ok."`) exited 0 with a normal stream. The probe keeps `--` because `gh copilot --help` recommends it. Stdout is JSONL (one event per line); stderr carries nothing on success. `--disallow-temp-dir` was added because the scratch folder lives in the system temp dir, which Copilot allows by default (without it `../outside.txt` would be in an allowed path and (e) would prove nothing).
Prompt-file handoff works: `-p "Read the file prompt.md in the current folder with your read tool and follow its instructions."` made the model `view` prompt.md, then `view` a.txt, then answer `## Quote\n\nalpha beta gamma\n\n## Summary\n...` (the handoff fixture).

## (b) Built-in tool names

Unrestricted `tools` call lists 23 names: `bash read_bash stop_bash list_bash view create edit web_fetch fetch_copilot_cli_documentation search_code_subagent semantic_code_search skill run_dynamic_workflow dynamic_workflows_manage sql session_store_sql read_agent list_agents write_agent grep glob task parallel` (the model's own listing; `search_code_subagent` and `parallel` are not in the "Disabled tools" list below, so they are model-visible names, not confirmed tools).
Read-only: `view`, `grep`, `glob`. `--available-tools view,grep,glob` works: the stream's `session.info` event says `Disabled tools: bash, create, dynamic_workflows_manage, edit, fetch_copilot_cli_documentation, list_agents, list_bash, read_agent, read_bash, run_dynamic_workflow, semantic_code_search, session_store_sql, skill, sql, stop_bash, task, web_fetch, write_agent`. (I did not re-run the `tools` listing with the restriction on; the evidence is the `session.info` event and (e).)

## (c) Event types and fields

Every line: `{"type", "data", "id", "timestamp", "parentId", "ephemeral"?}`; the last line has no `data`.
- Setup: `session.skills_loaded`, `session.mcp_servers_loaded`, `session.tools_updated`, `session.info` (`data.infoType`, `data.message`), `session.auto_mode_resolved` (`data.chosenModel`).
- `user.message`: `data.content`.
- Assistant message: `assistant.message` with `data.content` (string, may be empty when `toolRequests` is non-empty), `data.model`, `data.messageId`, `data.turnId`, `data.phase` (`final_answer` seen on streaming start), `data.toolRequests[]`. Streaming: `assistant.message_start`, `assistant.message_delta` (`data.deltaContent`, ephemeral), `assistant.tool_call_delta` (`toolCallId`, `toolName`, `inputDelta`). Also `assistant.reasoning`, `assistant.turn_start|turn_end`, `assistant.idle`.
- Tool call: `data.toolRequests[] = {toolCallId, name, arguments, type:"function", intentionSummary, toolTitle}` inside `assistant.message`; then `tool.execution_start` (`toolCallId`, `toolName`, `arguments`) and `tool.execution_complete` (`toolCallId`, `success`, `result.content` on success, `error.message` and `error.code` on failure, e.g. `"denied"`).
- Usage/credits: `session.usage_checkpoint` (`data.totalNanoAiu`, `data.totalPremiumRequests`, `data.accountingSnapshot.{totalNanoAiu, aiCreditsStatus, tokenDetails.{input,output,cache_read,cache_write}.tokenCount}`); final line `{"type":"result","sessionId","exitCode","usage":{"premiumRequests","totalApiDurationMs","sessionDurationMs","codeChanges"}}`.
- Errors: the two startup errors seen (unavailable model; effort on auto) produced NO JSON error event: exit code 1, plain text `Error: ...` on stderr (a session.skills_loaded line may be on stdout). A failed tool call is a `tool.execution_complete` with `success:false`. Not observed: credit-limit exhaustion, network failure, timeout.

## (d) Final assistant message

Take the last `assistant.message` event whose `data.toolRequests` is empty; its `data.content` is the answer. In the handoff fixture there are 3 `assistant.message` events (turn 0 and 1 have tool requests and empty content; turn 2 is the answer). Confirm success with the last line `type == "result"` and `exitCode == 0`. `assistant.message_delta` events are only streaming fragments of the same text.

## (e) Tool outside `--available-tools` / path outside cwd

Safety stop NOT triggered: `b.txt` was not created and the outside contents were not read.
- write-attempt (`-p "Create a new file b.txt in the current folder containing the word hello."` with `--available-tools view,grep,glob`): the model made no tool call and answered "I couldn't create `b.txt`: no file-writing tool is available in this session." A disabled tool is invisible to the model, so there is no tool error event. b.txt absent (probe prints `b.txt created: False`).
- escape-attempt (`../outside.txt`, file contained `SECRET-OUTSIDE`): the model called `view` on the resolved absolute path; `tool.execution_complete` had `success:false`, `error.code:"denied"`, message "Permission denied because no interactive user response was available..."; final answer "I couldn't read `../outside.txt`; access was denied." The secret did not appear in the stream. This relies on `--no-ask-user` and no `--allow-all*`/`--allow-all-paths`.

## (f) Credits for small calls

`totalNanoAiu` per call: tools 319,340,000 (1 premium request, long listing, first call); handoff 67,078,000 (3 turns); write-attempt 37,432,000; escape-attempt 46,521,500; trivial "ok" call 24,594,500. Each reports `totalPremiumRequests: 1`. If 1 AIU = 1e9 nanoAIU these are 0.02-0.32 AIU, far below `--max-ai-credits 30`; the unit mapping to `--max-ai-credits` is not verified. Each call took about 6 s wall time.
