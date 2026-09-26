#!/usr/bin/env python3
"""Write run_manifest.json + transcript.md for a run whose engine phase
finished but whose post-run step crashed (e.g. harness Python lacking the
engine's deps). Run with a Python that can import the engine:

    <venv>/bin/python pr_trials/recover_manifest.py --run-id tracer --started-at 2026-09-26T01:07:42Z

Does NOT rerun the engine. The manifest is marked `recovered: true`;
finished_at is the engine log's mtime.
"""
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_target as rt  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--run-id", required=True)
p.add_argument("--started-at", required=True, help="ISO UTC, e.g. 2026-09-26T01:07:42Z")
p.add_argument("--pushgateway-url", default="http://localhost:9091")
a = p.parse_args()

targets = {t["run_id"]: t for t in json.loads((rt.REPO_ROOT / "pr_trials/targets.json").read_text())["targets"]}
t = targets[a.run_id]
workdir = rt.REPO_ROOT / "pr_trials" / "runs" / t["name"]
repo = workdir / "repo"
started = datetime.fromisoformat(a.started_at.replace("Z", "+00:00")).timestamp()
finished = (workdir / "engine.log").stat().st_mtime
list_sessions, load_snapshot = rt._sqlite_funcs()
result = rt.collect_result(repo, list_sessions, load_snapshot)
prom = rt.scrape_pushgateway(a.pushgateway_url, "pr-trials", a.run_id)
base = rt._git(["rev-parse", "HEAD~0"], cwd=repo) if False else json.loads(
    (workdir / "run_manifest.json").read_text()).get("target_commit_base", "") if (workdir / "run_manifest.json").exists() else ""
prompt = (rt.REPO_ROOT / t["prompt_file"]).read_text()
manifest = {
    "run_id": a.run_id, "repo_url": t["repo_url"], "workdir": str(workdir),
    "task_prompt": prompt, "started_at": started, "finished_at": finished,
    "elapsed_wall_s": finished - started,
    "status": "ok" if result.pr_url else "failed:no-pr", "detail": "",
    "engine_commit": rt._git_ok(["rev-parse", "HEAD"], cwd=rt.ENGINE_DIR),
    "target_commit_base": base, "settle": t.get("settle", "pr"),
    "recovered": True,
    "stats": {k: getattr(result, k) for k in (
        "session_id", "cost", "prompt_tokens", "completion_tokens", "cached_tokens",
        "total_tokens", "requests", "turns", "tool_calls", "elapsed_s", "agents",
        "files_changed", "diffstat", "error")},
    "prometheus_metrics": prom, "pr_url": result.pr_url, "last_reply": result.last_reply,
    "paths": {"repo": str(repo), "session_db": str(repo / ".engine/session.db"),
              "trace_jsonl": str(repo / ".engine/trace.jsonl"),
              "engine_log": str(workdir / "engine.log"), "client_log": str(workdir / "client.log")},
}
(workdir / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
rt._write_transcript(repo, workdir / "transcript.md", list_sessions, load_snapshot)
print(f"[{a.run_id}] recovered: pr_url={result.pr_url or '-'} cost={result.cost}")
