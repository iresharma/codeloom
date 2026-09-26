#!/usr/bin/env python3
"""Run the engine, unattended, against one real target repo end to end.

Clones `--repo-url` into an empty `--workdir`, builds a throwaway venv for
this checkout's `workspace/engine`, boots the engine against the clone,
drives it headlessly with `--prompt-file`'s contents, waits for the
engine's own completion signal (no artificial per-task timeout — see
`wait_until_idle` in `headless_client.py`), and lets `--settle pr` open the
real PR. Adapted from `workspace/engine/scripts/bench_ab.py`'s single-side
helpers, simplified from A/B to one target, with Prometheus scraping added.

    python pr_trials/run_target.py \\
      --repo-url git@github.com:iresharma/reach-auth-proxy.git \\
      --prompt-file pr_trials/tasks/reach-auth-proxy.md \\
      --workdir pr_trials/runs/reach-auth-proxy \\
      --run-id reach-auth-proxy \\
      --settle pr

Writes `<workdir>/run_manifest.json` on completion (success or failure) and
never deletes `<workdir>` — it's the evidence bundle `review_pr.py` reads.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "workspace" / "engine"

PLACEHOLDERS = {"", "...", "<OPENROUTER_API_KEY>", "your-key", "changeme"}
_PR_URL = re.compile(r"https?://[^\s)\]>'\"*]+", re.I)


class RunError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# env.sh loading (deliberately reimplemented, stdlib-only: importing the
# engine's own llm.openrouter.load_env_sh would pull in its pip deps, and
# this script must run under plain system python3, not the per-run venv).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# git / venv / process helpers (trimmed from bench_ab.py)
# ---------------------------------------------------------------------------


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return (result.stdout or "").strip()


def _git_ok(args: list[str], *, cwd: Path) -> str:
    try:
        return _git(args, cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _require_empty(workdir: Path) -> None:
    if not workdir.exists():
        workdir.mkdir(parents=True)
        return
    if not workdir.is_dir():
        raise RunError(f"{workdir} is not a directory")
    leftover = [p.name for p in workdir.iterdir()]
    if leftover:
        raise RunError(f"{workdir} is not empty: {', '.join(sorted(leftover)[:8])}")


def _clone(url: str, dest: Path) -> None:
    subprocess.run(["git", "clone", url, str(dest)], check=True)
    _exclude_engine_state(dest)


def _exclude_engine_state(repo: Path) -> None:
    """Keep the engine's own session state out of the target's git history.

    Most target repos have never heard of this engine and don't gitignore
    `.engine/` — without this, `commit_if_dirty`'s `git add -A` sweeps
    `.engine/session.db` (full transcript) and `.engine/memory.json` straight
    into the PR. `.git/info/exclude` is local-only (never committed, never
    touches the repo's own tracked `.gitignore`), so it's safe even for a
    target that already gitignores `.engine/` itself.
    """
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.is_file() else ""
    if ".engine/" not in existing:
        with exclude_path.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(".engine/\n")


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _setup_venv(venv_dir: Path) -> None:
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    pip = venv_dir / "bin" / "pip"
    subprocess.run(
        [
            str(pip), "install", "--disable-pip-version-check", "-q",
            "-r", str(ENGINE_DIR / "requirements.txt"),
        ],
        check=True,
    )


def _wait_socket(path: Path, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        code = proc.poll()
        if code is not None:
            raise RunError(f"engine exited {code} before the socket appeared ({path})")
        time.sleep(0.1)
    raise RunError(f"timed out waiting for {path}")


def _stop(proc: subprocess.Popen, log) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log.close()


# ---------------------------------------------------------------------------
# Prometheus text-exposition parsing (no prometheus_client dependency in
# this script's own interpreter — only samples labeled for this run's
# job+instance are kept)
# ---------------------------------------------------------------------------

_METRIC_LINE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)'
)
_LABEL_PAIR = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')


def _parse_labels(raw: str) -> dict[str, str]:
    return {m.group("key"): m.group("value") for m in _LABEL_PAIR.finditer(raw or "")}


def scrape_pushgateway(url: str, job: str, instance: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/metrics", timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    metrics: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE.match(line)
        if not match:
            continue
        labels = _parse_labels(match.group("labels") or "")
        if labels.get("job") != job or labels.get("instance") != instance:
            continue
        try:
            value: Any = float(match.group("value"))
            if value.is_integer():
                value = int(value)
        except ValueError:
            continue
        name = match.group("name")
        extra = {k: v for k, v in labels.items() if k not in ("job", "instance")}
        if extra:
            metrics.setdefault(name, []).append({"labels": extra, "value": value})
        else:
            metrics[name] = value
    return metrics or None


# ---------------------------------------------------------------------------
# Result collection (trimmed from bench_ab.py's collect_side)
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    session_id: str = ""
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    turns: int = 0
    tool_calls: int = 0
    elapsed_s: float = 0.0
    agents: list[dict] = field(default_factory=list)
    files_changed: int = 0
    diffstat: str = ""
    last_reply: str = ""
    pr_url: str = ""
    error: str = ""


def _git_picture(workspace: Path) -> tuple[int, str]:
    parts: list[str] = []
    porcelain = _git_ok(["status", "--porcelain"], cwd=workspace)
    files = [line for line in porcelain.splitlines() if line.strip()]
    shortstat = _git_ok(["diff", "--shortstat"], cwd=workspace)
    if shortstat:
        parts.append(shortstat)
    trees = workspace / ".engine" / "worktrees"
    if trees.is_dir():
        for dest in sorted(p for p in trees.iterdir() if p.is_dir()):
            extra = _git_ok(["diff", "--shortstat"], cwd=dest)
            if extra:
                parts.append(f"{dest.name}: {extra}")
            extra_files = _git_ok(["status", "--porcelain"], cwd=dest)
            files.extend(line for line in extra_files.splitlines() if line.strip())
    return len(files), "; ".join(parts)


def _last_assistant(snapshot) -> str:
    abort = "(aborted by the user)"
    for message in reversed(list(snapshot.messages or [])):
        role = (message.role or "").split()[0].lower()
        if role != "assistant":
            continue
        text = (message.text or "").strip()
        if not text or text.startswith(abort):
            continue
        return text
    return ""


def _pr_urls(snapshot) -> str:
    found: list[str] = []
    seen: set[str] = set()
    for message in snapshot.messages or []:
        for match in _PR_URL.finditer(message.text or ""):
            url = match.group(0).rstrip(".,;:!?")
            if "/pull/" not in url and "/pulls/" not in url:
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)
    return "; ".join(found)


def _write_transcript(workspace: Path, dest: Path, list_sessions, load_snapshot) -> None:
    db = workspace / ".engine" / "session.db"
    sessions = list_sessions(db)
    if not sessions:
        dest.write_text("(no session)\n")
        return
    snapshot = load_snapshot(db, sessions[0].id)
    if snapshot is None:
        dest.write_text(f"(could not load session {sessions[0].id})\n")
        return
    blocks = []
    for message in snapshot.messages or []:
        role = (message.role or "?").strip() or "?"
        text = (message.text or "").rstrip() or "(empty)"
        blocks.append(f"## {role} ({message.ts})\n{text}")
    dest.write_text("\n\n".join(blocks) + "\n" if blocks else "(no messages)\n")


def collect_result(workspace: Path, list_sessions, load_snapshot) -> RunResult:
    result = RunResult()
    db = workspace / ".engine" / "session.db"
    sessions = list_sessions(db)
    if not sessions:
        result.error = "no session in sqlite"
        result.files_changed, result.diffstat = _git_picture(workspace)
        return result
    snapshot = load_snapshot(db, sessions[0].id)
    if snapshot is None:
        result.error = f"could not load session {sessions[0].id}"
        result.files_changed, result.diffstat = _git_picture(workspace)
        return result
    stats = snapshot.stats
    result.session_id = snapshot.session_id or sessions[0].id
    result.cost = float(stats.cost or 0)
    result.prompt_tokens = int(stats.prompt_tokens or 0)
    result.completion_tokens = int(stats.completion_tokens or 0)
    result.cached_tokens = int(stats.cached_tokens or 0)
    result.total_tokens = int(stats.total_tokens or 0)
    result.requests = int(stats.requests or 0)
    result.turns = int(stats.turns or 0)
    result.tool_calls = int(stats.tool_calls or 0)
    result.elapsed_s = float(stats.elapsed_s or 0)
    result.agents = [
        {"profile": row.profile, "agent_id": row.agent_id, "cost": row.cost}
        for row in (stats.agent_runs or [])
    ]
    result.last_reply = _last_assistant(snapshot)
    result.pr_url = _pr_urls(snapshot)
    result.files_changed, result.diffstat = _git_picture(workspace)
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--settle", choices=("keep", "pr", "merge", "discard"), default="pr")
    parser.add_argument("--max-continues", default="1")
    parser.add_argument("--verify-command", default="")
    parser.add_argument("--pushgateway-url", default="")
    parser.add_argument(
        "--fuse-hours",
        type=float,
        default=8.0,
        help="last-resort wall-clock circuit breaker for a truly wedged run; "
        "0 disables it. This is NOT a task-paced timeout — the client is always "
        "told to wait for the real completion signal (--timeout 0 internally).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = args.workdir.expanduser().resolve()
    _require_empty(workdir)

    load_env_sh(ENGINE_DIR / "env.sh")
    load_env_sh(REPO_ROOT / "env.sh")
    load_env_sh(REPO_ROOT / "pr_trials" / "env.sh")

    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key or api_key in PLACEHOLDERS:
        print("set OPENROUTER_API_KEY (env, workspace/engine/env.sh, or codeloom env.sh)", file=sys.stderr)
        return 2

    prompt = args.prompt_file.read_text()

    repo = workdir / "repo"
    venv_dir = workdir / "engine-venv"
    print(f"[{args.run_id}] cloning {args.repo_url}")
    _clone(args.repo_url, repo)
    base_sha = _git(["rev-parse", "HEAD"], cwd=repo)
    print(f"[{args.run_id}] base sha {base_sha}")

    print(f"[{args.run_id}] building venv")
    _setup_venv(venv_dir)

    engine_commit = _git_ok(["rev-parse", "HEAD"], cwd=ENGINE_DIR)

    env = os.environ.copy()
    env["ENGINE_TRACE_CALLS"] = "1"
    env["ENGINE_MAX_CONTINUES"] = str(args.max_continues)
    env["ENGINE_JUDGE"] = env.get("ENGINE_JUDGE", "calibrated")
    # The clone's venv holds the engine's requirements (pytest included); put
    # it first on PATH so a verify or coder `pytest` resolves to it.
    env["PATH"] = f"{venv_dir / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    if args.verify_command:
        env["ENGINE_VERIFY_CMD"] = args.verify_command
    if args.pushgateway_url:
        env["ENGINE_PUSHGATEWAY_URL"] = args.pushgateway_url
        env["ENGINE_METRICS_JOB"] = "pr-trials"
        env["ENGINE_METRICS_INSTANCE"] = args.run_id

    engine_log_path = workdir / "engine.log"
    client_log_path = workdir / "client.log"
    started_at = time.time()
    status = "ok"
    detail = ""

    with engine_log_path.open("w") as engine_log:
        engine_proc = subprocess.Popen(
            [str(_venv_python(venv_dir)), str(ENGINE_DIR / "app.py"), str(repo)],
            cwd=str(ENGINE_DIR),
            env=env,
            stdout=engine_log,
            stderr=subprocess.STDOUT,
        )
        try:
            print(f"[{args.run_id}] waiting for engine socket")
            _wait_socket(repo / ".engine" / "engine.sock", engine_proc)

            client_cmd = [
                str(_venv_python(venv_dir)),
                str(ENGINE_DIR / "dummy_client.py"),
                str(repo),
                "--message", prompt,
                "--auto",
                "--timeout", "0",  # wait for the real completion signal, no cutoff
                "--settle", args.settle,
            ]
            client_env = env.copy()
            client_env["PYTHONPATH"] = str(ENGINE_DIR) + os.pathsep + client_env.get("PYTHONPATH", "")
            fuse = args.fuse_hours * 3600 if args.fuse_hours and args.fuse_hours > 0 else None
            print(f"[{args.run_id}] driving headlessly (idle-wait; fuse={args.fuse_hours}h)")
            with client_log_path.open("w") as client_log:
                try:
                    result = subprocess.run(
                        client_cmd,
                        cwd=str(ENGINE_DIR),
                        env=client_env,
                        stdout=client_log,
                        stderr=subprocess.STDOUT,
                        timeout=fuse,
                    )
                except subprocess.TimeoutExpired:
                    status = "failed:hung"
                    detail = f"client exceeded {args.fuse_hours}h fuse"
                else:
                    if result.returncode != 0:
                        status = "failed:client"
                        detail = f"client exited {result.returncode}"
        except RunError as exc:
            status = "failed:engine"
            detail = str(exc)
        finally:
            print(f"[{args.run_id}] stopping engine")
            _stop(engine_proc, engine_log)

    finished_at = time.time()
    prom = None
    if args.pushgateway_url:
        prom = scrape_pushgateway(args.pushgateway_url, "pr-trials", args.run_id)

    list_sessions, load_snapshot = _sqlite_funcs()
    result = collect_result(repo, list_sessions, load_snapshot)
    manifest = {
        "run_id": args.run_id,
        "repo_url": args.repo_url,
        "workdir": str(workdir),
        "task_prompt": prompt,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_wall_s": finished_at - started_at,
        "status": status,
        "detail": detail,
        "engine_commit": engine_commit,
        "target_commit_base": base_sha,
        "settle": args.settle,
        "stats": {
            "session_id": result.session_id,
            "cost": result.cost,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cached_tokens": result.cached_tokens,
            "total_tokens": result.total_tokens,
            "requests": result.requests,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "elapsed_s": result.elapsed_s,
            "agents": result.agents,
            "files_changed": result.files_changed,
            "diffstat": result.diffstat,
            "error": result.error,
        },
        "prometheus_metrics": prom,
        "pr_url": result.pr_url,
        "last_reply": result.last_reply,
        "paths": {
            "repo": str(repo),
            "session_db": str(repo / ".engine" / "session.db"),
            "trace_jsonl": str(repo / ".engine" / "trace.jsonl"),
            "engine_log": str(engine_log_path),
            "client_log": str(client_log_path),
        },
    }
    (workdir / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    _write_transcript(repo, workdir / "transcript.md", list_sessions, load_snapshot)
    print(f"[{args.run_id}] done: status={status} pr_url={result.pr_url or '-'}")
    return 0 if status == "ok" else 1


def _sqlite_funcs():
    sys.path.insert(0, str(ENGINE_DIR))
    from runtime.store.sqlite import list_sessions, load as load_snapshot  # noqa: E402
    return list_sessions, load_snapshot


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {exc.cmd} (exit {exc.returncode})", file=sys.stderr)
        raise SystemExit(1) from exc
