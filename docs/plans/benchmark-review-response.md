# Benchmark review decisions

14 October 2026. These decisions revise [the implementation plan](benchmark-implementation.md); they are design decisions, not results of running the benchmark. No live model calls were made for this revision.

The release now has **16 tasks and 32 measured trials**, with no repeat slice. The budget is **$8 live development/calibration + $32 measured inference + $5 infrastructure recovery + $5 payment headroom = $50**. The same existing agents and provisional DeepSeek Pro model choice remain. Hosted-service prices and faithful integration still require preflight validation.

| Review point | Decision |
| --- | --- |
| 1. Cost/time calibrated on tiny tasks | Use two full-size development tasks with both systems. Measure throughput, delays, costs and completed duration. Freeze a shared watchdog from the longer completed calibration, with a 60-minute floor. A capped calibration is only a lower bound; do not proceed on unsupported affordability claims. |
| 2. Installed-package solution leaks | Scan all agent-visible filesystems, package installations, caches, archives and LSP paths. Explicit build staging and `.dockerignore` exclude evaluator/reference material. Replace a target if unavoidable runtime dependencies leak another implementation. |
| 3. Unspecified engine sandbox | Scheduler, mini controller and inference broker run on the trusted host. Engine/LSP/children and all model-visible commands run in task containers. Evaluator is separate. No Docker socket or host-home mounts. Pin linux/amd64 and report emulation. |
| 4. Budget in the wrong process | One host broker owns reservations and per-trial credentials for both systems and all child calls. Engine only needs routing/refusal handling, not its own spend ledger. No custom baseline provider wrapper. |
| 5. Asymmetric reasoning fixes | Select compatible native versions/backends/model mode before freeze. Apply identical conformance probes. No one-arm core reasoning patch during calibration; stop if faithful integration cannot fit the allocation. |
| 6. Ambiguous final patch | Grade only main's allowed diff and answer artifact. Settlement prompts receive a fixed merge-to-isolated-main answer. Unmerged child work is unsubmitted, retained only for diagnostics. |
| 7. Navigation format sensitivity | Public JSON schema and offline format validator in both prompts/containers. Freeze private fact/symbol equivalences using human-written answers, never measured-model answers on release tasks. |
| 8. Flaky tests and weak mutants | Three runs on correct code and each mutant; consistent assertion failures only. Framework-specific pytest/AVA/Go parsing. Five-run fixture certification, local timers/services, browser dependency check and named replacement targets. |
| 9. Hidden API contracts | Public feature briefs pin exports, names, signatures/types, defaults, errors and compatibility before hidden tests are frozen. |
| 10. Bug provenance inconsistency | Historical pre-fix snapshots preferred; fabricated regression only as labeled fallback. Keep upstream tests intact even if they expose the bug; disclose setup and preexisting failures. PR automation is removed. |
| 11. Misleading precision/reliability | Headline engine-only/mini-only/both-pass/both-fail counts with unresolved pairs. No headline CIs or significance claims. State within-repo correlation and lack of a run-variability estimate. |
| 12. Design-choice claims without ablations | Rename the section “Observations from traces”; explanations are hypotheses. |
| 13. Infrastructure retry ambiguity | At most one fresh retry for a confirmed provider/host failure, charged to recovery. Retry outcome becomes evaluated result; unresolved infrastructure excluded from that denominator but counted in delivery out of 16. Agent failures are never retried. All originals remain public. |
| 14. Excess release scope | Defer owned-repo PR automation, attempt comparisons and interactive trace timelines. Ship an overview, simple graphs/table, task explorer and downloads. |
| 15. Thin live-development allowance | Reserve $8 explicitly for representative calibration and real integration debugging. Remove the repeat slice now; no hidden additional model spend. |
| 16. Overstated reproduction | Guarantee deterministic offline reporting from sealed artifacts. Live reruns execute the pinned protocol when hosted services permit; outputs/prices/availability are not guaranteed. Substitutions create a new campaign. |

Smaller points are also resolved: IDs now use `repo-category`; exactly `tinydb-N`, `dotenv-T`, `pqueue-B`, and `chi-X` are deferred, leaving four tasks per category. Publish a timestamped preregistration commit with suite/schedule/metrics before measured inference. Seek independent task review, or disclose the engine author's category-selection bias. Record provider throttling, queue/backoff time and child concurrency separately.

The provisional model recommendation is preserved rather than treating this feedback as a fresh model-ranking exercise. The plan's external numbers remain dated research observations; compatibility, prices and full-size feasibility are implementation gates, not facts established by this review revision.


Implementation note (14 October 2026): the four reserves above were activated after Ky reference browser/timing certification failed. The active suite remains 16 tasks, four per category, across four repositories. References were Codex-assisted rather than independently human reviewed; that limitation is now explicit. Native reasoning fidelity and current provider pricing failed their gates before any live spending.
