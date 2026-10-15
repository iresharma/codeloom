"""Resume straight into the measuring phase: certify/calibrate/preregister
already succeeded and are tagged at the current commit (benchmark-v1-
preregistered). Only the run directory name changed, to avoid trial-id
collisions with the abandoned pre-429-fix attempt's settled ledger rows."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path("/root/codeloom/workspace/benchmark")
RUN = ROOT / "runs/railway-v1-restart-20261015"
EXPECTED_MEASURED_ATTEMPTS = 18


def main():
    values = json.loads(Path("/root/benchmark-env.json").read_text())
    if not all(values.get(k) for k in ("OPENROUTER_API_KEY", "MONITOR_URL", "MONITOR_UPLOAD_TOKEN", "GITHUB_TOKEN")):
        raise RuntimeError("Missing host credentials")
    values.pop("GITHUB_TOKEN")
    os.environ.update(values)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from cloud import worker as w

    w.STATE.mkdir(parents=True, exist_ok=True)
    handle = (w.STATE / "worker.lock").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    w.RUN = RUN
    w.CALIBRATION = ROOT / "runs/railway-calibration-reused-timing-20261015"
    w.state.update(
        phase="starting",
        message="Resuming measured run on the fixed run-directory name",
        run_id=RUN.name,
    )
    w.draft(RUN)
    thread = threading.Thread(target=w.heartbeat, daemon=True)
    thread.start()
    cli = [sys.executable, "-m", "codeloom_benchmark.cli"]
    exit_code = 0
    try:
        w.run_phase(
            "measuring",
            cli + ["run", "--run-dir", str(RUN)],
            "Running all nine matched pairs under the unchanged budget gates",
        )
        primary = [
            p
            for p in (RUN / "trials").glob("*/result.json")
            if w.read(p, {}).get("stage") == "measured"
        ]
        if len(primary) != EXPECTED_MEASURED_ATTEMPTS:
            raise RuntimeError(
                f"Measured schedule did not seal all {EXPECTED_MEASURED_ATTEMPTS} primary attempts"
            )
        with w.state_lock:
            w.state.update(
                phase="complete",
                message="All 18 measured attempts finished; sealed results and private backups retained.",
            )
    except Exception as exc:
        exit_code = 1
        with w.state_lock:
            w.state.update(phase="blocked", message=str(exc))
        print("Campaign stopped:", str(exc), flush=True)
    finally:
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
        (w.STATE / "full-campaign-429-fix-20261015-measure-resume.exit").write_text(str(exit_code) + "\n")
        print(json.dumps({"exit_code": exit_code, "phase": final["phase"], "message": final["message"]}), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
