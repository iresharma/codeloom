#!/usr/bin/env python3
"""Fire-and-forget entrypoint: run every target in pr_trials/targets.json,
in parallel, and chain each straight into its review the moment its engine
run finishes. Meant to be launched via start_overnight.sh (caffeinate +
nohup) and checked on the next morning.

Progress is visible without tailing logs: pr_trials/overnight_status.json is
updated at every stage transition, and pr_trials/overnight_summary.md is
written once everything is done (or has failed).

    python3 pr_trials/run_all.py [--targets pr_trials/targets.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_TRIALS = REPO_ROOT / "pr_trials"
STATUS_PATH = PR_TRIALS / "overnight_status.json"
SUMMARY_PATH = PR_TRIALS / "overnight_summary.md"
PUSHGATEWAY_CONTAINER = "pr-trials-pushgateway"
PUSHGATEWAY_URL = "http://localhost:9091"

_status_lock = Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class StatusBoard:
    """Thread-safe, disk-persisted progress board for the whole batch."""

    def __init__(self, path: Path, target_names: list[str]):
        self._path = path
        self._state: dict[str, Any] = {
            "started_at": _now(),
            "updated_at": _now(),
            "targets": {
                name: {
                    "stage": "queued",
                    "updated_at": _now(),
                    "pr_url": None,
                    "report_path": None,
                    "error": None,
                }
                for name in target_names
            },
        }
        self._write()

    def update(self, name: str, **fields: Any) -> None:
        with _status_lock:
            self._state["targets"][name].update(fields, updated_at=_now())
            self._state["updated_at"] = _now()
            self._write()

    def _write(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(self._path)

    def snapshot(self) -> dict[str, Any]:
        with _status_lock:
            return json.loads(json.dumps(self._state))


def ensure_pushgateway() -> str:
    """Start a disposable local Pushgateway via Docker; empty string if unavailable."""
    probe = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{PUSHGATEWAY_CONTAINER}$", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0 and PUSHGATEWAY_CONTAINER in probe.stdout:
        print("pushgateway already running")
        return PUSHGATEWAY_URL
    start = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", PUSHGATEWAY_CONTAINER,
            "-p", "9091:9091",
            "prom/pushgateway",
        ],
        capture_output=True, text=True,
    )
    if start.returncode != 0:
        print(f"warning: could not start pushgateway ({start.stderr.strip()}); "
              f"prometheus_metrics will be null in reports", file=sys.stderr)
        return ""
    # give it a moment to bind before the first scrape
    time.sleep(2)
    print(f"pushgateway started: {PUSHGATEWAY_URL}")
    return PUSHGATEWAY_URL


def teardown_pushgateway() -> None:
    subprocess.run(["docker", "rm", "-f", PUSHGATEWAY_CONTAINER], capture_output=True, text=True)


def run_one_target(target: dict[str, Any], pushgateway_url: str, board: StatusBoard) -> dict[str, Any]:
    name = target["name"]
    workdir = PR_TRIALS / "runs" / name
    outcome: dict[str, Any] = {"name": name}

    if workdir.exists() and any(workdir.iterdir()):
        board.update(name, stage="failed:workdir-exists",
                      error=f"{workdir} already exists and is not empty; "
                            "move or remove it before rerunning this target")
        outcome["error"] = "workdir-exists"
        return outcome

    board.update(name, stage="cloning")
    run_cmd = [
        sys.executable, str(PR_TRIALS / "run_target.py"),
        "--repo-url", target["repo_url"],
        "--prompt-file", str(REPO_ROOT / target["prompt_file"]),
        "--workdir", str(workdir),
        "--run-id", target["run_id"],
        "--settle", target.get("settle", "pr"),
        "--max-continues", str(target.get("max_continues", 1)),
    ]
    if pushgateway_url:
        run_cmd += ["--pushgateway-url", pushgateway_url]

    board.update(name, stage="engine_running")
    result = subprocess.run(run_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    (PR_TRIALS / "runs" / name).mkdir(parents=True, exist_ok=True)
    (workdir / "run_target_stdout.log").write_text(result.stdout)
    if result.stderr:
        (workdir / "run_target_stderr.log").write_text(result.stderr)

    manifest_path = workdir / "run_manifest.json"
    if not manifest_path.is_file():
        board.update(name, stage="failed:no-manifest",
                      error="run_target.py produced no run_manifest.json; see run_target_stderr.log")
        outcome["error"] = "no-manifest"
        return outcome

    manifest = json.loads(manifest_path.read_text())
    pr_url = (manifest.get("pr_url") or "").split(";")[0].strip()
    board.update(name, stage="pr_opened" if pr_url else "engine_finished_no_pr", pr_url=pr_url or None)
    outcome["pr_url"] = pr_url
    outcome["run_status"] = manifest.get("status")

    if manifest.get("status") != "ok" and not pr_url:
        board.update(name, stage=f"failed:{manifest.get('status')}", error=manifest.get("detail"))
        outcome["error"] = manifest.get("detail")
        return outcome

    board.update(name, stage="reviewing")
    review_cmd = [sys.executable, str(PR_TRIALS / "review_pr.py"), "--workdir", str(workdir)]
    review_result = subprocess.run(review_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    (workdir / "review_pr_stdout.log").write_text(review_result.stdout)
    if review_result.stderr:
        (workdir / "review_pr_stderr.log").write_text(review_result.stderr)

    if review_result.returncode != 0:
        board.update(name, stage="failed:review", error="review_pr.py failed; see review_pr_stderr.log")
        outcome["error"] = "review-failed"
        return outcome

    report_paths = sorted((PR_TRIALS / "reports").glob(f"{target['run_id']}-*.json"))
    report_path = str(report_paths[-1]) if report_paths else None
    board.update(name, stage="done", report_path=report_path)
    outcome["report_path"] = report_path
    return outcome


def write_summary(outcomes: list[dict[str, Any]], board: StatusBoard) -> None:
    lines = ["# Overnight PR trial summary", "", f"generated {_now()}", ""]
    snap = board.snapshot()
    for outcome in outcomes:
        name = outcome["name"]
        stage = snap["targets"][name]["stage"]
        pr_url = outcome.get("pr_url") or "—"
        report = outcome.get("report_path") or "—"
        lines.append(f"## {name}")
        lines.append(f"- stage: `{stage}`")
        lines.append(f"- PR: {pr_url}")
        lines.append(f"- report: {report}")
        if outcome.get("error"):
            lines.append(f"- error: {outcome['error']}")
        lines.append("")
    SUMMARY_PATH.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=PR_TRIALS / "targets.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(args.targets.read_text())
    targets = config["targets"]
    board = StatusBoard(STATUS_PATH, [t["name"] for t in targets])

    pushgateway_url = ensure_pushgateway()
    outcomes: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {
                pool.submit(run_one_target, target, pushgateway_url, board): target["name"]
                for target in targets
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:  # noqa: BLE001 — one target's crash must not kill the batch
                    board.update(name, stage="failed:exception", error=str(exc))
                    outcomes.append({"name": name, "error": str(exc)})
                print(f"[{name}] finished: {board.snapshot()['targets'][name]['stage']}")
    finally:
        if pushgateway_url:
            teardown_pushgateway()

    write_summary(outcomes, board)
    print(f"all targets done; see {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
