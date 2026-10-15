**Status: resolved and published, 2026-10-16.** All items below are fixed in
`codeloom.benchmark` (`bcc59e9`) and `codeloom.pages` (`609ef1b`), live at
https://codeloom.iresharma.com/engine/results. Final verdict: 8/9 pass each
system, `chi-N` the sole fail for both (owner-approved: `task_id` strictness
stands, `allowed_paths` strictness does not). See commits for the exact
reasoning on each call.

**Follow-up hardening, 2026-10-16** (commit TBD at push time): the owner
asked to fix the remaining known gaps for the *next* campaign (the fixes
above only covered the already-run v1 report; they couldn't change
`codeloom_benchmark/*.py` without invalidating that campaign's
preregistration). Home-k8s migration was considered and explicitly
cancelled — staying on Railway.

- `runner.py` now sends `disclosed_prompt(task)` instead of the raw
  `task["prompt"]` to both agents: states the real `allowed_paths` globs,
  and for navigation-category tasks, the exact `task_id` string the schema
  requires. This is the root fix for the `tinydb-X`/`chi-N` class of bug —
  future agents can actually comply with constraints that were previously
  silent grading-time-only rules.
- `broker.py`'s `BudgetRefused` handler now writes `exc.reason` directly as
  the journal `status` instead of collapsing every reason but
  `call_cost_limit`/`token_denied` into generic `"budget_exhausted"`.
  `ledger.py`'s reasons (`overrun_halt`, `protocol_violation_halt`,
  `unknown_bills_halt`, `budget_pending`, plus the pre-existing ones) now
  flow through end to end.
- `runner.py`'s termination-override only treats genuine admission-halt
  reasons (`ADMISSION_HALT_STATUSES`: `budget_exhausted`, `overrun_halt`,
  `protocol_violation_halt`, `unknown_bills_halt`) as overriding a
  controller's own outcome. `budget_pending` — the same-trial in-flight-bill
  race that caused `dotenv-X`/`engine-core`'s mislabel — is deliberately
  excluded: it resolves on its own and must not stomp a `completed`
  termination the controller already reported.
- Every `attempt()` result now carries `admission_events`: the full set of
  distinct non-`complete` journal statuses for that trial, so a mix of
  events isn't silently collapsed to whichever one happened to win the
  (still single-valued) `termination` field's priority chain.
- Explicitly left as a documented limitation, not fixed: Engine's
  `completed` termination is only `process.returncode == 0`, not a check
  that its internal tester/reviewer sub-agents actually ran (see
  `railway-benchmark.md`'s 2026-10-15 entry) — fixing this needs parsing
  `engine-events.jsonl` for expected sub-agent completion events, which is
  new scope beyond what was found this session.

**Certification speed, 2026-10-16** (separate follow-up): `Dockerfile` used
to `COPY engine /opt/engine` into the shared base/task image layers, so
*any* Engine commit change produced a new base image digest and forced a
full recertification of every task, even though certification never
actually runs Engine at all (noop/reference/mutant checks are pure fixture
validation against generic language tooling). Fixed: Engine's source is now
bind-mounted into `/opt/engine` at `docker run` time (`sandbox.py:
start_container`, used for `engine-core` attempts only) instead of baked
into the image. `prepare_runtime` still records `engine_commit`/
`engine_files`/`engine_dirty` in `.cache/runtime.json` for provenance; since
the checkout can now drift between certify-time and run-time (previously
impossible by construction), `start_container` enforces that the mounted
Engine commit matches what was certified and is clean, refusing otherwise.
Verified end to end on the Railway sandbox: the runtime image digest is
byte-identical before and after an Engine-only source change, the mount
correctly exposes the full Engine tree inside the container, and a
drifted/dirty checkout is correctly refused.

# Pre-publish corrections for `runs/railway-v1-restart-20261015`

Owner instruction (2026-10-15): keep a running note of every harness bug and
mislabeled result found while the v1 measured campaign is live, and apply all
of them before `import-site`/publish to codeloom.iresharma.com. Do not block
the live run on these; fix and re-grade/re-report before publication.

## Fixed, already live in this run (`codeloom.benchmark` commit `c58fb37`)

1. **Unbilled 429/408 poisoned the whole campaign's admission.** A
   pre-generation rate-limit/timeout rejection left `charge=NULL` in the
   ledger; `reserve()`'s blanket "any unknown bill halts every trial" gate
   then refused admission campaign-wide, not just for the trial that hit it.
   Fix: settle these at $0 inline in `broker.py` (nothing is billed before
   generation starts).
2. **`engine-core` ran with `OPENROUTER_DISABLE_RETRIES=1`**, disabling the
   Engine's own backoff retry (500ms-8s, up to 60s) and making a single
   upstream 429 kill the whole attempt (`HeadlessError: engine went idle
   without a final assistant reply`) instead of being absorbed. Removed in
   `runner.py`; each retry still hits our broker as its own request, so
   ledger accounting is unaffected.

## Found, NOT yet fixed (still live risk in the running campaign)

3. **Summary table requires `termination == "completed"` AND
   `verification.pass`** (`reporting.py:94-99`) to show "pass". Any trial
   whose journal contains even one transient `budget_exhausted`-labeled call
   — anywhere, even mid-run, even if the agent fully recovered and finished
   cleanly — gets its real `completed` termination overwritten and shows as
   "fail" in the summary table, while the per-attempt detail page (reading
   `verification.pass` directly) correctly shows "pass". Confirmed instance:
   `dotenv-X` / `engine-core` — controller log ends `{"termination":
   "completed"}`, `verification.pass: true`, cost $0.728 (well under the $4
   heavy cap), yet the table shows "fail".
4. **Root cause of #3: `broker.py` collapses every `BudgetRefused` reason
   except `call_cost_limit`/`token_denied` into one generic `budget_exhausted`
   status.** This conflates at least four different conditions: real
   actual-spend-cap exhaustion, the call-size-vs-3x-multiplier gate, the
   unknown-bills halt, and `reserve()`'s own-trial "waiting for this trial's
   in-flight bill" concurrency guard (`refusal_reason: budget_pending`) — a
   benign same-trial race, not a budget problem at all. `dotenv-X`/
   `engine-core` hit exactly this at call 31 of 237 and recovered on its own.
   `runner.py`'s termination-override logic then treats *any* occurrence of
   the generic status as if it defines the whole trial's outcome.
5. **Neither system is told `allowed_paths`.** `runner.py:attempt()` builds
   `cfg` once with `prompt=task["prompt"]` verbatim and shares it between
   `engine-core` and `mini-swe-agent` — `allowed_paths` is never injected
   into what either agent sees; it is used only at grading time
   (`fixtures.py:186`). Some task prompts happen to restate the real
   constraint (e.g. `pqueue-T`: "Only add tests under `benchmark_tests/`."),
   others do not. Confirmed instance: `tinydb-X` / `engine-core` added
   `TinyDB.flush()`/`Storage.flush()` correctly, and also documented them in
   `docs/api.rst` (verified correct: `TinyDB` section uses
   `.. autoclass:: tinydb.database.TinyDB` + `:members:`, so the added
   docstring is auto-picked up by Sphinx; the `Storage.flush()` entry was
   added by hand in the existing hand-written `Storage` class block, matching
   the file's own pattern for `close()`/`read()`/`write()` exactly). This
   edit is valid, not a mistake — `disallowed_edit` should not have failed
   it. Symmetric between both systems (neither is told the constraint), so
   it isn't unfair *between* engine-core and mini-swe-agent, but it does
   mean some `disallowed_edit` fails are not genuine capability failures.

## Before publishing

- Fix #3/#4 in `broker.py`/`runner.py`/`reporting.py`: keep the specific
  `refusal_reason` (`budget_pending`, `call_cost_limit`, `token_denied`,
  actual-spend-exhausted, unknown-bill-halt) distinct through to the journal
  and the report, and stop a transient recovered blip from overwriting a
  genuinely `completed` termination.
- Decide and implement a principled fix for #5 (likely: either disclose
  `allowed_paths` to the agent, or validate out-of-scope edits for actual
  correctness/relevance before failing on `disallowed_edit`, matching the
  "valid edit -> pass, invalid edit -> fail" rule the owner gave for
  `tinydb-X`/`engine-core`) — then re-run `fixtures`/grading affected by it.
- Re-grade/re-report every sealed trial in `runs/railway-v1-restart-20261015`
  under the corrected code before `import-site`/publish. Do **not** silently
  hand-edit individual `result.json` files; fix the code and regenerate.
- Audit for any further `budget_exhausted`-labeled trials beyond the ones
  already found (`dotenv-X`/`engine-core`, mislabeled; `tinydb-X`/
  `engine-core`, genuine `disallowed_edit` fail pending the #5 decision) as
  the remaining attempts seal.
