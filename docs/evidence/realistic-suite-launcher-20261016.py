"""One-shot launcher for the standalone 'realistic' task suite (10 multi-file
tasks, see docs/realistic-tasks.md). Reuses the real, currently-fixed harness
code (certify/grade/broker/ledger/runner) and the SAME existing monitor the
v1 campaign publishes to -- just a different run_id, outside the v1
preregistered schedule and its budget tracking (stage 'development', the
same bucket smoke/calibration tasks already use). No preregistration, no
mutants, no per-difficulty budget caps -- this is exploratory, not a
scientific campaign.

Does not touch cli.py's CLI commands at all: it calls cli.certification()
with the suite-bound tasks()/validate_suite()/fingerprint() functions
monkeypatched to "realistic" for the duration of the call, then restores
them, and drives runner.attempt() directly for the measured loop (bypassing
campaign.measured()'s v1-specific pairing/schedule machinery entirely,
since that's not needed here).
"""
from __future__ import annotations
import functools
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/root/codeloom/workspace/benchmark")
SUITE = "realistic"
RUN = ROOT / "runs/realistic-v1"
PER_ATTEMPT_LIMIT_USD = 2.0  # generous for a 6-7 file feature task; development stage, not part of the v1 measured budget

# Python installs no handler for SIGTERM by default, so `kill <pid>` (unlike
# Ctrl-C/SIGINT) terminates the process immediately at the C level, skipping
# every try/finally -- including the one that restores the real v1
# campaign's reports/certification.json from its backup below. Convert it
# into an ordinary Python exception so that restore always runs.
def _raise_on_sigterm(signum, frame):
    raise SystemExit(1)


signal.signal(signal.SIGTERM, _raise_on_sigterm)


def main():
    values = json.loads(Path("/root/benchmark-env.json").read_text())
    if not all(values.get(k) for k in ("OPENROUTER_API_KEY", "MONITOR_URL", "MONITOR_UPLOAD_TOKEN")):
        raise RuntimeError("Missing host credentials")
    values.pop("GITHUB_TOKEN", None)
    os.environ.update(values)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from cloud import worker as w
    from codeloom_benchmark import cli
    from codeloom_benchmark.manifests import (
        tasks as real_tasks,
        validate_suite as real_validate_suite,
        fingerprint as real_fingerprint,
        config,
        model_metadata,
    )
    from codeloom_benchmark.campaign import credentials, parallel_pair
    from codeloom_benchmark.broker import Broker
    from codeloom_benchmark.ledger import Ledger
    from codeloom_benchmark.sandbox import Network
    from codeloom_benchmark.runner import attempt
    from codeloom_benchmark.artifacts import write_json
    from codeloom_benchmark.reporting import report
    from codeloom_benchmark.pricing import check as check_pricing
    import platform as platform_module

    w.STATE.mkdir(parents=True, exist_ok=True)
    import fcntl

    handle = (w.STATE / "worker.lock").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    w.RUN = RUN
    # Leave w.CALIBRATION at its default (a path this run never creates), so
    # publish() skips the dev-calibration import branch cleanly instead of
    # double-importing the same manifest under two names.
    w.state.update(
        phase="starting",
        message="Owner-requested standalone 'realistic' suite run (smaller-model monitoring)",
        run_id=RUN.name,
    )
    # Not w.draft(RUN): that hardcodes suite "v1" and calls tasks() with no
    # suite argument (defaulting to v1), which would embed the wrong task
    # list into this run's manifest. Build it directly instead.
    RUN.mkdir(parents=True, exist_ok=True)
    write_json(
        RUN / "manifest.json",
        {
            "suite": SUITE,
            "run_id": RUN.name,
            "settings": config(),
            "tasks": real_tasks(SUITE),
            "platform": "linux/amd64",
            "host_architecture": platform_module.machine(),
            "calibration_status": "not_run",
            "limitations": [
                "Exploratory suite for smaller-model monitoring, not part of the v1 preregistered campaign.",
                "No mutants, no mutation-score grading -- reference/hidden-oracle checks only.",
                "One run per task; no reliability, interaction, or component-effect claims.",
            ],
        },
    )
    report(RUN)
    thread = threading.Thread(target=w.heartbeat, daemon=True)
    thread.start()
    exit_code = 0
    try:
        w.run_phase(
            "validating",
            [sys.executable, "-c", f"from codeloom_benchmark.manifests import validate_suite; validate_suite({SUITE!r})"],
            "Validating the realistic-suite task fixtures",
        )
        with w.state_lock:
            w.state.update(phase="certifying", message="Certifying the realistic suite's task images")
        # reports/certification.json is shared with the real v1 benchmark (its
        # preregister() reads it); certification() writes there unconditionally
        # regardless of suite. Back it up and restore it after, so a v1
        # campaign's own cached certification record is never disturbed by
        # this exploratory suite. Per-task caches under .cache/certification/
        # are keyed by task id, which is a distinct realistic-* namespace, so
        # those don't need the same treatment.
        shared_report = ROOT / "reports/certification.json"
        backup = shared_report.with_suffix(".json.pre-realistic-backup")
        if shared_report.exists():
            backup.write_bytes(shared_report.read_bytes())
        cli.tasks = functools.partial(real_tasks, suite=SUITE)
        cli.validate_suite = functools.partial(real_validate_suite, suite=SUITE)
        cli.fingerprint = functools.partial(real_fingerprint, suite=SUITE)
        try:
            certified = cli.certification(ROOT.parent / "engine")
        finally:
            cli.tasks, cli.validate_suite, cli.fingerprint = real_tasks, real_validate_suite, real_fingerprint
            (ROOT / "reports/certification-realistic.json").write_text(
                json.dumps(certified)
            )
            if backup.exists():
                shared_report.write_bytes(backup.read_bytes())
                backup.unlink()
            elif shared_report.exists():
                shared_report.unlink()
        if certified["status"] != "certified":
            raise RuntimeError("Realistic suite failed certification")

        settings = config()
        if not settings.get("paid_runs_authorized", False):
            raise ValueError("Paid runs are on hold; explicit owner approval is required")
        if check_pricing()["status"] != "passed":
            raise ValueError("Pinned provider price exceeds the fixed admission rates")
        # A dedicated ledger, NOT the shared .cache/campaign/ledger.sqlite the
        # v1 campaign uses: this exploratory suite shouldn't compete with or
        # be constrained by the real benchmark's budget tracking at all.
        # Same Broker/Ledger/Network classes, same actual-spend policy,
        # fully isolated state.
        ledger = Ledger(
            RUN / "ledger.sqlite",
            settings["campaign_limit_usd"],
            settings["stage_limits"],
            budget_policy=settings.get("budget_policy", "strict-reservation"),
            call_limit_multiplier=settings.get("call_limit_multiplier", 3),
        )
        if ledger.snapshot()["unknown_calls"] or ledger.snapshot()["overrun"]:
            raise ValueError("Resolve unknown bills or overruns in the realistic-suite ledger first")
        if ledger.active_trials():
            raise ValueError("An active realistic-suite trial must be reconciled first")
        broker = Broker(ledger, RUN, credentials(), settings, model_metadata=model_metadata())
        port = broker.start("0.0.0.0")
        network = Network(port)
        task_rows = real_tasks(SUITE)
        images = {r["id"]: r["image"] for r in certified["tasks"]}
        with w.state_lock:
            w.state.update(phase="measuring", message="Running the realistic suite against both systems")
        try:
            for task in task_rows:
                # Both systems for a task run concurrently via the same
                # parallel_pair() the real v1 campaign uses for
                # parallel_attempts: 2 -- at most one matched pair in flight
                # at a time (one ThreadPoolExecutor per task, not a single
                # pool spanning the whole suite), tasks themselves still run
                # one after another.
                def execute(system, task=task):
                    trial_id = f"{RUN.name}-{task['id']}-{system}"
                    path = RUN / "trials" / trial_id / "result.json"
                    if path.exists():
                        print("already sealed, skipping:", trial_id, flush=True)
                        return None
                    print("running:", trial_id, flush=True)
                    result = attempt(
                        task,
                        system,
                        images[task["id"]],
                        broker,
                        network,
                        RUN,
                        trial_id,
                        # "measured", not "development": reporting.summarize()
                        # (what the live monitor's paired-outcomes table reads)
                        # only counts stage in ("measured", "recovery") --
                        # "development" attempts are invisible there even once
                        # sealed. Safe here because the ledger above is this
                        # run's own, fully isolated instance: "measured"
                        # just selects its $32 bucket over its $8
                        # "development" one, never the real v1 campaign's.
                        "measured",
                        None,  # no watchdog, same policy as the v1 campaign
                        PER_ATTEMPT_LIMIT_USD,
                    )
                    print(trial_id, result["termination"], f"${result['cost_usd']:.4f}" if result["cost_usd"] is not None else "$?", flush=True)
                    return result

                parallel_pair(settings["systems"], execute)
        finally:
            network.close()
            broker.close()
        with w.state_lock:
            w.state.update(phase="complete", message="Realistic suite finished; sealed results retained.")
    except Exception as exc:
        exit_code = 1
        with w.state_lock:
            w.state.update(phase="blocked", message=str(exc))
        print("Realistic suite stopped:", str(exc), flush=True)
    finally:
        w.stop.set()
        thread.join(timeout=180)
        with w.state_lock:
            final = w.final_status()
        published = False
        for _ in range(3):
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
            print("Final publication unavailable; local state and results retained on Railway", flush=True)
        (w.STATE / "realistic-suite-20261016.exit").write_text(str(exit_code) + "\n")
        print(json.dumps({"exit_code": exit_code, "phase": final["phase"], "message": final["message"]}), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
