# pr_trials

Runs the engine, unattended, against real GitHub repos to produce real PRs,
then reviews each PR (code + the agent's actual tool-call/reasoning trace)
with `claude -p`. See `/Users/iresharma/.claude/plans/we-are-done-with-reactive-engelbart.md`
for the full design rationale.

## One-time setup

```bash
cp pr_trials/env.sh.example pr_trials/env.sh   # fill in ANTHROPIC_API_KEY
# OPENROUTER_API_KEY / TYPESAFE_JEV_API_KEY are already read from
# ../env.sh and ../workspace/engine/env.sh
```

Requires: `git`, `gh` (authenticated, `repo` scope), Docker running (for the
local Pushgateway; soft-fails to `prometheus_metrics: null` if unavailable),
and the `claude` CLI.

## Run everything overnight

```bash
./pr_trials/start_overnight.sh
```

Detached with `caffeinate` + `nohup`; safe to close the terminal. For each
target in `targets.json`: clones it, drives the engine headlessly with no
artificial timeout (waits for the real orchestrator-idle signal), opens a
real PR (`--settle pr`), then immediately reviews that PR with `claude -p`
the moment it lands — targets run in parallel and don't wait on each other.

Check progress:

```bash
cat pr_trials/overnight_status.json   # per-target stage
tail -f pr_trials/overnight.log
```

When done: `pr_trials/overnight_summary.md` (one line per target — PR URL,
report path) and the full JSON reports in `pr_trials/reports/`.

## Run one target manually

```bash
python3 pr_trials/run_target.py \
  --repo-url git@github.com:iresharma/reach-auth-proxy.git \
  --prompt-file pr_trials/tasks/reach-auth-proxy.md \
  --workdir pr_trials/runs/reach-auth-proxy \
  --run-id reach-auth-proxy --settle pr

python3 pr_trials/review_pr.py --workdir pr_trials/runs/reach-auth-proxy
```

## Layout

| Path | What |
|---|---|
| `targets.json` | The batch: repo URL, task prompt file, run id per target |
| `tasks/*.md` | The task prompt sent to the engine for each target |
| `run_target.py` | Clone + venv + boot engine + headless drive + settle PR + Prometheus scrape + `run_manifest.json` |
| `review_pr.py` | Assembles the evidence bundle, runs `claude -p`, assembles the final report |
| `review_prompt.md` | Instructions + schema given to `claude -p` |
| `run_all.py` | Parallel batch runner + chaining + `overnight_status.json` |
| `start_overnight.sh` | The one command |
| `runs/<name>/` | Evidence per run: repo clone, `.engine/session.db` + `trace.jsonl`, logs, `run_manifest.json`, `review_bundle/` (gitignored — regenerate by rerunning) |
| `reports/` | Final `<run_id>-<timestamp>.json` reports (kept) |
