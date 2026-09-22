#!/usr/bin/env python3
"""Build a review bundle from a finished run_target.py run and get Claude's
review of it, headlessly, via the `claude` CLI.

Two review passes are kept separate on purpose: `claude -p` is only asked to
produce the *analytical* parts (code_review, process_review,
overall_assessment) into `review_findings.json`. The *factual* parts (PR
url/number/SHAs, cost/token stats, file links) come straight from
`run_manifest.json` and `gh`, assembled by this script — so a fact we
already have authoritatively is never left to the model to transcribe.

    python pr_trials/review_pr.py --workdir pr_trials/runs/reach-auth-proxy

Writes <workdir>/review_bundle/{task.md,pr.md,transcript.md,trace.jsonl,
stats.json,review_findings.json} and pr_trials/reports/<run_id>-<ts>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_TRIALS = REPO_ROOT / "pr_trials"

REQUIRED_FINDINGS_KEYS = {"code_review", "process_review", "overall_assessment"}
ANTHROPIC_KEY_PLACEHOLDERS = {"", "sk-ant-...", "your-key", "changeme"}


class ReviewError(RuntimeError):
    pass


def load_env_sh(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _gh_pr_bundle(pr_url: str) -> tuple[str, dict[str, Any]]:
    view = _run([
        "gh", "pr", "view", pr_url, "--json",
        "title,body,url,number,headRefName,headRefOid,baseRefName,"
        "commits,files,additions,deletions,statusCheckRollup,"
        "headRepositoryOwner,headRepository",
    ])
    if view.returncode != 0:
        raise ReviewError(f"gh pr view failed: {view.stderr.strip()}")
    meta = json.loads(view.stdout)
    diff = _run(["gh", "pr", "diff", pr_url])
    if diff.returncode != 0:
        raise ReviewError(f"gh pr diff failed: {diff.stderr.strip()}")
    md = [
        f"# PR: {meta.get('title', '')}",
        f"url: {meta.get('url', '')}",
        f"number: {meta.get('number', '')}",
        f"head: {meta.get('headRefName', '')} @ {meta.get('headRefOid', '')}",
        f"base: {meta.get('baseRefName', '')}",
        f"files changed: {len(meta.get('files', []))}  "
        f"(+{meta.get('additions', 0)} / -{meta.get('deletions', 0)})",
        "",
        "## Body",
        meta.get("body", "") or "(empty)",
        "",
        "## Diff",
        "```diff",
        diff.stdout,
        "```",
    ]
    return "\n".join(md), meta


def build_bundle(manifest: dict[str, Any], bundle_dir: Path) -> dict[str, Any]:
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "task.md").write_text(manifest.get("task_prompt", ""))

    pr_url = (manifest.get("pr_url") or "").split(";")[0].strip()
    pr_meta: dict[str, Any] = {}
    if pr_url:
        pr_md, pr_meta = _gh_pr_bundle(pr_url)
        (bundle_dir / "pr.md").write_text(pr_md)
    else:
        (bundle_dir / "pr.md").write_text(
            "(no PR URL found in run_manifest.json — the run may have failed "
            "before settling the worktree; see transcript.md / engine.log)"
        )

    paths = manifest.get("paths", {})
    transcript_src = Path(manifest["workdir"]) / "transcript.md"
    if transcript_src.is_file():
        shutil.copy(transcript_src, bundle_dir / "transcript.md")
    else:
        (bundle_dir / "transcript.md").write_text("(no transcript)")

    trace_src = Path(paths.get("trace_jsonl", ""))
    if trace_src.is_file():
        shutil.copy(trace_src, bundle_dir / "trace.jsonl")
    else:
        (bundle_dir / "trace.jsonl").write_text("")

    stats_payload = {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "detail": manifest.get("detail"),
        "elapsed_wall_s": manifest.get("elapsed_wall_s"),
        "stats": manifest.get("stats"),
        "prometheus_metrics": manifest.get("prometheus_metrics"),
    }
    (bundle_dir / "stats.json").write_text(json.dumps(stats_payload, indent=2))

    return pr_meta


def run_claude_review(bundle_dir: Path, prompt_path: Path, timeout_s: float = 3600) -> str:
    prompt = prompt_path.read_text()
    findings_path = bundle_dir / "review_findings.json"
    if findings_path.exists():
        findings_path.unlink()
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Read Grep Glob Write",
        "--add-dir", str(bundle_dir),
    ]
    env = os.environ.copy()
    result = subprocess.run(
        cmd, cwd=str(bundle_dir), env=env,
        capture_output=True, text=True, timeout=timeout_s,
    )
    (bundle_dir / "claude_cli_stdout.json").write_text(result.stdout)
    if result.stderr:
        (bundle_dir / "claude_cli_stderr.log").write_text(result.stderr)
    if result.returncode != 0:
        raise ReviewError(f"claude -p exited {result.returncode}; see claude_cli_stderr.log")
    return result.stdout


def load_findings(bundle_dir: Path) -> dict[str, Any]:
    findings_path = bundle_dir / "review_findings.json"
    if not findings_path.is_file():
        raise ReviewError("claude did not write review_findings.json")
    try:
        findings = json.loads(findings_path.read_text())
    except json.JSONDecodeError as exc:
        raise ReviewError(f"review_findings.json is not valid JSON: {exc}") from exc
    missing = REQUIRED_FINDINGS_KEYS - findings.keys()
    if missing:
        raise ReviewError(f"review_findings.json missing keys: {sorted(missing)}")
    return findings


def run_verification(workdir: Path) -> dict[str, Any] | None:
    """Actually build/vet/test the PR branch - facts, not the LLM's opinion.

    Runs pr_trials/verify_build.py, which checks out both the PR's base
    commit and its head branch and runs the language's real toolchain
    (go build/vet/test, or a Python venv + py_compile + pytest) against
    both, so a pre-existing issue in the target repo is never misattributed
    to this PR. Returns None (rather than raising) on any failure - a
    verification hiccup must not take down the whole review.
    """
    result = subprocess.run(
        [sys.executable, str(PR_TRIALS / "verify_build.py"), "--workdir", str(workdir)],
        capture_output=True, text=True, timeout=1800,
    )
    verification_path = workdir / "verification.json"
    if not verification_path.is_file():
        print(f"verification failed: {result.stderr.strip()[-2000:]}", file=sys.stderr)
        return None
    return json.loads(verification_path.read_text())


def assemble_report(
    manifest: dict[str, Any], pr_meta: dict[str, Any], findings: dict[str, Any],
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    pr_url = (manifest.get("pr_url") or "").split(";")[0].strip()
    head_sha = pr_meta.get("headRefOid", "")
    repo_full = ""
    owner = pr_meta.get("headRepositoryOwner", {})
    if isinstance(owner, dict):
        login = owner.get("login", "")
        repo_name = pr_meta.get("headRepository", {})
        repo_name = repo_name.get("name", "") if isinstance(repo_name, dict) else ""
        if login and repo_name:
            repo_full = f"{login}/{repo_name}"

    return {
        "meta": {
            "repo": repo_full or manifest.get("repo_url", ""),
            "pr_url": pr_url,
            "pr_number": pr_meta.get("number"),
            "task_prompt": manifest.get("task_prompt", ""),
            "run_id": manifest.get("run_id"),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "engine_commit": manifest.get("engine_commit"),
            "target_commit_base": manifest.get("target_commit_base"),
            "target_commit_head": head_sha,
            "settle": manifest.get("settle"),
            "run_status": manifest.get("status"),
            "run_detail": manifest.get("detail"),
        },
        "stats": {
            **(manifest.get("stats") or {}),
            "prometheus_metrics": manifest.get("prometheus_metrics"),
        },
        "verification": (verification or {}).get("summary") or {
            "language": "unknown", "builds": None, "has_tests": None, "tests_pass": None,
            "note": "verification did not complete; see workdir/verification.json or its absence",
        },
        "code_review": findings["code_review"],
        "process_review": findings["process_review"],
        "overall_assessment": findings["overall_assessment"],
        "links": {
            "pr_url": pr_url,
            "repo_url": manifest.get("repo_url", ""),
            "diff_url": f"{pr_url}.diff" if pr_url else "",
            "trace_file": (manifest.get("paths") or {}).get("trace_jsonl", ""),
            "session_db": (manifest.get("paths") or {}).get("session_db", ""),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--prompt-file", type=Path, default=PR_TRIALS / "review_prompt.md")
    parser.add_argument("--reports-dir", type=Path, default=PR_TRIALS / "reports")
    parser.add_argument("--claude-timeout", type=float, default=3600.0)
    parser.add_argument(
        "--allow-interactive-auth",
        action="store_true",
        help="don't require ANTHROPIC_API_KEY; let `claude -p` fall back to this "
        "shell's interactive login. Fine for a one-off manual/dry-run invocation; "
        "never use this for an unattended overnight batch (there's no interactive "
        "session to fall back to, so a missing/placeholder key would otherwise "
        "silently succeed here and just as silently fail overnight).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_sh(PR_TRIALS / "env.sh")

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if api_key in ANTHROPIC_KEY_PLACEHOLDERS:
        if args.allow_interactive_auth:
            print(
                "warning: ANTHROPIC_API_KEY is unset/placeholder; falling back to "
                "this shell's interactive `claude` auth (--allow-interactive-auth)",
                file=sys.stderr,
            )
        else:
            print(
                "ANTHROPIC_API_KEY is unset or still the placeholder from "
                "env.sh.example. Set a real key in pr_trials/env.sh before running "
                "unattended (or pass --allow-interactive-auth for a one-off manual "
                "run using this shell's interactive login).",
                file=sys.stderr,
            )
            return 2

    workdir = args.workdir.expanduser().resolve()
    manifest_path = workdir / "run_manifest.json"
    if not manifest_path.is_file():
        print(f"no run_manifest.json in {workdir}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    run_id = manifest.get("run_id", workdir.name)

    bundle_dir = workdir / "review_bundle"
    print(f"[{run_id}] assembling review bundle")
    try:
        pr_meta = build_bundle(manifest, bundle_dir)
    except ReviewError as exc:
        print(f"[{run_id}] bundle assembly failed: {exc}", file=sys.stderr)
        return 1

    print(f"[{run_id}] verifying build/tests (facts, not the LLM's opinion)")
    verification = run_verification(workdir)
    if verification is None:
        print(f"[{run_id}] verification did not complete; report will note this and continue")

    print(f"[{run_id}] invoking claude -p for review")
    try:
        run_claude_review(bundle_dir, args.prompt_file, timeout_s=args.claude_timeout)
        findings = load_findings(bundle_dir)
    except (ReviewError, subprocess.TimeoutExpired) as exc:
        print(f"[{run_id}] review failed: {exc}", file=sys.stderr)
        return 1

    report = assemble_report(manifest, pr_meta, findings, verification)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    report_path = args.reports_dir / f"{run_id}-{ts}.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"[{run_id}] wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
