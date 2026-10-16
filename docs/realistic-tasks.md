# Realistic multi-file tasks — smaller model run

Ten standalone coding tasks for monitoring a smaller model's real-world
capability: 6-7+ files each, across production code, tests and docs in a
real open-source repo (tinydb, python-dotenv, p-queue, chi — the same repos
the main v1 benchmark already uses).

This now runs **through the real harness**, on the fixed/latest
`codeloom.benchmark` + Engine code, and publishes to the **same monitor**
the v1 campaign used:

**https://benchmark-monitor-production.up.railway.app/**

It's a separate, standalone suite (`suites/realistic/`, run id
`realistic-v1`) — not part of the v1 preregistered campaign, no mutants, no
mutation-score grading, no per-difficulty budget caps. Grading is reference
+ a hidden oracle check per task (same tier as the harness's own
`dev-python`/`dev-typescript` calibration tasks). It uses its own dedicated
ledger (`runs/realistic-v1/ledger.sqlite`), isolated from the v1 campaign's
budget tracking, so it can't interfere with or be constrained by the real
benchmark's spend history.

## Status tracker

| # | Task id | Repo | Status | Notes |
|---|---------|------|--------|-------|
| 1 | `realistic-tinydb-trash` | tinydb | fixture landed | commit `3785875` |
| 2 | `realistic-tinydb-validator` | tinydb | fixture landed | commit `3785875` |
| 3 | `realistic-tinydb-cache` | tinydb | fixture landed | commit `3785875` |
| 4 | `realistic-dotenv-default` | dotenv | fixture landed | commit `d562e17` |
| 5 | `realistic-dotenv-override` | dotenv | fixture landed | commit `d562e17` |
| 6 | `realistic-dotenv-diff` | dotenv | fixture landed | commit `d562e17` |
| 7 | `realistic-pqueue-dedup` | pqueue | fixture landed | commit `3785875` |
| 8 | `realistic-pqueue-subqueue` | pqueue | fixture landed | commit `3785875`; implemented directly (the authoring agent was killed by the session's rate limit before reaching this task) |
| 9 | `realistic-chi-maxbytes` | chi | fixture landed | commit `d562e17` |
| 10 | `realistic-chi-tags` | chi | fixture landed | commit `d562e17` |

All 10 fixtures are landed, verified (parses, reconstructs byte-for-byte,
applies cleanly, full upstream suite + hidden oracle pass, oracle fails on
the unmodified baseline) and pushed to `codeloom.benchmark` main. The suite
validates and fingerprints cleanly; `pytest`/`ruff` on the harness itself
stay green. **Not yet done:** syncing to the Railway sandbox and actually
launching the `realistic-v1` campaign (real, paid LLM calls) — that still
needs an explicit go-ahead before it runs.

Live per-attempt status, cost and logs show up on the monitor once the run
starts. This table is for your own quick reference; update it from the
monitor/report, not the other way round.

## Using a smaller model

`configs/core.yaml`'s `model`/`provider` fields are the harness's pinned
model (currently the same DeepSeek v4 Pro the v1 campaign used) — shared
with the real benchmark, so don't change it permanently without knowing
that affects any future v1 run too. For a genuinely smaller/cheaper model,
either:
- Point `OPENROUTER_MODEL`/`configs/core.yaml` at it just for this run and
  revert after, or
- Note that `runs/realistic-v1`'s dedicated ledger (see below) still
  respects whatever model/provider is configured at the time it runs — the
  harness doesn't support per-suite model overrides, only global config.

## Running it

On the Railway sandbox (same place the v1 campaign ran), as a detached
process, same pattern as every other campaign launch this project has used:

```bash
railway sandbox exec --detach -- bash -c \
  "cd /root/codeloom/workspace/benchmark && \
   nohup /root/codeloom/workspace/benchmark/.venv/bin/python \
     /root/run-realistic-suite.py \
     > /root/railway-realistic-suite.log 2>&1 < /dev/null & disown"
```

Launcher script: [`docs/evidence/realistic-suite-launcher-20261016.py`](evidence/realistic-suite-launcher-20261016.py)
(copy it to `/root/run-realistic-suite.py` on the sandbox first). It:

1. Validates the `realistic` suite's fixtures.
2. Certifies all 10 tasks (builds their Docker images, checks the
   reference solution + hidden oracle actually pass) — reuses the real
   `certification()` pipeline, suite-bound to `realistic` for the
   duration of the call only; the v1 campaign's own
   `reports/certification.json` is backed up and restored around it, so
   this never disturbs the real benchmark's cached certification record.
3. Runs all 10 tasks × both systems (20 attempts) directly, publishing
   heartbeat/status/results to the monitor the whole time, same as any
   other campaign.

Fixtures live at `suites/realistic/tasks/*.yaml` with hidden oracle checks
at `evaluator/realistic/<task-id>/`. Both need to exist in the sandbox's
benchmark checkout before running — commit and push them to
`codeloom.benchmark` first, then pull on the sandbox, exactly like every
other harness change this project makes.

## Monitoring progress

Same as always — check `.cache/cloud/state.json` on the sandbox, or just
watch the monitor page; it publishes under run id `realistic-v1`, a
separate card/section from `railway-v1-restart-20261015`.

```bash
railway sandbox exec -- python3 -c "
import json
d = json.load(open('/root/codeloom/workspace/benchmark/.cache/cloud/state.json'))
print(d.get('phase'), d.get('message'))
print([a['id'] for a in d.get('active', [])])
"
```

## Task reference

Full task definitions (prompt, allowed paths, verification) are in the YAML
fixtures themselves (`suites/realistic/tasks/*.yaml`) — that's now the
source of truth, not this doc. Summary:

1. **tinydb-trash** — soft-delete: `Table.trash()`/`restore()`/`purge_trash()`
2. **tinydb-validator** — per-table schema validator hook, CachingMiddleware-aware
3. **tinydb-cache** — fix stale LRU query cache after deletes, add `cache_stats()`
4. **dotenv-default** — `${VAR:-default}` interpolation syntax
5. **dotenv-override** — layered `override_files` support
6. **dotenv-diff** — `dotenv diff file1 file2` CLI subcommand
7. **pqueue-dedup** — task deduplication by `id`
8. **pqueue-subqueue** — named sub-queues with per-group concurrency
9. **chi-maxbytes** — per-route `middleware.MaxBytes(limit)` 
10. **chi-tags** — route metadata/tagging + `RoutesWithTags()`
