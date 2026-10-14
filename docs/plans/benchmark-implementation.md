# CodeLoom benchmark implementation plan

Date: 14 October 2026. Status: implementation proposal; no benchmark has been run.

## Outcome and scope

Build `codeloom.benchmark` as a new submodule under `workspace/benchmark`. Release a versioned 16-task suite across five small repositories, compare an existing minimal agent (mini-SWE-agent) with one fixed engine configuration under a $50 total budget. Run zero component ablations in v1. Ship a single reproduction command, machine-readable results, a comparison table, downloadable traces and patches, a short findings report, and a redesigned `/engine/results` page. Do not implement a second coding-agent loop inside the benchmark.

The benchmark measures independently verified outcomes. Agent assertions, review-model opinions, and PR creation do not determine success. Failures remain visible. The current three website reports remain available as historical case studies and are excluded from benchmark aggregates.

Review revision: use **16 tasks × two existing systems × one run = 32 measured trials**, with **no repeat slice in v1**. Allocate **$8 to live development/calibration**, **$32 to measured trials ($1 per system/task)**, and **$5 to infrastructure recovery**: **$45 API maximum plus $5 payment headroom = $50 total**. Calibrate on two full-size development tasks outside the measured set, using both systems. Determine shared timeouts from observed throughput and duration before freezing the campaign. This supersedes the earlier 20-task/$0.80/20-minute proposal.

This document plans the work; it does not create GitHub repositories, run paid evaluations, open PRs, or implement the page.

## Existing code to reuse

| Area | Existing location | Implementation consequence |
| --- | --- | --- |
| Sub-repo creation | `scripts/new`, `scripts/sync-workspace` | Use the existing workflow. There is no `scripts/new.sh` in this checkout. |
| Engine transport | `workspace/engine/runtime/server.py`, `protocol/commands.py`, `protocol/events.py` | Drive the full engine through its NDJSON Unix socket, rather than depending on agent internals in the harness. |
| Runtime configuration | `workspace/engine/runtime/config.py` | Reuse existing LSP/context behavior. Route calls to the shared budget broker and pin provider/model settings; do not build ablation switches in v1. |
| Model loop/provider | `agents/agent_loop.py`, `agents/orchestrator.py`, `llm/provider.py`, `llm/openrouter.py` | Capture every provider call, including summaries, retries, and child reports. Profile models can override the child-model environment variable. |
| Context handling | `agents/compactor.py` | Reuse the existing compactor and history validation. No alternative context strategy implementation is required in v1. |
| Judge | `runtime/judge.py`, `runtime/judge_decisions.py` | Optional integration, disabled in the fixed v1 engine profile to avoid another paid service. Clearly label results as classifier off. |
| Observability | `runtime/trace.py`, `agents/hooks.py`, session usage callbacks, `runtime/metrics.py` | Existing traces cap strings at 4,000 characters and records at 16,000. They are useful diagnostics but insufficient as the complete benchmark record. |
| Results route | `workspace/pages/apps/web/app/engine/results/page.tsx` | Replace metadata about “three real tickets” with versioned benchmark metadata. |
| Results UI | `workspace/pages/packages/ui/src/landings/engine/results/{index-page,report,types}.tsx` / `.ts` | Keep the existing report type for historical case studies. Introduce a separate benchmark schema and dashboard. |

Treat executable configuration as the authority: some older judge documentation describes intent/write sites that are absent from the current `JUDGE_SITES` configuration. Do not plan v1 work around those historical sites.

## 1. Create and organize the harness

During implementation, run from the umbrella root:

```bash
./scripts/new benchmark --public
./scripts/sync-workspace
```

The first command creates and pushes `codeloom.benchmark`; use it only when implementing, not while drafting this plan. Record the benchmark and engine submodule commits in the umbrella repository. Use `codex/benchmark-harness` and corresponding engine/pages branches for implementation changes.

Use Python for orchestration, validation, grading, and reporting; the engine is already Python. Run the trusted scheduler and broker on the host, using a locked Python environment. Package task runtimes and the engine in separate images. Use Docker on Linux or Docker Desktop on macOS; serialize measured trials on one host. Do not put a Docker socket in a runner or agent container. Select **linux/amd64** for the reference campaign and lock per-platform digests. Record host architecture and any emulation (for example on Apple Silicon). A native arm64 run is a separately labeled campaign; latency across architectures/emulation is not interchangeable.

Proposed layout:

```text
workspace/benchmark/
  README.md
  pyproject.toml
  uv.lock
  Dockerfile
  scripts/reproduce
  codeloom_benchmark/
    cli.py                  # reproduce, validate, run, grade, report, export
    manifests.py            # strict schema and provenance checks
    sandbox.py              # separate agent/evaluator environments
    scheduler.py            # paired randomized order; resume ledger
    broker.py               # proxy, per-trial tokens, reservations, usage/timing
    adapters/{mini_swe_agent,engine}.py  # launch/capture adapters only
    graders/{patch,navigation,test_generation}.py
    traces.py
    reporting.py
    export.py               # local report and website data only
  suites/v1/
    repos.lock.json
    tasks/*.yaml
    public/*.md
    setup/*.patch
    suite.lock.json
  evaluator/v1/
    hidden_tests/
    answers/
    mutants/
    reference_patches/
  configs/{calibration,core}.yaml
  schemas/{attempt,report,site-export,navigation-answer}.json
  public/validate_answer.py  # format validation only; no private answer data
  .dockerignore             # excludes evaluator, references and run artifacts
  tests/
  reports/v1/                # generated compact release artifacts
  runs/                     # ignored raw runs and checkouts
```

Keep the evaluator outside every agent-accessible checkout, container, Git object database, and image layer. The reproduction orchestrator can access both; the agent cannot. Publicly released evaluator files remain “hidden” only at execution time. Call that limitation out in the methodology.

Construct each agent checkout from the post-setup source archive and initialize a fresh Git repository with one starting commit. Do not clone the upstream object database into the agent container: for fabricated bugs, `git show HEAD~1` would otherwise reveal the correct pre-regression implementation. Keep setup patches, upstream history, references, and fixture-authoring branches in the orchestrator's store only. Preserve attribution and the upstream commit in the manifest. Check this boundary explicitly during fixture certification.

### Process and filesystem boundaries

| Process | Location and access |
| --- | --- |
| Scheduler / mini-SWE-agent controller | Trusted host runtime; launches containers and captures artifacts. mini-SWE-agent's model-visible shell executes only inside its task container. Its host process must not inject host/evaluator data into model messages. |
| Budget/model broker | Trusted host process; holds the real OpenRouter key, only forwards allowed inference calls, enforces all budgets, and writes usage/timing logs. It never includes evaluator data in responses. |
| CodeLoom engine, LSP and every child/worktree | Inside the agent task container. File reads, commands and worktrees stay there. No evaluator mounts, host home directory, Docker socket, or real provider/GitHub credentials. |
| Evaluator | Separate container launched only after outputs are sealed. Receives trusted source/tests and permitted output patches; never shares its writable state with an agent. |

The host adapter connects to an engine socket through a dedicated socket-only mount; no host source tree is mounted into the agent. Containers reach only the inference broker through a constrained network gateway, and loopback services declared by the trusted test command. They cannot fetch packages, call the provider directly, reach GitHub, or connect to other agent/evaluator containers. Unprivileged users, no privileged flags, no Docker socket, and no other host mounts. Broker credentials are per-trial ephemeral tokens; neither token reveals the real account key.

Build task images from an explicit allowlisted staging directory. `.dockerignore` also excludes `evaluator/`, reference patches, mutants, private answer files, reports, credentials and run artifacts; inspect image layers and build context contents. This is a defense in addition to not copying those files.

Certification scans **the entire agent-visible filesystem**, including Python site-packages/venvs, LSP paths, npm caches/tarballs, Go module/download caches/archives, examples and mounted files, for extra copies or newer implementations of the target library. Use package inventories, path/symbol searches and source fingerprints, not only identical-file hashes. The intended source checkout is the only executable target implementation. Dependencies are staged without cached source archives for the target. If an engine dependency unavoidably installs a competing target implementation, replace that target fixture/repo before freeze; relocating a readable venv does not fix the leak. Rescan the final image and fresh writable mounts per trial.

## 2. Freeze a realistic 16-task corpus

Select these five candidates because they cover the engine's supported languages, have modular code and upstream tests, and can be evaluated without production services. Repository metadata and source paths were inspected on 14 October 2026; the following are candidate base commits, not yet certified runnable fixtures.

| Repository | Language | Candidate upstream commit | Source areas | Candidate tasks |
| --- | --- | --- | --- | --- |
| [msiemens/tinydb](https://github.com/msiemens/tinydb) | Python | `6d7c4fc3e04d398b1282876514660a33a7897b41` | `tinydb/{database,table,queries,storages,middlewares}.py` | 4 |
| [theskumar/python-dotenv](https://github.com/theskumar/python-dotenv) | Python | `0b2880591780a426b0551436f47a17e7a76d954f` | `src/dotenv/{main,parser,variables,cli}.py` | 4 |
| [sindresorhus/p-queue](https://github.com/sindresorhus/p-queue) | TypeScript | `7f272ab5ff2254c0dc2537b7977e88ede1cd55e8` | `source/{index,priority-queue,queue,options,lower-bound}.ts` | 4 |
| [sindresorhus/ky](https://github.com/sindresorhus/ky) | TypeScript | `3541888878c4d303a8bc2ad61815f4e048f55a5d` | `source/core`, `source/utils`, `source/types`, `source/errors` | 4 |
| [go-chi/chi](https://github.com/go-chi/chi) | Go | `c9744a3641452cfe4fbb9929ceb554659d6bd950` | `mux.go`, `tree.go`, `context.go`, `chain.go`, `middleware` | 4 |

GitHub metadata identifies MIT licenses for TinyDB, p-queue, Ky, and chi, and BSD-3-Clause for python-dotenv. Preserve the license files and attribution in fixtures and releases. Resolve exact runtime versions, installation commands, tests, compiler checks, dependency lockfiles, and container digests during fixture certification. If a candidate's current development snapshot is unstable, pin a suitable release commit before freezing the suite.

The candidate pool below has 20 tasks, one per category per repo. Freeze **16 active tasks: four per category**, with five repos and three languages represented. Defer exactly `tinydb-N`, `dotenv-T`, `pqueue-B`, and `chi-X` before measurement. TinyDB, dotenv, p-queue and chi contribute three tasks each; Ky contributes four. These removals reduce overlapping navigation/interpolation/queue cases and defer the least grounded writer-reset feature. All task IDs use `repo-category`, without obsolete numeric suffixes.

Most tasks should be medium difficulty, with some harder cases. Favor meaningful behavior changes, interacting edge cases and cross-file reasoning over trivial operator swaps or rename exercises. Difficulty is set from the reference specification and patch, before agent outcomes. The author chose the categories and writes the engine; a 25% navigation share may favor its tooling. Seek an independent unpaid task/specification review before freeze. If unavailable, document author-designed selection bias explicitly; neither overall nor category scores estimate general coding ability.

For the four active bug tasks, prefer a reproducible historical bug at its pre-fix commit, with the original issue cited. The targets below guide sourcing. Use a clearly labeled fabricated regression only if a suitable historical fixture cannot be certified. Published historical fixes may have been in model training; disclose that limitation. Cross-file features should resemble a scoped real library change and require runtime/API/type/storage integration, not an arbitrary rename. Do not reduce hidden-test rigor when reducing task count.

A historical task pins its actual pre-fix commit rather than the candidate current commit in the repository table. Keep the original public issue description where possible; move clarifying acceptance details into a documented requirements brief without revealing the gold implementation. Four bug fixes plus four substantive cross-file changes form the eight active patch-producing tasks; navigation and mutation-graded test generation remain separately labeled outcomes.

### Proposed task inventory

`B` = historical bug when certifiable, otherwise a labeled fabricated regression in an isolated snapshot; `X` = explicitly specified new change; `N` = code navigation; `T` = test generation on correct code. The table specifies authoring targets, not claims of existing upstream defects. Validate each against the selected base; if a feature already exists, replace that candidate before suite freeze. Candidate tasks are independent snapshots; the manifest marks the four deferred IDs as excluded before any run.

| ID | V1 status | Task specification | Grading target |
| --- | --- | --- | --- |
| tinydb-B | Active | Restore cache invalidation after document update/removal. | Query before and after mutation; changed and unrelated records. |
| tinydb-X | Active | Add a public flush contract through database, storage, and caching middleware. | Pending data becomes durable without closing; unsupported/default storage semantics specified. |
| tinydb-N | Deferred | Explain how document IDs survive JSON storage round trips. | Correct conversion boundaries, paths, and symbols. |
| tinydb-T | Active | Add tests for middleware flush thresholds and close. | Kill held-out threshold and skipped-flush mutants. |
| dotenv-B | Active | Restore environment-vs-file interpolation precedence. | Both override settings, defaults, missing and empty values. |
| dotenv-X | Active | Add strict invalid-binding handling to the loading API and CLI. | Source/line error reporting; existing lenient default preserved. |
| dotenv-N | Active | Trace CLI `run` environment construction and override behavior. | CLI, main API, and variable-resolution edges. |
| dotenv-T | Deferred | Test variable defaults, empty values, and precedence combinations. | Kill held-out resolution and ordering mutants. |
| pqueue-B | Deferred | Restore bookkeeping after a queued/running task is aborted. | Promise outcome, queue size, pending count, later task progress. |
| pqueue-X | Active | Add bulk priority updates for queued task IDs. | Queue implementation, interface and public API; missing/running IDs specified. |
| pqueue-N | Active | Trace `add` through enqueue, task execution, and completion counters. | Required queue/interface/implementation edges. |
| pqueue-T | Active | Test pause/resume, rejection, and eventual idle completion. | Kill held-out pending/dequeue/idle mutants; no flaky sleeps. |
| ky-B | Active | Restore preservation of inherited headers when extending defaults. | Headers object/record forms, overrides, deletion semantics. |
| ky-X | Active | Add an opt-in error-summary header-redaction option. | Option typing, normalization, error construction; selected names redacted case-insensitively. |
| ky-N | Active | Trace timeout/abort handling from request setup to error export. | Constructor/util/error boundaries and cleanup behavior. |
| ky-T | Active | Test cancellation during retry delay and request execution. | Kill held-out signal/cleanup mutants using fake clocks/fetch. |
| chi-B | Active | Restore request-context reset so pooled contexts cannot leak URL params. | Multiple requests; nested and absent params. |
| chi-X | Deferred | Add response-writer reset support and integrate it into middleware use. | Counters/status/tee state reset; underlying-writer semantics specified. |
| chi-N | Active | Explain nested route parameter lookup and route-context lifetime. | Context/router/tree boundaries; precedence and reset. |
| chi-T | Active | Test response-writer optional interfaces and accounting. | Kill held-out wrapper-selection/status/byte mutants. |

Include no synthetic rename-only tasks; navigation should support understanding substantive behavior. Cross-file tasks require coordinated production-code changes in at least two files; a test or README alone does not qualify. Each public feature brief pins callable names, modules/exports, signatures/type declarations, default behavior, error behavior and compatibility requirements, with example input/output and visible compile/import checks. Hidden tests may exercise unpublished edge-case inputs, but may not require an API contract absent from the public brief. Resolve the proposed p-queue bulk-update method and Ky redaction option names/types during authoring, before grader implementation/freeze.

### Task manifest and certification

Each manifest contains task/category/language/difficulty IDs; upstream URL and immutable commit; setup patch hash; prompt hash; allowed output paths; baseline expected failures; installation and verification commands; evaluator/reference hashes; navigation rubric or mutant IDs; timeouts; license attribution; and provenance (`fabricated`, `historical_issue`, or `new_feature`). Never send hidden test names, answers, mutants, reference patches, or solution paths to the model.

Historical bugs use their genuine pre-fix snapshot; fabricated bugs apply a declared narrow behavior regression to otherwise correct code. Both must build, reproduce the specified failure, and pass their declared unaffected tests. No-op fails the private acceptance checks; the reference patch passes all acceptance and regression checks. **Keep upstream tests intact even when they expose the bug.** Public failing tests are legitimate developer evidence. Never delete or weaken tests to make a fabricated regression harder. Record every preexisting failure and setup change, and restore immutable upstream tests during grading. Feature tasks use an unchanged base that fails new acceptance checks and a reference patch that passes all checks.

Navigation uses a public `navigation-answer.json` schema, included in both prompts, and an offline format validator available in both containers. Fields include task ID, relative paths, qualified symbols, call edges and claim IDs/evidence; plain explanations are supplementary. The validator checks structure/path conventions only and contains no gold symbols/answers. The private rubric checks required facts and accepted symbol/relationship equivalents rather than string matching prose or line numbers. Freeze accepted equivalents using at least two human-written equivalent answer formulations; record their authors and disclose if both were written by the engine author; never use the measured model's answers on measured tasks to tune grading.

Test generation accepts new test files only; protect production/existing tests and trusted verification commands. Correct code and unchanged trusted tests must pass. Require at least 80% of five or more certified non-equivalent mutants to be killed by the new assertions, with none already killed by the trusted visible suite. Establish framework-specific structured result parsers for pytest, AVA and `go test -json`. An assertion failure in an executed generated test counts; build/import/collection errors, exits from process crashes, and timeouts do not count as kills.

Before grading a test-generation submission, run it **three times on correct code** and require all three passes. Run each mutant **three times** under the same deterministic clock/seed setup; count a kill only when the same intended assertion fails in all three runs. Certify trusted fixtures/reference tests with five repeated runs. Flaky submissions receive `unstable_tests` and fail; do not pick their best run. Timing tests should use controllable clocks and local fake responses. Fixture certification identifies whether Ky needs Chromium and a loopback test server; include pinned runtimes/services if required, with no external network access. Record trusted-suite coverage and commands rather than silently skipping browser tests.

The mutant requirement is a real certification risk in well-tested libraries. Prepare category-preserving alternate test-generation targets before freeze: middleware durability/state transitions (TinyDB), priority update/cancellation interleavings (p-queue), header normalization/retry-signal boundaries (Ky), and optional-interface/short-write behavior (chi). If existing tests cover all valid mutants, a replacement may use a declared correct new feature implementation with its reference tests withheld from both agents; publish that setup and retain unrelated upstream tests. It still needs five independent behavioral mutants and manual reference validation. If no candidate certifies, replace the fixture before preregistration; do not lower the threshold or drop failures afterward.

Use deterministic fake-provider tests for most integration. The explicit live-development allocation covers real reasoning/stream/tool-continuation probes and **two full-size development tasks outside the measured set**, each run with both systems. Make one a TypeScript async/cross-file task and one a Python persistence/validation task, comparable in expected change size to release tasks. Small probes test transport only; they cannot calibrate measured budgets or runtime. Stop development at its allocation and document any blocking compatibility or certification issue. Freeze active tasks, references, graders and controls before measurement; oracle changes require versioned regrading of all affected outputs.

## 3. Publish artifacts without maintainer work

V1 produces downloadable patches, answers, verification logs and sanitized native traces in the benchmark repository/release artifacts. Preserve source attribution and label each task `historical_issue`, `fabricated`, `new_feature`, `navigation`, or `test_generation`, as appropriate. No pushes to upstream, issue comments, fixture-repo creation, GitHub credentials inside agents, or automated draft PRs. PR automation and any independently reviewed genuine upstream contribution are deferred. Existing historical website PR case studies remain available, outside the measured dataset.

## 4. Implement the baseline and engine adapter

**Baseline: pinned mini-SWE-agent.** Install a locked release/commit of the existing package and run its non-interactive `DefaultAgent` on the trusted host, with its existing compatible model backend pointed at the shared broker and its Docker environment restricted to the task container. It provides the agent loop, shell actions, model-history handling and trajectory serialization. Its provider configuration records the requested model settings; the broker pins the effective provider/decoding settings and supplies usage metadata. The benchmark only supplies configuration, starts a run and collects its native trajectory, answer and final Git diff. No custom list/read/search/edit tools, baseline prompt engineering, bespoke agent loop, or fork of its core. [Agent overview](https://mini-swe-agent.com/latest/), [OpenRouter backend](https://mini-swe-agent.com/latest/reference/models/openrouter/), [Python bindings](https://mini-swe-agent.com/latest/usage/python_bindings/).

Use the package's standard non-interactive system prompt and native shell interface, recording both. Append the same task/output requirements used for CodeLoom; navigation tasks return a structured answer artifact through the documented completion path. Pin the native package/API at integration time rather than reimplementing it. Choose an existing compatible backend/version that accepts a custom inference base URL; do not patch its core to route calls. Model/provider/reasoning settings, starting source, sandbox access, monetary allowance, watchdog and graders are shared. System prompts and tool/context handling are part of the systems being compared and need not be artificially identical.

The baseline adapter only launches with the task/config, observes native completion, collects output and normalizes native usage/trajectory metadata. **No baseline provider wrapper or agent fork.** The host broker is the shared pre-call budget enforcement point. Native package limits may remain secondary guards but must not be tighter than the common envelope.

Reasoning fidelity has one symmetric rule: **select existing package versions/backends and a model mode that preserve provider-required continuation fields natively in both systems; do not patch one arm during calibration to improve reasoning fidelity.** Inspect request/response round trips with the same scripted and live compatibility probes. If either fails, select another pinned version/backend or model before freezing and rerun the affected development probes. If no faithful combination fits the budget, stop and report the incompatibility; never claim Max-mode evaluation while silently dropping required state. Optional-field behavior and each system's native compaction remain disclosed system characteristics. Any later core reasoning fix creates a different engine/package version and a separate campaign.

**Engine profile (`engine-core`):** run the entire engine and its LSP/children/worktrees inside the task container. Retain the orchestrator, coder/tester/reviewer flow, tree-sitter, LSP, write funnel, within-task memory and compactor, with `ENGINE_JUDGE=off`. Connect from the host through its socket, send `StartSession` and the task, and capture events. Observe pending follow-ups and automatic verifiers, not just the first assistant message.

**Submission policy fixed before M2:** grade the main checkout's allowed diff and the public answer artifact at the terminal boundary, relative to its starting commit. Successful writes committed to main are included as well as remaining edits/new allowed files. Child-worktree-only changes are **not submitted**, even if their tests passed. Ask both systems to leave final changes applied to their main task checkout. The harness answers any engine settlement prompt with **merge into this isolated main task checkout**; it requests no PR, push, custom branch selection or recovery cherry-pick. Successful native settlement belongs to the engine's output; failed merges remain failed. If multiple child branches remain unmerged, retain them as diagnostic artifacts, grade only main, and record `unsubmitted_child_work`.

The terminal boundary is explicit completion with the orchestrator idle, all children/verifiers/settlement work inactive and no queued follow-up/prompt, or broker budget refusal/watchdog expiry/crash. Seal the main tree before shutdown can clean up children. Record completion state and settlement events. Navigation requires the public answer artifact in main; assistant prose is not an alternate submission channel. Source edits on navigation tasks remain prohibited; the answer artifact is an allowed output.

The harness supplies no requirements or hidden-test feedback mid-run. A request for missing product decisions becomes `needs_input`. Other mechanical prompt answers are frozen before measurement and remain inside the shared budget. On budget refusal, stop all task processes, capture partial main output, and record failure; partial hidden-test passes cannot turn a terminated trial into primary success.

Disable external research, GitHub mutation, browser access, MCP connections, user skills, and inherited workspace memory in both systems for this repository-only study. Network access to the LLM goes through a broker outside the code container; dependencies are prefetched. This avoids online solution lookup, hidden-grader access, and different external assistance. Apply the same restrictions to child worktrees.

## 5. Implement only the controls needed for v1

Do not build LSP on/off switches, alternative compaction strategies, classifier calibration, or an interaction runner for the first release. Preserve current engine LSP and context behavior. Install fixed language-server versions; do not allow `npx` to fetch a new version during an attempt.

1. **Shared inference broker.** Enforce the campaign, stage and per-trial reservation ledger at one trusted host process, not inside the engine. A signed/opaque ephemeral trial token identifies every call, including child/summary calls and provider retry charges. Before forwarding, reserve input plus maximum output at recorded prices, using a verified tokenizer or conservative input upper bound; reconcile actual charges. No model call bypasses the broker. It forwards protocol-compatible responses/streams without selecting tools or changing history, and logs request/response hashes, usage, token throughput, first-token delay, queue/429/backoff time, model/provider IDs and errors. Missing/unknown bills retain conservative reservations and block overspending. Simultaneous child requests share one envelope; per-call or per-child limits cannot multiply it.
2. **Minimal engine adapter changes.** Add a configurable inference base URL and ephemeral broker credential, verify all auxiliary calls use it, and translate a typed broker refusal into `budget_exhausted` before graceful collection/abort. Native library/SDK retries may not bypass broker accounting; freeze retry configuration. Keep profile/model override behavior explicit. Existing turn/tool fuses remain disclosed system settings. Do not implement a second engine-side spend ledger.
3. **Observable artifacts.** Reuse native traces/events and broker call records; preserve full payloads in hashed compressed files when native traces clip them. Collect parent/child IDs, main output, unsubmitted branches and actual compaction events. The broker never repairs dropped reasoning state or makes tool/settlement decisions.

Test the broker with concurrent reservations, aborts, unknown cost and HTTP/stream failures; test engine routing/refusal cleanup and terminal-tree sealing. Use the same reasoning-continuation conformance cases for both pinned systems. No ablation controls or special baseline agent logic are required.

## 6. Affordable experiment and model decision

### Zero ablations; two systems

| Configuration | Agent | Navigation | Optional classifier | Context |
| --- | --- | --- | --- | --- |
| `baseline` | Pinned mini-SWE-agent DefaultAgent | Native shell interface | Off | Native linear history |
| `engine-core` | Existing orchestrator/agent engine | Tree-sitter + LSP | Off | Existing compactor |

This comparison asks whether the harness improves independently graded outcomes and cost at a fixed generator. It cannot establish which individual component caused a difference. A tool-use or navigation trace can suggest a mechanism, but a causal component claim needs a later dedicated ablation. Do not publish an invented “LSP improved success by X%” conclusion from these two arms.

Freeze the **16 active tasks** named in section 2. Run each once with both systems: **32 primary trials**. Repeats are deferred, leaving money for representative calibration and integration work. Neither result stability nor a repeat-based reliability estimate is measured in v1. Every failed/aborted invocation remains an artifact; an eligible infrastructure recovery is a separate linked execution, not a repeat selected by score.

### Model choice informed by coding benchmarks

Recommend **`deepseek/deepseek-v4-pro-0813` in Max reasoning mode**, initially pinned to **`baidu/fp8`** on OpenRouter. This is a dated full Pro revision, selected for repository/terminal-agent capability, not simply its price. Keep this same generator, provider and reasoning configuration for both systems and every child/summary. The endpoint requires full-size development compatibility/cost/time calibration; published rates alone are insufficient.

Research covered the official SWE-bench leaderboard, SWE-bench Pro, Terminal-Bench, model-author reports, Artificial Analysis, and OpenRouter endpoint metadata. There is no defensible universal “perfect model”; benchmark versions, harnesses, inference budgets and reasoning modes differ. Give agentic repository/terminal results more weight than general knowledge or contest-code scores.

| Evidence | Results observed | Interpretation |
| --- | --- | --- |
| Official SWE-bench Verified, mini-SWE-agent | Kimi K2.5 high: 70.8%; DeepSeek V3.2 high: 70.0% | Same named harness; credible candidates. These are older revisions, not evidence about K2.6 or V4 Pro. |
| Official SWE-bench Multilingual, mini-SWE-agent | Kimi K2.5: 67.3%; DeepSeek V3.2: 59.0%; both marked checked | Useful evidence of multi-language repository competence; different from Verified. |
| Moonshot's Kimi K2.6 report | Verified 80.2%; Pro 58.6%; Terminal-Bench 2.0 66.7% | Strong alternative. Vendor-run; SWE uses an in-house scaffold, terminal uses Terminus-2; not the preceding leaderboard setup. |
| DeepSeek's current Pro/Flash comparison | Pro 0813 vs Flash 0731: Terminal-Bench 2.1 87.9% vs 82.7%; DeepSWE 62.7% vs 54.4% | Relevant evidence favoring Pro within the vendor's reported comparison. Do not compare TB 2.1 numbers directly with Kimi's TB 2.0 result. |
| Artificial Analysis | Pro 0813 Max: Intelligence Index 36; Flash 0731 Max: 34; Kimi K2.6 reasoning: 27 | Independent corroboration, but broad intelligence is secondary to repository coding. |

Sources: [official SWE-bench leaderboards](https://www.swebench.com/), [SWE-bench Pro methodology/results](https://labs.scale.com/leaderboard/swe_bench_pro), [Moonshot's model card](https://huggingface.co/moonshotai/Kimi-K2.6), [DeepSeek's Pro 0813 card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro-0813), [Artificial Analysis Pro evaluation](https://artificialanalysis.ai/models/deepseek-v4-pro), [AA Flash/Kimi comparison](https://artificialanalysis.ai/models/comparisons/deepseek-v4-flash-vs-kimi-k2-6). The official SWE page embeds its results as `leaderboard-data`; save the exact source snapshot with the research notes. Neither V4 Pro 0813 nor Kimi K2.6 appeared in that fetched official SWE dataset. Do not call their model-card scores official leaderboard verification. Avoid importing the generic “SWE-bench” numbers from pricing-site summaries without a known dataset and scaffold.

This evidence supports Pro 0813 as a strong affordable choice; it does not establish superiority in CodeLoom. Kimi K2.6 is the fallback if Pro's hosted endpoint cannot faithfully carry the engine's tool/stream/history contract. Do not automatically fall back mid-campaign or use different models on harder tasks.

### Endpoint, settings, and compatibility gate

Use the dated model slug with `provider.only=["baidu/fp8"]`, `allow_fallbacks=false`, `require_parameters=true`, and save endpoint revision/quantization/prices. OpenRouter's provider details inspected on 14 October 2026 identify FP8, tool support and reasoning support. The selected endpoint currently has promotional pricing around **$0.25/M uncached input and $0.75/M output**; conservatively plan at **$0.30/M input and $0.90/M output**. Refresh before spending. [Model/provider table](https://openrouter.ai/deepseek/deepseek-v4-pro-0813), [endpoint API](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-pro-0813/endpoints).

The earlier Flash/DeepInfra endpoint costs less, but task breadth is now reduced so stronger Pro inference fits. Kimi K2.6 also fits a smaller study: for example Chutes INT4 metadata listed $0.45/M input and $1.95/M output, making its reasoning output materially more expensive than the inspected Pro endpoint. Pricing alone cannot guarantee deployment fidelity or quality. Preserve exact metadata and validate real tool continuations in development probes. [Kimi endpoint metadata](https://openrouter.ai/api/v1/models/moonshotai/kimi-k2.6/endpoints).

Select Max reasoning and confirm the OpenRouter/provider mapping during the shared development compatibility probes: the author calls this `max`, while OpenRouter may expose the corresponding setting as `xhigh`. Do not silently accept an ignored parameter. Use the author's recommended `temperature=1.0`, `top_p=0.95` in both systems and an initial per-response output allowance of **32,768 tokens**, including reasoning. A small 8k output cap could truncate deliberate reasoning and undercut model capability; development probes must check cutoff handling and preserve reasoning/tool history. This still does not reproduce the vendor evaluation budget. All completions reserve worst-case input/output charges under the shared task cap.

### Representative calibration and frozen watchdog

Allocate **$8 maximum to live development**, including both systems, streaming/reasoning/tool-continuation debugging and calibration. Reserve up to $1.50 per trial for **two full-size development tasks × two systems = four calibration trials ($6 maximum)**, with $2 for short live probes and fixes. Fake-provider tests cover the rest. These tasks are outside the measured corpus and do not publish a model ranking. Small probes cannot justify a measured-time or spend cap.

The broker measures actual end-to-end generated-token throughput (including reasoning), first-token/queue delays and total duration; report decode throughput separately where streaming allows it. Collect returned completion/reasoning counts and input replay for all agents. The lower observed throughput and longest duration across both systems drive a **shared** watchdog, frozen before measurement: `W = max(60 minutes, ceil(2 × longest completed full-size calibration duration + 2 minutes))`. Derive compatible per-request/idle timeouts from observed token rate and the maximum completion allowance so SDK defaults do not truncate slow Max reasoning. Record exact values and method in the preregistration. This deliberately avoids the old uncalibrated 20-minute watchdog; time is local, not another paid service.

If calibration trials exhaust their development cap or fail to complete, they give a lower bound rather than a trustworthy completion duration. Do not use them to assert that the $1 measured envelope is adequate. Run an additional representative calibration only within the remaining development allocation, or stop before measurement with a scoped feasibility report. Two development tasks cannot estimate the long tail; describe this uncertainty. Both systems use the same preregistered $1 envelope, with budget-hit counts reported separately from behavioral failures. If large portions would be capped, do not spend the measured allocation merely to produce a misleading score.

### Spend allocation

| Allocation | Maximum API spend |
| --- | --- |
| Live integration and full-size calibration | $8.00 |
| 16 measured tasks × two systems × $1 each | $32.00 |
| Eligible infrastructure recovery | $5.00 |
| **All API spend** | **$45.00** |
| Payment fees/taxes headroom | **$5.00** |
| **Total ceiling** | **$50.00** |

The broker enforces the $45 campaign cap including in-flight reservations and stage pools; a dedicated OpenRouter key supplies a second limit. Auto top-up stays off. Reserve $2 before starting a measured task pair. Any development overrun must be resolved **before** preregistration by reducing measured scope or stopping, without exceeding $50; there is no repeat allocation to spend. No paid grading, classifier calls, cloud compute or PR-summary generation. Confirm the actual checkout fee/tax amount and reduce usable API pools if payment headroom is insufficient. [OpenRouter pricing](https://openrouter.ai/pricing).

At the earlier conservative $0.30/$0.90 token-price snapshot, 2M cumulative input plus 150k completion/reasoning output costs **$0.735** per trial, illustrating why $0.80 left little headroom. This is a scenario, not a measured forecast. The new $1 cap is still a budget-constrained evaluation and must be checked against both systems' full-size calibration. Cache savings are not assumed. Refresh endpoint prices and availability before the live allocation; promotions can end. Record rate limits, 429s, queue/backoff time and child concurrency separately: these affect latency and cost even when the model is identical.

### Minimal fairness and practical execution

Keep the same public task prompt, dated generator/provider, reasoning settings, execution sandbox, task snapshot, $1 measured spend allowance and calibrated watchdog W in both arms. Preserve the engine's existing 120,000-token context ceiling and mini-SWE-agent's native context/history behavior; document the effective settings. Do not implement compaction or a custom rolling window just to change the baseline. Context handling is part of the system comparison; this is not an isolated test of any one component. No special CPU benchmark matrix or strict cold-cache experiment is needed. Run serially on the same local computer, with locked task dependencies. Record ordinary runtime fuses so reproduction is interpretable.

Start each attempt with a fresh task Git snapshot, empty engine memory/session, and no previous conversation. Record cached provider tokens but do not try to force provider caches empty. Randomize system order within each task block and record the seed before preregistration; there are no measured repeats in v1. Include engine/LSP startup in elapsed time; report setup/evaluation time separately. Verify LSP works in the engine profile and classifier calls remain zero.

Do not grow the matrix or run a second measured model merely because spare funds remain. Save that money. A later component experiment is a separate budget and campaign chosen from actual failure evidence.

## 7. Evaluation and metrics

Run evaluation only after final outputs are sealed. In a separate evaluator container, reconstruct the trusted task baseline, apply only permitted output changes, restore trusted verification commands, and mount the private oracle. Agent changes cannot edit graders, weaken existing tests, alter evaluation config, or inspect hidden fixtures. Validate patch paths including symlinks. Evaluate generated code/tests with resource limits and no credentials.

**Patch success:** all hidden behavioral checks pass, trusted build/type checks pass, and all initially passing trusted regression tests still pass. Agent-written tests supplement evidence but cannot replace trusted checks.

**Navigation success:** the public schema/validator accepts the answer artifact and the frozen private rubric accepts all required symbols, relationships and claim evidence. Prose quality is supplementary and does not decide the primary outcome. Main source code is unchanged.

**Test-generation success:** only allowed test files change; generated tests pass correct code three times; the trusted suite passes; at least 80% of five or more certified behavioral mutants are killed by the same assertions on all three mutant runs. A structured parser rejects build/import/collection/runtime failures and timeouts as kills. Unstable submissions fail with `unstable_tests`.

Maintain separate trial status and verification fields. Agent failures include explicit needs-input, context/format errors, agent crashes, unsubmitted child work, budget exhaustion and watchdog expiry; they are not retried. Trusted-grader defects require a versioned correction and regrading **all** affected sealed outputs without new agent calls; they are not silently scored as model failures.

### Fixed infrastructure recovery policy

Eligible infrastructure errors are confirmed provider transport/HTTP 408/429/5xx failures that abort the trial, or scheduler/container-host failures outside the agent process. Agent command failures, emitted invalid tool calls, model context overflow, merge conflicts and agent process crashes are agent outcomes. A broker-denied request due to depleted budget is an agent budget outcome, not an infrastructure error. Identify eligibility from immutable logs; never from whether the patch looks promising.

Allow **at most one full fresh retry per system/task** for an eligible infrastructure error, with the same task snapshot, settings, seed policy, money cap and watchdog. Do not resume its conversation or patch. Original and retry have separate linked IDs; original logs, bills and elapsed time remain public. Spend all recovery from the $5 pool, with no hidden SDK retry path outside the broker. Configure provider request retries as disabled for v1 so a task-level retry is the declared recovery policy. Admission requires enough campaign/recovery headroom. Otherwise the trial is `infra_unresolved`.

The **evaluated primary score** uses the eligible retry outcome when present; resolved infrastructure originals are not extra task votes. An unresolved infrastructure trial is excluded from the evaluated-outcome denominator, which is shown explicitly. Alongside it show a **scheduled-task delivery score out of all 16 tasks per system**, with unresolved/missing outputs as unsuccessful deliveries. For paired counts, use pairs with evaluated outcomes for both systems and show excluded/unresolved pairs. Costs and operational elapsed time include the original and eligible retry in both evaluated and delivery reporting. Never replace an agent failure with a fresh try. Abort and version a campaign-wide harness fix affecting measured semantics; do not repair one system mid-campaign.

| Metric | Definition |
| --- | --- |
| Paired outcomes, headline | Engine-only pass, mini-only pass, both pass and both fail on evaluable matched pairs; unresolved pair count shown separately. |
| Success / delivery | Evaluated passes / evaluable tasks and delivered passes / all 16 scheduled tasks, with explicit denominators. |
| Cost per evaluated success | All measured and eligible-recovery charges / evaluated successes; zero successes is undefined/∞. Development spend shown separately, unknown costs remain flagged. |
| Elapsed time | Actual trial latency and operational task latency including eligible recovery; median plus full points/range. p90, if shown, is labeled unstable for a small sample. |
| Regression / termination | Counts of behavioral failures, regressions, budget/watchdog hits, crashes, unsubmitted work and unresolved infrastructure. |
| Run variability | Not estimated in v1: one evaluated trial per system/task, subject only to the declared infrastructure recovery rule. |
| Operational telemetry | All-agent input/output/reasoning/cached tokens, calls, observed throughput, LSP startup, actual compaction, concurrent-child peaks, 429s and queue/backoff delay. |

Lead with raw paired counts and repo/category breakdowns. Sixteen tasks within five repos are correlated, and neither task-wise independent confidence intervals nor a five-repo bootstrap supports precise generalization. Omit headline success CIs/significance claims in v1. Small changes at temperature 1.0 may be noise. The author-designed category mix, single-provider endpoint, native reasoning/context behavior, shared money limits and hardware/emulation all constrain interpretation. No repeated-pair sample remains, so make no reliability claim.

## 8. One-command reproduction and immutable outputs

After cloning the umbrella repository with submodules and configuring the API credentials and Docker, the proposed command is:

```bash
./workspace/benchmark/scripts/reproduce --profile core --suite v1
```

The wrapper validates pins and credentials, builds/pulls locked images, verifies task fixtures, prints attempt count and spend ceiling, executes the frozen schedule, grades sealed outputs, aggregates results, and creates the website bundle under the $45 API campaign limit ($50 including payment headroom). Repository network/setup work happens before attempts; code containers cannot reach the evaluator. Fetch the engine from the recorded immutable commit, not whichever sibling checkout happens to be dirty. Record local differences only in explicitly non-release development runs.

Also support:

```bash
./workspace/benchmark/scripts/reproduce --profile calibration --suite dev
./workspace/benchmark/scripts/reproduce --profile core --suite v1 --dry-run
./workspace/benchmark/scripts/reproduce --profile core --suite v1 --resume RUN_ID
```

Resume only unstarted scheduled trials after checking every suite/config/engine/model hash. Eligible infrastructure recovery follows section 7; it is not an ordinary resume. A crashed attempt is a recorded failure, not “missing.” Do not create an extra model run through resume. A fresh reproduction creates a fresh run ID. Add an offline `report` command to regenerate tables/site exports from an existing artifact bundle without paying for model calls.

Reproduction has two explicit tiers. **Offline artifact/report rebuild** guarantees matching normalized tables/site exports/checksums from a released sealed bundle, pinned reporting code and deterministic serialization. **Live rerun** guarantees executing the documented protocol on the pinned source/fixtures/settings when the hosted model/endpoint is available; it cannot guarantee matching samples, costs, runtime, availability or provider behavior. If the original endpoint disappears or its prices exceed the ledger, fail preflight. An explicitly selected substitute endpoint/model creates a new campaign ID and provenance, never an exact reproduction claim. Same-platform images are pinned; another architecture is a separately labeled rerun.

Output one immutable release folder per run:

```text
reports/<run-id>/
  manifest.json             # all pins, prices, budgets, schedule, host/resources
  attempts.jsonl
  summary.csv
  summary.json
  report.md
  tasks/<task-id>.json
  traces/<attempt-id>.jsonl.gz
  blobs/<sha256>.*
  patches/<attempt-id>.patch
  verification/<attempt-id>.json
  site/summary.json
  site/tasks/*.json
  SHA256SUMS
```

Track compact summaries and report text in the harness repository; store full trace bundles as versioned release assets. Public traces remove credentials, headers containing tokens, and machine-specific private paths while preserving model-visible task content and decisions. Retain evaluator internals outside the public agent trace. Assert trace counts and usage totals agree with the ledger; do not sum cumulative `StatsUpdated` events or double-count child costs already included in session totals.

The table summarizes evaluated primary outcomes and separately accounts for delivery/recovery; before measurement show “Not run.”

| System | Pass / evaluable | Delivery / 16 | Total measured + recovery cost | Cost / success | Median / range elapsed | Budget / watchdog hits | Infrastructure unresolved |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mini-SWE-agent | Not run | Not run | — | — | — | — | — |
| Engine core (classifier off) | Not run | Not run | — | — | — | — | — |

`report.md` should be short: methodology, table, one measured system comparison and two or three trace-based observations, counterexamples, and limitations. The system-level claim gives the paired success/cost/time change, affected categories, uncertainty and linked failures. Trace-based component explanations are hypotheses, not measured ablation effects. Link any engineering response to its motivating failure; measuring the fix belongs to a subsequent campaign. If a difference is inconclusive, say so.

## 9. Redesign the website around the report

Continue in the existing Next.js 15 / React 19 / Tailwind 4 site. Reuse navigation, product shell, fonts, dark surface and engine accent. The UI/UX skill's data-dense-dashboard guidance fits the report; unrelated sales-page templates do not. Use restrained chart color, direct labels, shapes in addition to color, accessible tables, and keyboard-visible controls. Avoid decorative score meters or a radar chart that hides the different units.

Order `/engine/results` from summary to detail:

1. **Provenance:** suite/run, model/provider versions, classifier-off status, engine/mini commits, calibrated budget/time settings, date, hardware/platform and completion state.
2. **Paired outcome summary:** engine-only versus mini-only passes, both-pass/both-fail counts and unresolved/excluded pairs. Show success and delivery fractions with denominators alongside total spend and termination counts.
3. **Simple graphs:** counts by task category, cost per evaluated success, and task elapsed-time points. No headline CIs or significant-difference badges; captions state the small, correlated sample.
4. **Results table and task explorer:** the two systems, category/repo filters, all failures visible, submission/verification summaries and download links for patches/answers, traces, CSV and JSON. Graphs and summaries remain above the repo list.
5. **Observations from traces:** a short report with linked evidence, hypotheses and counterexamples. No “Design-choice findings” section or component-effect claims.
6. **Methodology/limitations and historical case studies:** grading/recovery definitions, author selection bias, controls, reproducibility tiers and the three existing PR reports outside the benchmark.

V1 needs the overview and a task explorer with lightweight expandable rows plus downloads. Preserve an immutable overview route `/engine/results/benchmarks/[suite]/[run]` and existing historical report URLs. Defer dedicated attempt pages, side-by-side attempt comparisons, an interactive full trace timeline, live hosting features and GitHub PR automation. Native downloadable trajectories provide the detailed evidence.

Build the website from a validated generated `site-export` schema, copied into the pages repo by an explicit import command with source run ID and checksum. No handwritten metric copies and no live provider credentials in Next.js. Keep publication/deployment separate from reproduction; the benchmark command produces files and a previewable site bundle. Preserve run-specific links even when “latest” changes.

Use responsive chart/table alternatives at 375, 768, 1024 and 1440px, visible chart values with a data-table fallback, direct labels, keyboard/filter access, adequate contrast and reduced-motion support. On mobile show cards or a deliberately scrollable labeled table; do not require hover to read results. Partial data must prominently show its denominator and cannot silently become the latest complete release.

## 10. Implementation sequence and acceptance gates

| Milestone | Work | Exit condition |
| --- | --- | --- |
| M1: Corpus/contracts/isolation | Certify active IDs and substitutes, public navigation/API schemas, trusted regression commands and whole-filesystem leakage scans. | Reference/no-op and repeated-run checks work; no evaluator/history/installed-target leakage; engine/container/host boundaries explicit. |
| M2: Existing-agent adapters and broker | Launch pinned mini-SWE-agent and containerized engine; implement shared broker/reservations, native routing, fixed main-tree submission policy and sealing. | Both native systems pass symmetric continuation probes; concurrent spend cannot bypass the ledger; no custom agent/baseline wrapper. |
| M3: Live calibration | Use the $8 allocation for compatibility debugging and two representative development tasks with both systems; measure throughput/latency/cost. | Faithful versions/model mode selected; completed calibration supports the shared $1 envelope; W and request timeouts recorded; otherwise stop before measurement. |
| M4: Freeze and preregister | Commit active corpus, exclusions, refs/graders, schedule, model/provider/platform, prompt settings, metrics and retry/submission rules. | Publish a timestamped commit/tag and record its hash **before** measured inference. Independent review recorded, or author-bias limitation explicit. |
| M5: Measured campaign | Run 16 tasks × two systems serially, with declared eligible recovery only. | 32 scheduled trials accounted for; evaluated/delivery denominators and all bills reconcile; API ≤$45 and total outlay ≤$50. |
| M6: Minimal results site | Validated import, overview/paired counts, simple graphs/table, task explorer, downloads and methodology. | Display matches source data; accessible/mobile/empty states reviewed; production build passes; historical URLs preserved. |
| M7: Release/reproduction | Offline rebuild rehearsal, locked rerun protocol, immutable source/trace/patch/report artifacts. | Artifact rebuild deterministic; live rerun prerequisites/cost drift explicit; no promise of identical hosted-model outcomes or prices. |

Implement as separate reviewable changes in benchmark, engine and pages subrepos. M6 can proceed with explicitly labeled development fixtures after the schema stabilizes; these must never be presented as measured results. The final release depends on M5 and clean reproduction.

Harness tests should target meaningful risks: grading accepts the reference and rejects no-op/protected edits; evaluator isolation; invalid mutants do not count; concurrent reservations cannot overspend; schedule/resume has no duplicate IDs; model mismatch stops release eligibility; $45 campaign accounting includes development/calibration, recovery and in-flight requests; failure costs are included; zero successes and missing cost are handled; evaluated/delivery denominators and paired exclusions are correct; malformed exports fail import. Do not test the plan document itself or spend live API credits on routine CI.

## Definition of done

- A public harness subrepo follows `scripts/new` and is recorded as a submodule.
- Sixteen independently certified tasks across five repos and four categories, with complete provenance and reproducible environments.
- A pinned existing mini-SWE-agent baseline and one engine-core profile, with zero ablations and the same dated DeepSeek model/provider, reasoning settings and spend envelope; no second home-grown coding-agent implementation.
- All 32 scheduled primary trials and any eligible recovery executions accounted for, with live-development spending separate, no repeat slice, and all failures/regressions retained.
- One live protocol command with explicit hosted-service prerequisites and deterministic offline report regeneration; immutable result table, traces, patches/answers, checksums and a short observations report.
- The results page places paired-count summaries and simple graphs before the repository/task explorer and offers downloads; detailed attempt comparison/timelines are deferred.
- No automated PRs or detailed interactive attempt views in v1; existing historical results URLs continue to work.
- Total spending stays within $50 including credit-purchase fees; local compute and deterministic grading avoid additional service costs.
- No claim that external leaderboard scores predict CodeLoom success or identify a universally perfect model; no claim of an individual component effect is made without its own ablation; no benchmark claim comes from invented numbers, cherry-picked repeats, unverified agent reports, or PR acceptance.


## Implementation record — 14 October 2026

The implemented command is `./workspace/benchmark/scripts/benchmark run --run-dir runs/v1`. Offline report rebuilding uses the `report` subcommand and website import uses `import-site`. The new public sub-repository was created with the project's `./scripts/new benchmark --public` workflow.

The active suite is 16 independent tasks across TinyDB, python-dotenv, p-queue and chi, with one bug, feature, navigation and test-generation task per repository. Ky's four candidates were excluded during authoring because the trusted reference suite required additional browser engines and failed strict timing checks under AMD64 emulation. The originally named reserves `tinydb-N`, `dotenv-T`, `pqueue-B`, and `chi-X` were activated before any freeze or model calls. Raw exclusion records are retained; an early Ky navigation rubric failure was an authoring check, not agent performance. All four bugs are explicitly fabricated, with upstream tests retained.

Navigation references were authored with Codex assistance and checked against pinned source. Independent human review is still absent; the implementation does not claim the planned independent equivalence calibration took place.

The broker, two native adapters, sealed-output grading, mutation grading, accounting and offline report/site exports are implemented. Runtime routing/refusal/idle-barrier changes are isolated in the Engine revision `64ee57f`. The results page now places the overview, paired outcomes, category graph, metrics table, filterable tasks and downloads before existing repository case studies. Synthetic adapter traces are labeled and excluded from results.

No live calibration or measured calls have run, and no API money has been spent. Native synthetic continuation checks identify required DeepSeek reasoning state lost by Engine, while mini-SWE-agent's native LiteLLM OpenRouter backend retains the required `reasoning_content`. The configured Baidu endpoint's observed price also exceeds the original admission rates. These gates stop the campaign before credentials are used. There is no preregistration tag or claim of a completed study: a reviewed native compatibility revision, provider/rate decision, successful live calibration and clean preregistration remain prerequisites. See the benchmark repository's README and versioned setup reports for actual current status.

### Results explorer update — 14 October 2026

The overview now adds a clickable repository/category coverage matrix, planned budget donut, recorded setup gates and a cost/time scatter plot that remains empty until scored attempts exist. A run catalog opens offline inspectors with filterable model/tool timelines, spawned-agent parent relationships, wire responses and recorded effort, request latency, deterministic grader output and submitted patches. Both existing synthetic adapter checks are available and explicitly excluded from model-performance results.

Historical reports lead with per-phase cost, published tool activity and finding-severity charts, followed by concise strengths/concerns. Original prompts and review narratives remain expandable. Missing per-call logs are disclosed; conflicting run-summary and agent-phase tool totals are preserved and labeled rather than reconciled by assumption. Reported historical elapsed values are retained as reported.

The export is deterministic and requires no inference. Validation: 31 harness tests, Ruff, the production Next build, archive/checksum checks, and browser interaction checks; overview, inspector and report layouts were checked at phone widths, including a fixed landscape chart-legend overflow. Grading rules are unchanged. Paid calibration and measured runs remain paused pending Engine thinking/effort changes and explicit owner approval; no paid calls were made for this update.
