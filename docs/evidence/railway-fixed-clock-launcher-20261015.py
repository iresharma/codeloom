"""One-shot Railway campaign using the repository's fixed gates and monitor."""
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
ENGINE_SHA = "677cfbeca7525ae4e66475be0566703d7b1831f0"
BENCHMARK_SHA = "ad49a4dbba4272d7fe314669828608591bb0f6bf"
CALIBRATION = ROOT / "runs/railway-calibration-fixed-clock-20261015"

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
    if CALIBRATION.exists() or any((ROOT / "runs/railway-v1/trials").glob("*/attempt.json")):
        raise RuntimeError("Paid output already exists in the selected run directories")
    path = ROOT / ".cache/campaign/ledger.sqlite"
    with sqlite3.connect(path) as db:
        if db.execute("SELECT 1 FROM trials WHERE active=1 OR stage!='development' LIMIT 1").fetchone():
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
    # Keep every ledger row and guard each update against concurrent settlement.
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
    if account.headroom("development") < 6:
        raise RuntimeError("Insufficient development allocation for four $1.50 attempts")
    if len(tasks()) != 10 or len(settings["systems"]) != 2:
        raise RuntimeError("Expected ten tasks and two arms")
    if args.check:
        print(json.dumps({"status": "ready", **source, "development_headroom_usd": account.headroom("development"), "measured_attempts": 20, "calibration_run": CALIBRATION.name}), flush=True)
        return 0

    # Dedicated marker is exclusive; the historical supervisor markers remain intact.
    marker = w.STATE / "full-campaign-fixed-clock-20261015.started"
    with marker.open("x") as output:
        json.dump({**source, "started_at": time.time(), "authorization": "owner requested full unattended certification, calibration and all tasks"}, output)
    (w.STATE / "paid-start-authorized").write_text(json.dumps({"authorized_at": time.time(), **source, "summary_delivered": True}))
    w.CALIBRATION = CALIBRATION
    w.state.update(phase="starting", message="Owner-authorized full unattended sequence", source=source, calibration_run=CALIBRATION.name, run_id=w.RUN.name)
    w.draft(w.RUN)
    w.draft(w.CALIBRATION)
    thread = threading.Thread(target=w.heartbeat, daemon=True)
    reconcile_stop = threading.Event()
    reconciler = threading.Thread(target=reconcile_transport_errors, args=(reconcile_stop, w.STATE), daemon=True)
    thread.start()
    reconciler.start()
    cli = [str(PYTHON), "-m", "codeloom_benchmark.cli"]
    exit_code = 0
    try:
        w.run_phase("validating", cli + ["validate"], "Validating the fixed ten-task protocol")
        w.run_phase("certifying", cli + ["certify"], "Rebuilding the pinned runtime and certifying all ten fixtures")
        w.run_phase("compatibility", cli + ["compatibility"], "Checking native reasoning and model/effort fidelity without paid inference")
        w.run_phase("calibrating", cli + ["calibrate", "--run-dir", str(w.CALIBRATION)], "Four fresh development attempts on the fixed runtime")
        calibration = w.read(ROOT / "reports/calibration.json", {})
        if calibration.get("status") != "passed" or calibration.get("suite_fingerprint") != fingerprint()[0]:
            raise RuntimeError("Fresh calibration did not pass on the current fingerprint")
        w.run_phase("recording_setup", ["git", "add", "reports", "suites/v1/dependencies"], "Recording certified and calibrated evidence")
        w.run_phase("checking_setup", ["git", "diff", "--cached", "--check"], "Checking generated evidence before preregistration")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            w.run_phase("committing_setup", ["git", "commit", "-m", "Record full Railway certification and final calibration before measurement"], "Committing setup evidence")
        w.run_phase("preregistering", cli + ["preregister"], "Freezing the 20-attempt schedule before measured inference")
        w.run_phase("publishing_protocol", ["git", "push", "--atomic", "origin", "HEAD:refs/heads/codex/railway-benchmark", "benchmark-v1-preregistered"], "Publishing preregistration before any measured calls")
        w.run_phase("measuring", cli + ["run", "--run-dir", str(w.RUN)], "Running all ten matched pairs under the unchanged budget gates")
        primary = [p for p in (w.RUN / "trials").glob("*/result.json") if w.read(p, {}).get("stage") == "measured"]
        if len(primary) != 20:
            raise RuntimeError("Measured schedule did not seal all 20 primary attempts")
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
        (w.STATE / "full-campaign-fixed-clock-20261015.exit").write_text(str(exit_code) + "\n")
        print(json.dumps({"exit_code": exit_code, "phase": final["phase"], "message": final["message"]}), flush=True)
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
