# RBOT MVP: design

Status: agreed with the owner on 2026-11-09 through a grilling session. Each decision below is
the owner's ruling. Slices 0–2 have an implementation plan
(`docs/superpowers/plans/2026-11-09-rbot-slices-0-2.md`). Later slices get their own plans
when they are reached, written against the code that exists by then.

## What RBOT is

RBOT is a credit-research robot. An analyst puts a case folder (source files plus a
`context.json`) into `inbox/<pathway>/`. RBOT runs that pathway's modules through GitHub
Copilot CLI (`gh copilot`) and writes one offline HTML report to `outbox/<pathway>/<case>/`.

## Decisions

| # | Topic | Ruling |
|---|---|---|
| 1 | Domain | Credit research. |
| 2 | Pathways | All six pathways from final-workflow, with module lists from its `PATHWAY_MODULES` presets: credit_snapshot, earnings_update, relative_value, covenant_refinancing, distressed_restructuring, portfolio_decision. Twenty modules in total. |
| 3 | LLM transport | One `gh copilot -p` call per module attempt. Only Copilot's built-in read-only tools (view, grep, glob or their real names, pinned by the slice-0 probe) are available, over the case's extracted source text and upstream handoffs. Shell, write and URL tools are unavailable. Copilot runs the tool loop. Nothing from final-workflow's ToolLoop, providers, transport or Copilot SDK is ported. |
| 4 | Historical numbers | CP-1 ends its handoff with one fenced `json` block of inputs, each sourced or derived. Python computes the ratios with `Decimal` into `calculation.json`. |
| 5 | Deliverable | One static `report.html` per case. No JavaScript (CSP `script-src 'none'`). It has a cover, a key-metrics table, a section per module, the model and forecast, sources and gaps, and SVG charts. |
| 6 | Forecast | CP-MODEL emits base and downside assumptions as a JSON block. Python projects 3 years into `forecast.json`. The projection maths is repurposed from final-workflow CP-2G (`owners/cp2g.py`); historical metrics come from `model/metrics.py` `_METRICS`. |
| 7 | Inputs | Case folder = sources (pdf, htm/html, txt, md, xlsx, csv) + `context.json`. Other files are skipped and logged. |
| 8 | Pickup | `rbot watch` polls `inbox/` every 10 s. A case is ready when `context.json` is present and its file list, sizes and mtimes are unchanged across two polls. It is claimed with `os.replace` into `work/<pathway>/<case>/`, run one case at a time, and resumed from `work/` after a restart. Output goes to `outbox/<pathway>/<case>/`, or `FAILED.md` on failure. `rbot run <case>` uses the same code. |
| 9 | Context | Full `context/1`: the ported `docs/templates/context-form.html` with its pathway selector, and the ported `context.py` validation (pydantic). `run.pathway` must equal the inbox folder. |
| 10 | Module output | Markdown handoffs with fixed H2 sections `Request`, `Sources`, `Findings`, `Calculations`, `Gaps`, `Next module`. Every quotation must appear verbatim, after normalisation, on the page it cites (`[<source> p.N]`). Figures that come from computed JSON must match it. |
| 11 | Model and retry | Owner chose `gpt-6.1-sol` at `medium`. Ruling 2026-11-09: this Copilot account rejects every explicit `--model` (including gpt-6.1-sol), so the default is `auto` with `--auto-tier intelligence` and no effort flag; `RBOT_MODEL`/`RBOT_EFFORT` override when a model becomes available. A handoff that fails validation gets up to 3 attempts in total, each retry carrying the checker's findings. A CLI failure (non-zero exit, rate limit, auth, timeout) is never retried: the case fails, and a restart resumes it. 30-minute timeout per call. Every call is logged in `run.log`. |
| 12 | Repo | `EricMG13/rbot-mvp`, public, MIT, local `~/Documents/rbot-mvp`. |
| 13 | Brand | "RBOT" wordmark in Red Hat Display. A gitignored local `brand/logo.svg` or `brand/logo.png` is inlined when present. Third-party logos from final-workflow (`ssim-logo…png`, `symbol.svg`) are never committed. |
| 14 | Platforms | macOS and Windows on one machine, with `inbox/`, `work/` and `outbox/` on one local disk. CI runs on `macos-latest` and `windows-latest`. |
| 16 | Module numbers | Every quantitative module emits a JSON block of inputs and judgments. A per-module `Decimal` calculator, ported as a math core from the matching `owners/cp*.py`, writes `<module>.json`. |
| 18 | Extra inputs | CP-8 reads `prior/` (a copy of an earlier outbox folder) inside the case when `attachments.prior_case` is true. The management bridge and portfolio workbook are files in the case folder named in the context. A mismatch fails the case when it is claimed. |
| 19 | Acceptance | Each calculator matches final-workflow's frozen oracle (`cases/<module>-owner/expected.json`); the controller translates oracle inputs, the implementer never edits them. Each of the six pathways passes an offline end-to-end test with a fake `gh`. Live runs pass on TDG FY2024 (credit_snapshot), ARCC FY2024 (relative_value) and CZR 2026Q2 (earnings_update). |

Decision 15 was folded into 19.

## Standing rules (owner)

- **Reuse first.** Copy and trim from final-workflow at commit
  `fda70c29296797766ecfd52908b57740eb3bafef` (public repo `EricMG13/final-workflow`) or from
  Final-workflow-PY where named. Every copied file is listed in `tools/copied-from.txt`. New
  code only where the source is too entangled with domain code to trim.
- **Small, provable, live-tested wins.** Each slice is one PR of at most 800 changed lines
  (copies listed in `copied-from.txt` excepted). It ends with a live run on a real case, and
  the run's `run.log` and output summary go in the PR description. Live runs set
  `--max-ai-credits`.
- **Keep the repo clean.** Nothing from `runs/`, `cases/` (except oracle figures under
  `tests/oracles/`, with provenance), `deliverables/`, `apps/` or worktrees. Test fixtures are
  synthetic. Live cases live outside the repo.

## Layout

```
inbox/<pathway>/<case>/      user drops: sources + context.json [+ prior/]
work/<pathway>/<case>/       claimed case
  sources/                   the user's files, untouched
  source-text/<source>/p0001.txt   one UTF-8 file per page
  handoffs/<MODULE>.md       accepted handoffs (<MODULE>.fp beside each: prompt fingerprint)
  attempts/<MODULE>/<n>/{prompt.md,events.jsonl,stderr.txt,handoff.md,findings.txt}
  calculation.json  forecast.json  <module>.json
  run.log
outbox/<pathway>/<case>/     report.html, handoffs/, *.json, run.log, sources/ | FAILED.md
```

## Slices

| # | Slice | Live proof |
|---|---|---|
| 0 | Repo, gates, CI (macOS and Windows), Copilot probe pinning the event schema and the read-only tool names | One probe call |
| 1 | Source readers, CP-0 via `gh copilot`, handoff checks, minimal `report.html` | CP-0 on TDG |
| 2 | CP-1, JSON block, `calculation.json`, key-metrics table, leverage chart | TDG ratios match the answer key |
| 3 | Watcher, pathway folders, `context.py` validation, ported form | TDG dropped in inbox appears in outbox |
| 4 | CP-MODEL, `forecast.json`, forecast charts | TDG forecast |
| 5–6 | CP-1A, CP-2, CP-4, CP-3, CP-5, CP-6 | Full TDG snapshot |
| 7 | earnings_update (CP-1B, CP-1D) | CZR 2026Q2 |
| 8–9 | relative_value (CP-1C, CP-2D, CP-3C, CP-3D) | ARCC FY2024 |
| 10+ | covenant_refinancing, distressed_restructuring (CP-2A, CP-2G, CP-2H, CP-4C), portfolio_decision (CP-8) | Offline oracles; live when the owner has a case |

## Reuse map (final-workflow paths)

| RBOT need | Source |
|---|---|
| HTML to text | `src/deploy_workflow/html_text.py` (stdlib, no domain imports) |
| PDF pages | `src/deploy_workflow/model/handoff.py` `_pages` (pypdf `extraction_mode="layout"`) |
| XLSX | `src/deploy_vii/preparation.py` `xlsx_content` and helpers (stdlib) |
| Page sizes | `cpos_launch.py`: HTML 3,000 chars, other text 12,000 chars |
| Design tokens and fonts | `src/deploy_vii/assets/tokens.css`, `assets/fonts/*` (OFL) |
| Charts | `src/deploy_vii/charts.py` bar and line core (`_line`, `_columns`, helpers, `render_svg`) |
| Context | `src/deploy_workflow/context.py`, `docs/templates/context-form.html` |
| Module prompts | `src/deploy_vii/methods/*.md`, `prompts/CP-*.md`, Final-workflow-PY `src/deploy_workflow/prompts/texts/*.md` |
| Calculators | `src/deploy_workflow/owners/cp*.py` (math only), `model/metrics.py` |
| Oracles | `cases/<module>-owner/{expected,submissions,manifest}.json` |
| Gates | Final-workflow-PY `tools/pr_size.py`; final-workflow `tools/ruff-gate.toml` limits |

## Known risks

- Rewriting typed-JSON methods into Markdown prompts can lose analytical depth. Each module
  slice compares its live handoff with the method's analytical rules.
- Copilot CLI's built-in tool names, JSON event schema, path sandbox and stdin prompt input are
  unverified until the slice-0 probe. On Windows a command line is capped at 32,767 characters,
  so prompts go through stdin or a file, never `-p` text alone.
