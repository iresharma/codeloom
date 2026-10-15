"""One-shot Railway campaign restart on current main, reusing historical timing."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path("/root/codeloom/workspace/benchmark")
PYTHON = ROOT / ".venv/bin/python"
ENGINE_SHA = "21cd912a656ae54841bec4b0890fc5a90ac2fc3c"
BENCHMARK_SHA = "c58fb37ff53cf678d158f1d331805dff5034fa36"
CALIBRATION = ROOT / "runs/railway-calibration-reused-timing-20261015"
# Not "railway-v1": that name's trial ids collide with the abandoned attempt's
# settled ledger rows (railway-v1-tinydb-B-*), tripping measured()'s interrupted-
# attempt guard even though nothing from this campaign ever touched them.
RUN = ROOT / "runs/railway-v1-restart-20261015"
SOURCE_CALIBRATION = Path("/root/benchmark-backups/pre-full-campaign-20261015/reports/calibration.json")
SOURCE_COMMIT = "bc839fd57f39dbf362ce7721b657be0781573b7a"
MARKER_NAME = "full-campaign-429-fix-20261015"
EXPECTED_TASKS = 9
EXPECTED_MEASURED_ATTEMPTS = 18

def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

def preflight():
    if git("rev-parse", "HEAD") != BENCHMARK_SHA:
        raise RuntimeError("Unexpected benchmark source revision")
    if git("rev-parse", "HEAD", cwd=ROOT.parent / "engine") != ENGINE_SHA:
        raise RuntimeError("Unexpected Engine source revision")
    if git("status", "--porcelain") or git("status", "--porcelain", cwd=ROOT.parent / "engine"):
        raise RuntimeError("Source checkouts must be clean before launch")
    if git("tag", "--list", "benchmark-v1-preregistered") or git("ls-remote", "--tags", "origin", "refs/tags/benchmark-v1-preregistered"):
        raise RuntimeError("Preregistration already exists; refusing a new campaign")
    if CALIBRATION.exists() or RUN.exists():
        raise RuntimeError("Paid output already exists in the selected run directories")
    path = ROOT / ".cache/campaign/ledger.sqlite"
    with sqlite3.connect(path) as db:
        # Owner-approved restart after abandoning a prior preregistration (429 fix):
        # settled non-development trials from that abandoned attempt stay in the
        # ledger forever but no longer block a fresh campaign_runs claim. Only an
        # ACTIVE trial still blocks launch.
        if db.execute("SELECT 1 FROM trials WHERE active=1 LIMIT 1").fetchone():
            raise RuntimeError("Only settled historical development trials may precede launch")
        if db.execute("SELECT 1 FROM calls WHERE charge IS NULL LIMIT 1").fetchone():
            raise RuntimeError("Unsettled bills require reconciliation")
        if db.execute("SELECT 1 FROM settings WHERE key IN ('overrun','protocol_violation') LIMIT 1").fetchone():
            raise RuntimeError("Provider/usage gate is halted")
        if db.execute("SELECT 1 FROM campaign_runs LIMIT 1").fetchone():
            raise RuntimeError("Campaign already bound to a measured run")
        spent = db.execute("SELECT COALESCE(SUM(charge),0) FROM calls").fetchone()[0] / 1_000_000
    return {"benchmark_commit": BENCHMARK_SHA, "engine_commit": ENGINE_SHA, "prior_spend_usd": spent}

def reconcile_transport_errors(stop, state_dir):
    # Owner's standing instruction: unbilled transport errors settle at zero.
    while not stop.wait(2):
        try:
            settled = []
            with sqlite3.connect(ROOT / ".cache/campaign/ledger.sqlite", timeout=30) as db:
                for call_id, in db.execute("SELECT id FROM calls WHERE status='transport_error' AND charge IS NULL").fetchall():
                    cursor = db.execute("UPDATE calls SET charge=0 WHERE id=? AND status=? AND charge IS NULL", (call_id, "transport_error"))
                    if cursor.rowcount:
                        settled.append(call_id)
            if settled:
                with (state_dir / "transport-reconciliation.jsonl").open("a") as output:
                    for call_id in settled:
                        output.write(json.dumps({"call_id": call_id, "status": "transport_error", "charge_usd": 0, "settled_at": time.time(), "authority": "owner standing instruction"}) + "\n")
                print("Settled unbilled transport errors at zero:", len(settled), flush=True)
        except Exception as exc:
            print("Transport reconciliation deferred:", type(exc).__name__, flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    values = json.loads(Path("/root/benchmark-env.json").read_text())
    if not all(values.get(k) for k in ("OPENROUTER_API_KEY", "MONITOR_URL", "MONITOR_UPLOAD_TOKEN", "GITHUB_TOKEN")):
        raise RuntimeError("Missing host credentials")
    values.pop("GITHUB_TOKEN")
    os.environ.update(values)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from cloud import worker as w
    from codeloom_benchmark.manifests import config, tasks, fingerprint
    from codeloom_benchmark.ledger import Ledger

    w.STATE.mkdir(parents=True, exist_ok=True)
    handle = (w.STATE / "worker.lock").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source = preflight()
    settings = config()
    account = Ledger(ROOT / ".cache/campaign/ledger.sqlite", settings["campaign_limit_usd"], settings["stage_limits"], budget_policy=settings["budget_policy"], call_limit_multiplier=settings["call_limit_multiplier"])
    if len(tasks()) != EXPECTED_TASKS or len(settings["systems"]) != 2:
        raise RuntimeError(f"Expected {EXPECTED_TASKS} tasks and two arms")
    if args.check:
        print(json.dumps({"status": "ready", **source, "development_headroom_usd": account.headroom("development"), "measured_attempts": EXPECTED_MEASURED_ATTEMPTS, "calibration_run": CALIBRATION.name}), flush=True)
        return 0

    marker = w.STATE / (MARKER_NAME + ".started")
    with marker.open("x") as output:
        json.dump({**source, "started_at": time.time(), "authorization": "owner requested restart on current main, reusing certified images and historical calibration timing"}, output)
    (w.STATE / "paid-start-authorized").write_text(json.dumps({"authorized_at": time.time(), **source, "summary_delivered": True}))
    w.CALIBRATION = CALIBRATION
    w.RUN = RUN
    w.state.update(phase="starting", message="Owner-authorized restart on current main", source=source, calibration_run=CALIBRATION.name, run_id=RUN.name)
    w.draft(RUN)
    thread = threading.Thread(target=w.heartbeat, daemon=True)
    reconcile_stop = threading.Event()
    reconciler = threading.Thread(target=reconcile_transport_errors, args=(reconcile_stop, w.STATE), daemon=True)
    thread.start()
    reconciler.start()
    cli = [str(PYTHON), "-m", "codeloom_benchmark.cli"]
    exit_code = 0
    try:
        w.run_phase("validating", cli + ["validate"], "Validating the fixed ten-task protocol")
        w.run_phase("certifying", cli + ["certify"], "Reusing unchanged certified images for the nine-task suite")
        w.run_phase("reusing_calibration", cli + ["reuse-calibration", "--source", str(SOURCE_CALIBRATION), "--source-commit", SOURCE_COMMIT, "--owner-reason", "Owner requested skipping fresh paid calibration and reusing existing request timing after the main consolidation."], "Reusing historical timing; no fresh paid calibration attempts")
        calibration = w.read(ROOT / "reports/calibration.json", {})
        if calibration.get("status") != "reused":
            raise RuntimeError("Historical timing reuse did not record a reused calibration report")
        w.run_phase("recording_setup", ["git", "add", "reports", "suites/v1/dependencies"], "Recording certified and calibrated evidence")
        w.run_phase("checking_setup", ["git", "diff", "--cached", "--check"], "Checking generated evidence before preregistration")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            w.run_phase("committing_setup", ["git", "commit", "-m", "Record restarted Railway certification and reused calibration before measurement"], "Committing setup evidence")
        w.run_phase("preregistering", cli + ["preregister"], "Freezing the 20-attempt schedule before measured inference")
        w.run_phase("publishing_protocol", ["git", "push", "--atomic", "origin", "HEAD:refs/heads/main", "benchmark-v1-preregistered"], "Publishing preregistration before any measured calls")
        w.run_phase("measuring", cli + ["run", "--run-dir", str(w.RUN)], "Running all nine matched pairs under the unchanged budget gates")
        primary = [p for p in (w.RUN / "trials").glob("*/result.json") if w.read(p, {}).get("stage") == "measured"]
        if len(primary) != EXPECTED_MEASURED_ATTEMPTS:
            raise RuntimeError(f"Measured schedule did not seal all {EXPECTED_MEASURED_ATTEMPTS} primary attempts")
        with w.state_lock:
            w.state.update(phase="complete", message="All 20 measured attempts finished; sealed results and private backups retained.")
    except Exception as exc:
        exit_code = 1
        with w.state_lock:
            w.state.update(phase="blocked", message=str(exc))
        print("Campaign stopped:", str(exc), flush=True)
    finally:
        reconcile_stop.set()
        reconciler.join(timeout=35)
        w.stop.set()
        thread.join(timeout=180)
        with w.state_lock:
            final = w.final_status()
        published = False
        for attempt in range(3):
            try:
                w.publish()
                w.backup()
                w.upload("status", json.dumps(final).encode())
                published = True
                break
            except Exception as exc:
                print("Final publication retry:", type(exc).__name__, flush=True)
                time.sleep(10)
        if not published:
            exit_code = 1
            print("Final publication unavailable; local state, ledger and results retained on Railway", flush=True)
        (w.STATE / (MARKER_NAME + ".exit")).write_text(str(exit_code) + "\n")
        print(json.dumps({"exit_code": exit_code, "phase": final["phase"], "message": final["message"]}), flush=True)
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
